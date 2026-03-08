"""
WebSocket长连接客户端

管理单个bot与企业微信服务器的WebSocket连接生命周期：
- 连接 wss://openws.work.weixin.qq.com
- 订阅 aibot_subscribe (bot_id + secret)
- 心跳 ping (30s间隔)
- 接收消息分发 (aibot_msg_callback / aibot_event_callback)
- 断线指数退避重连
"""

import asyncio
import json
import logging
import uuid
from typing import Callable, Awaitable, Dict, Optional

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_INTERVAL = 30  # seconds
HEARTBEAT_TIMEOUT = 10   # seconds
MAX_RECONNECT_DELAY = 60 # seconds


class WsClient:
    """单个bot的WebSocket连接管理器"""

    def __init__(
        self,
        bot_id: str,
        secret: str,
        bot_key: str = "",
        on_msg_callback: Optional[Callable[[dict], Awaitable[None]]] = None,
        on_event_callback: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        self.bot_id = bot_id
        self.secret = secret
        self.bot_key = bot_key or bot_id
        self._on_msg_callback = on_msg_callback
        self._on_event_callback = on_event_callback

        self._ws: Optional[ClientConnection] = None
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._reconnect_count = 0
        self._running = False

    @staticmethod
    def _generate_req_id() -> str:
        return uuid.uuid4().hex[:16]

    async def run(self):
        """主循环：连接 → 订阅 → 并发(心跳 + 收消息)，异常时重连"""
        self._running = True
        while self._running:
            try:
                await self._connect()
                await self._subscribe()
                self._reconnect_count = 0
                logger.info("[WsClient:%s] 连接就绪，开始收消息和心跳", self.bot_key)
                await asyncio.gather(
                    self._heartbeat_loop(),
                    self._receive_loop(),
                )
            except asyncio.CancelledError:
                logger.info("[WsClient:%s] 收到取消信号，退出", self.bot_key)
                break
            except Exception as e:
                logger.error("[WsClient:%s] 连接异常: %s", self.bot_key, e)
                await self._reconnect()

    async def stop(self):
        """停止连接"""
        self._running = False
        if self._ws:
            await self._ws.close()

    async def send_reply(self, payload: dict):
        """发送消息（不等待响应，fire-and-forget）"""
        if not self._ws:
            logger.error("[WsClient:%s] 连接未建立，无法发送消息", self.bot_key)
            return
        raw = json.dumps(payload, ensure_ascii=False)
        await self._ws.send(raw)
        logger.debug("[WsClient:%s] 发送消息: cmd=%s", self.bot_key, payload.get("cmd"))

    async def send_and_wait(self, payload: dict, timeout: float = 10.0) -> dict:
        """发送消息并等待响应（通过req_id关联）"""
        req_id = payload.get("headers", {}).get("req_id", "")
        if not req_id:
            raise ValueError("payload must have headers.req_id")

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[req_id] = future

        try:
            raw = json.dumps(payload, ensure_ascii=False)
            await self._ws.send(raw)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("[WsClient:%s] 等待响应超时: req_id=%s", self.bot_key, req_id)
            raise
        finally:
            self._pending_requests.pop(req_id, None)

    # ---- 内部方法 ----

    async def _connect(self):
        """建立WebSocket连接"""
        logger.info("[WsClient:%s] 正在连接 %s ...", self.bot_key, WS_URL)
        self._ws = await websockets.connect(
            WS_URL,
            ping_interval=None,  # 我们自己管理心跳
            ping_timeout=None,
            close_timeout=5,
        )
        logger.info("[WsClient:%s] WebSocket连接建立成功", self.bot_key)

    async def _subscribe(self):
        """发送订阅请求"""
        req_id = self._generate_req_id()
        payload = {
            "cmd": "aibot_subscribe",
            "headers": {"req_id": req_id},
            "body": {
                "bot_id": self.bot_id,
                "secret": self.secret,
            },
        }

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[req_id] = future

        raw = json.dumps(payload, ensure_ascii=False)
        await self._ws.send(raw)
        logger.info("[WsClient:%s] 已发送订阅请求: req_id=%s", self.bot_key, req_id)

        try:
            resp = await asyncio.wait_for(future, timeout=10.0)
        finally:
            self._pending_requests.pop(req_id, None)

        errcode = resp.get("errcode", -1)
        if errcode != 0:
            raise RuntimeError(
                f"订阅失败: errcode={errcode}, errmsg={resp.get('errmsg', '')}"
            )
        logger.info("[WsClient:%s] 订阅成功", self.bot_key)

    async def _heartbeat_loop(self):
        """心跳循环，每30秒发送ping"""
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                req_id = self._generate_req_id()
                payload = {"cmd": "ping", "headers": {"req_id": req_id}}
                resp = await self.send_and_wait(payload, timeout=HEARTBEAT_TIMEOUT)
                if resp.get("errcode", -1) != 0:
                    logger.warning("[WsClient:%s] 心跳响应异常: %s", self.bot_key, resp)
            except Exception as e:
                logger.error("[WsClient:%s] 心跳失败: %s", self.bot_key, e)
                raise  # 触发外层重连

    async def _receive_loop(self):
        """接收消息循环"""
        async for raw_msg in self._ws:
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                logger.warning("[WsClient:%s] 收到非JSON消息: %s", self.bot_key, raw_msg[:100])
                continue

            cmd = msg.get("cmd", "")
            req_id = msg.get("headers", {}).get("req_id", "")

            # 1. 检查是否是pending请求的响应
            if req_id and req_id in self._pending_requests:
                self._pending_requests[req_id].set_result(msg)
                continue

            # 2. 回调消息分发
            if cmd == "aibot_msg_callback":
                if self._on_msg_callback:
                    asyncio.create_task(self._safe_callback(
                        self._on_msg_callback, msg
                    ))
            elif cmd == "aibot_event_callback":
                event_type = msg.get("body", {}).get("event", {}).get("eventtype", "")
                if event_type == "disconnected_event":
                    logger.warning("[WsClient:%s] 收到连接断开事件，触发重连", self.bot_key)
                    raise ConnectionError("disconnected_event received")
                if self._on_event_callback:
                    asyncio.create_task(self._safe_callback(
                        self._on_event_callback, msg
                    ))
            else:
                logger.debug("[WsClient:%s] 收到未知消息: cmd=%s", self.bot_key, cmd)

    async def _safe_callback(self, callback, msg: dict):
        """安全执行回调，捕获异常防止影响receive_loop"""
        try:
            await callback(msg)
        except Exception as e:
            logger.error(
                "[WsClient:%s] 回调处理异常: cmd=%s, error=%s",
                self.bot_key, msg.get("cmd"), e, exc_info=True
            )

    async def _reconnect(self):
        """指数退避重连"""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        self._reconnect_count += 1
        delay = min(2 ** self._reconnect_count, MAX_RECONNECT_DELAY)
        logger.info(
            "[WsClient:%s] 将在 %ds 后重连 (第%d次)",
            self.bot_key, delay, self._reconnect_count
        )
        await asyncio.sleep(delay)
