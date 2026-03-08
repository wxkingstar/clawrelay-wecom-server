# WebSocket Long Connection Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Migrate Enterprise WeChat bot from HTTP callback mode to WebSocket long connection mode, removing FastAPI and encryption dependencies entirely.

**Architecture:** Pure asyncio program. Each bot gets a WsClient Task that maintains a WebSocket connection to `wss://openws.work.weixin.qq.com`. MessageDispatcher routes incoming callbacks to existing handlers. Stream updates are pushed directly via WebSocket with 500ms throttle.

**Tech Stack:** Python 3.12+ / asyncio / websockets / MySQL / SSE streaming

**Design Doc:** `docs/plans/2026-03-09-websocket-long-connection-design.md`

---

## Task 1: Database Migration & Config Changes

**Files:**
- Create: `sql/migrate_websocket.sql`
- Modify: `config/bot_config.py`

**Step 1: Create database migration script**

Create `sql/migrate_websocket.sql`:

```sql
-- WebSocket long connection migration
-- Add secret field, make HTTP callback fields nullable

ALTER TABLE robot_bots ADD COLUMN `secret` VARCHAR(200) DEFAULT NULL
  COMMENT 'WebSocket long connection secret' AFTER `encoding_aes_key`;

ALTER TABLE robot_bots MODIFY COLUMN `token` VARCHAR(100) DEFAULT NULL
  COMMENT 'Enterprise WeChat token (deprecated, HTTP callback mode)';

ALTER TABLE robot_bots MODIFY COLUMN `encoding_aes_key` VARCHAR(50) DEFAULT NULL
  COMMENT 'Enterprise WeChat AES key (deprecated, HTTP callback mode)';

ALTER TABLE robot_bots MODIFY COLUMN `callback_path` VARCHAR(200) DEFAULT NULL
  COMMENT 'Callback path (deprecated, HTTP callback mode)';
```

**Step 2: Update BotConfig to add `secret` field**

In `config/bot_config.py`:

- `BotConfig.__init__`: Add `secret: str = ""` parameter. Make `token`, `encoding_aes_key`, `callback_path` default to `""`.
- `BotConfigManager._parse_bot_row`: Read `secret` from DB row.
- `BotConfigManager.get_bot_config` static method: Add `secret` to returned dict.
- Remove `callback_path` from required validation in `_parse_bot_row` (line `if not all([row.get('bot_id'), row.get('token'), row.get('encoding_aes_key')])` should change to only require `bot_id` and `secret`).

**Step 3: Commit**

```bash
git add sql/migrate_websocket.sql config/bot_config.py
git commit -m "feat: add secret field to BotConfig for WebSocket auth"
```

---

## Task 2: Add `websockets` Dependency

**Files:**
- Modify: `requirements.txt`

**Step 1: Add websockets to requirements.txt**

Add after the `aiohttp` line:

```
# WebSocket客户端
websockets==14.2
```

**Step 2: Install dependency**

```bash
pip install websockets==14.2
```

**Step 3: Commit**

```bash
git add requirements.txt
git commit -m "feat: add websockets dependency for long connection mode"
```

---

## Task 3: Create WsClient

**Files:**
- Create: `src/transport/__init__.py`
- Create: `src/transport/ws_client.py`

**Step 1: Create package init**

Create empty `src/transport/__init__.py`.

**Step 2: Implement WsClient**

Create `src/transport/ws_client.py` with:

```python
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
```

**Step 3: Commit**

```bash
git add src/transport/__init__.py src/transport/ws_client.py
git commit -m "feat: add WsClient for WebSocket long connection management"
```

---

## Task 4: Create MessageDispatcher

**Files:**
- Create: `src/transport/message_dispatcher.py`

**Step 1: Implement MessageDispatcher**

Create `src/transport/message_dispatcher.py` with:

```python
"""
WebSocket消息分发器

接收WsClient分发的消息回调，路由到现有handler处理，
通过WebSocket推送流式回复（500ms节流）。
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from config.bot_config import BotConfig
from src.transport.ws_client import WsClient
from src.core.claude_relay_orchestrator import ClaudeRelayOrchestrator
from src.core.session_manager import SessionManager
from src.handlers.command_handlers import CommandRouter
from src.utils.weixin_utils import ImageUtils, FileUtils
from src.utils.database import get_user_name_by_wework_user_id

logger = logging.getLogger(__name__)

# 节流间隔(秒)
STREAM_THROTTLE_INTERVAL = 0.5


class MessageDispatcher:
    """WebSocket消息分发与回复"""

    def __init__(self, ws_client: WsClient, bot_config: BotConfig):
        self.ws = ws_client
        self.config = bot_config
        self.bot_key = bot_config.bot_key

        # 命令路由器
        self.command_router = CommandRouter()

        # Claude Relay编排器
        self.orchestrator = ClaudeRelayOrchestrator(
            bot_key=bot_config.bot_key,
            relay_url=bot_config.relay_url or "http://localhost:50009",
            working_dir=bot_config.working_dir or "",
            model=bot_config.model or "",
            system_prompt=bot_config.system_prompt or "",
            env_vars=bot_config.env_vars or None,
        )

        # 会话管理
        self.session_manager = SessionManager()

        # 加载自定义命令
        self._load_custom_commands()

        # 机器人名称（用于过滤@提及）
        self.bot_name = bot_config.name or ""

        # 消息去重集合
        self._processed_msgids: dict[str, float] = {}

        logger.info("[Dispatcher:%s] 初始化完成", self.bot_key)

    def _load_custom_commands(self):
        """加载自定义命令模块"""
        if not self.config.custom_commands:
            return
        for module_path in self.config.custom_commands:
            try:
                import importlib
                module = importlib.import_module(module_path)
                if hasattr(module, 'register_commands'):
                    module.register_commands(self.command_router)
                    logger.info("[Dispatcher:%s] 加载自定义命令: %s", self.bot_key, module_path)
            except Exception as e:
                logger.error("[Dispatcher:%s] 加载自定义命令失败: %s (%s)", self.bot_key, module_path, e)

    # ---- 消息回调 ----

    async def on_msg_callback(self, msg: dict):
        """处理 aibot_msg_callback"""
        req_id = msg["headers"]["req_id"]
        body = msg["body"]
        msgid = body.get("msgid", "")

        # 消息去重
        if msgid and msgid in self._processed_msgids:
            logger.info("[Dispatcher:%s] 重复消息，跳过: msgid=%s", self.bot_key, msgid)
            return
        if msgid:
            self._processed_msgids[msgid] = time.time()
            self._cleanup_processed_msgids()

        user_id = body.get("from", {}).get("userid", "")
        msgtype = body.get("msgtype", "")
        chattype = body.get("chattype", "single")
        chatid = body.get("chatid", "")
        session_key = chatid if chattype == "group" else user_id

        logger.info(
            "[Dispatcher:%s] 收到消息: msgtype=%s, user=%s, chattype=%s, session_key=%s",
            self.bot_key, msgtype, user_id, chattype, session_key
        )

        # 用户白名单检查
        if self.config.allowed_users and user_id not in self.config.allowed_users:
            logger.warning("[Dispatcher:%s] 用户 %s 不在白名单中", self.bot_key, user_id)
            await self._reply_text(req_id, "抱歉，您没有使用此机器人的权限。\n\n如需开通权限，请联系管理员。", finish=True)
            return

        # 按消息类型路由
        if msgtype == "text":
            await self._handle_text(req_id, body, user_id, session_key, chattype)
        elif msgtype == "image":
            await self._handle_image(req_id, body, user_id, session_key, chattype)
        elif msgtype == "voice":
            await self._handle_voice(req_id, body, user_id, session_key, chattype)
        elif msgtype == "file":
            await self._handle_file(req_id, body, user_id, session_key, chattype)
        elif msgtype == "mixed":
            await self._handle_mixed(req_id, body, user_id, session_key, chattype)
        else:
            logger.warning("[Dispatcher:%s] 不支持的消息类型: %s", self.bot_key, msgtype)

    async def _handle_text(self, req_id: str, body: dict, user_id: str, session_key: str, chattype: str):
        """处理文本消息"""
        content = body.get("text", {}).get("content", "").strip()
        if not content:
            return

        # 过滤@机器人名称前缀
        if self.bot_name and content.startswith(f"@{self.bot_name} "):
            content = content[len(f"@{self.bot_name} "):].strip()
        if self.bot_name and content.startswith(f"@{self.bot_name}"):
            content = content[len(f"@{self.bot_name}"):].strip()

        # 检查命令
        normalized = content.strip().lower()

        # 重置会话命令
        if normalized in ("reset", "new", "clear", "重置", "清空"):
            await self.session_manager.clear_session(self.bot_key, session_key)
            await self._reply_text(req_id, "会话已重置，可以开始新的对话。", finish=True)
            return

        # 停止任务命令
        import re
        stop_msg = re.sub(r'[^\w\u4e00-\u9fff]', '', normalized)
        if stop_msg in ("stop", "停止", "暂停", "停"):
            from src.core.task_registry import get_task_registry
            cancelled = get_task_registry().cancel(f"{self.bot_key}:{session_key}")
            if cancelled:
                await self._reply_text(req_id, "已停止当前任务。", finish=True)
            else:
                await self._reply_text(req_id, "当前没有正在运行的任务。", finish=True)
            return

        # AskUserQuestion 文本回答
        choice_result = await self._handle_text_choice_answer(normalized, user_id, req_id)
        if choice_result:
            return

        # 调用AI处理，带节流流式推送
        stream_id = uuid.uuid4().hex[:12]
        log_context = {
            'chat_type': chattype,
            'chat_id': body.get('chatid', ''),
            'message_type': 'text',
        }

        await self._process_with_streaming(
            req_id=req_id,
            stream_id=stream_id,
            handler_coro=self.orchestrator.handle_text_message(
                user_id=user_id,
                message=content,
                stream_id=stream_id,
                session_key=session_key,
                log_context=log_context,
            ),
        )

    async def _handle_image(self, req_id: str, body: dict, user_id: str, session_key: str, chattype: str):
        """处理图片消息"""
        image_info = body.get("image", {})
        image_url = image_info.get("url", "")
        aeskey = image_info.get("aeskey", "")

        if not image_url:
            return

        try:
            data_uri = await ImageUtils.download_and_decrypt_to_base64(image_url, aeskey)
            content_blocks = [
                {"type": "text", "text": "[用户发送了一张图片] 请描述或分析这张图片"},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]
        except Exception as e:
            logger.error("[Dispatcher:%s] 图片下载解密失败: %s", self.bot_key, e)
            await self._reply_text(req_id, "图片处理失败，请重试。", finish=True)
            return

        stream_id = uuid.uuid4().hex[:12]
        log_context = {'chat_type': chattype, 'message_type': 'image'}

        await self._process_with_streaming(
            req_id=req_id,
            stream_id=stream_id,
            handler_coro=self.orchestrator.handle_multimodal_message(
                user_id=user_id,
                content_blocks=content_blocks,
                stream_id=stream_id,
                session_key=session_key,
                log_context=log_context,
            ),
        )

    async def _handle_voice(self, req_id: str, body: dict, user_id: str, session_key: str, chattype: str):
        """处理语音消息（已转为文本）"""
        voice_content = body.get("voice", {}).get("content", "")
        if not voice_content:
            await self._reply_text(req_id, "语音识别失败，请重试或发送文字。", finish=True)
            return

        # 修改body模拟文本消息，复用文本处理
        body["text"] = {"content": voice_content}
        body["_original_msgtype"] = "voice"
        await self._handle_text(req_id, body, user_id, session_key, chattype)

    async def _handle_file(self, req_id: str, body: dict, user_id: str, session_key: str, chattype: str):
        """处理文件消息"""
        file_info = body.get("file", {})
        file_url = file_info.get("url", "")
        file_name = file_info.get("filename", "")
        aeskey = file_info.get("aeskey", "")

        if not file_url:
            return

        try:
            file_bytes, header_filename = await FileUtils.download_and_decrypt(file_url, aeskey)
            if not file_name:
                file_name = header_filename or FileUtils.detect_filename_from_bytes(file_bytes)
            if not FileUtils.is_allowed(file_name):
                await self._reply_text(req_id, f"不支持的文件类型: {file_name}", finish=True)
                return
            file_data = FileUtils.encode_for_relay(file_bytes, file_name)
        except Exception as e:
            logger.error("[Dispatcher:%s] 文件下载解密失败: %s", self.bot_key, e)
            await self._reply_text(req_id, "文件处理失败，请重试。", finish=True)
            return

        stream_id = uuid.uuid4().hex[:12]
        message = f"[用户发送了文件: {file_name}] 请分析这个文件的内容。"
        log_context = {'chat_type': chattype, 'message_type': 'file', 'file_info': [{'filename': file_name}]}

        await self._process_with_streaming(
            req_id=req_id,
            stream_id=stream_id,
            handler_coro=self.orchestrator.handle_file_message(
                user_id=user_id,
                message=message,
                files=[file_data],
                stream_id=stream_id,
                session_key=session_key,
                log_context=log_context,
            ),
        )

    async def _handle_mixed(self, req_id: str, body: dict, user_id: str, session_key: str, chattype: str):
        """处理图文混排消息"""
        items = body.get("mixed", {}).get("msg_item", [])
        if not items:
            return

        content_blocks = []
        for item in items:
            item_type = item.get("msgtype", "")
            if item_type == "text":
                text = item.get("text", {}).get("content", "")
                if text:
                    content_blocks.append({"type": "text", "text": text})
            elif item_type == "image":
                image_url = item.get("image", {}).get("url", "")
                aeskey = item.get("image", {}).get("aeskey", "")
                if image_url:
                    try:
                        data_uri = await ImageUtils.download_and_decrypt_to_base64(image_url, aeskey)
                        content_blocks.append({"type": "image_url", "image_url": {"url": data_uri}})
                    except Exception as e:
                        logger.warning("[Dispatcher:%s] 混排图片解密失败: %s", self.bot_key, e)
                        content_blocks.append({"type": "text", "text": "[图片处理失败]"})

        if not content_blocks:
            return

        stream_id = uuid.uuid4().hex[:12]
        log_context = {'chat_type': chattype, 'message_type': 'mixed'}

        await self._process_with_streaming(
            req_id=req_id,
            stream_id=stream_id,
            handler_coro=self.orchestrator.handle_multimodal_message(
                user_id=user_id,
                content_blocks=content_blocks,
                stream_id=stream_id,
                session_key=session_key,
                log_context=log_context,
            ),
        )

    # ---- 事件回调 ----

    async def on_event_callback(self, msg: dict):
        """处理 aibot_event_callback"""
        req_id = msg["headers"]["req_id"]
        body = msg["body"]
        event_type = body.get("event", {}).get("eventtype", "")
        user_id = body.get("from", {}).get("userid", "")

        logger.info(
            "[Dispatcher:%s] 收到事件: eventtype=%s, user=%s",
            self.bot_key, event_type, user_id
        )

        if event_type == "enter_chat":
            await self._handle_enter_chat(req_id, body, user_id)
        elif event_type == "template_card_event":
            await self._handle_template_card_event(req_id, body, user_id)
        elif event_type == "feedback_event":
            logger.info("[Dispatcher:%s] 用户反馈事件: user=%s, body=%s", self.bot_key, user_id, body)
        elif event_type == "disconnected_event":
            # 由WsClient处理，这里不应该收到
            pass
        else:
            logger.warning("[Dispatcher:%s] 未知事件类型: %s", self.bot_key, event_type)

    async def _handle_enter_chat(self, req_id: str, body: dict, user_id: str):
        """处理进入会话事件，回复欢迎语"""
        user_name = get_user_name_by_wework_user_id(user_id) or user_id
        welcome = f"你好 {user_name}！我是AI助手，有什么可以帮您的吗？"

        payload = {
            "cmd": "aibot_respond_welcome_msg",
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": "text",
                "text": {"content": welcome},
            },
        }
        await self.ws.send_reply(payload)

    async def _handle_template_card_event(self, req_id: str, body: dict, user_id: str):
        """处理模板卡片点击事件"""
        # 复用现有choice_manager处理AskUserQuestion的卡片点击
        from src.core.choice_manager import get_choice_manager
        choice_mgr = get_choice_manager()

        task_id = body.get("event", {}).get("task_id", "")
        selected_items = body.get("event", {}).get("selected_items", {})

        if task_id.startswith("choice@"):
            # AskUserQuestion的选择回复
            logger.info("[Dispatcher:%s] 处理AskUserQuestion卡片点击: task_id=%s", self.bot_key, task_id)
            # TODO: 集成choice_manager处理逻辑
        else:
            logger.info("[Dispatcher:%s] 未知卡片事件: task_id=%s", self.bot_key, task_id)

    async def _handle_text_choice_answer(self, text: str, user_id: str, req_id: str) -> bool:
        """检查是否有待处理的AskUserQuestion，用文本作为答案"""
        from src.core.choice_manager import get_choice_manager
        choice_mgr = get_choice_manager()
        if not choice_mgr.has_pending_choice(self.bot_key, user_id):
            return False
        # TODO: 集成choice文本回答逻辑
        return False

    # ---- 流式推送 ----

    async def _process_with_streaming(
        self,
        req_id: str,
        stream_id: str,
        handler_coro,
    ):
        """执行handler并通过WebSocket节流推送流式内容

        handler_coro 是 orchestrator.handle_text_message() 等协程，
        改造后会通过 on_stream_delta 回调推送流式更新。
        """
        last_pushed_text = ""
        last_push_time = 0.0
        throttle_task: Optional[asyncio.Task] = None
        push_lock = asyncio.Lock()

        async def on_stream_delta(accumulated_text: str, finish: bool):
            """节流推送回调"""
            nonlocal last_pushed_text, last_push_time, throttle_task

            if finish:
                # 完成时立即推送最终内容
                if throttle_task and not throttle_task.done():
                    throttle_task.cancel()
                await self._reply_stream(req_id, stream_id, accumulated_text, finish=True)
                last_pushed_text = accumulated_text
                return

            # 节流：距上次推送不足STREAM_THROTTLE_INTERVAL，延迟推送
            now = time.monotonic()
            elapsed = now - last_push_time

            if elapsed >= STREAM_THROTTLE_INTERVAL and accumulated_text != last_pushed_text:
                async with push_lock:
                    await self._reply_stream(req_id, stream_id, accumulated_text, finish=False)
                    last_pushed_text = accumulated_text
                    last_push_time = time.monotonic()
            elif throttle_task is None or throttle_task.done():
                # 启动延迟推送任务
                async def delayed_push():
                    await asyncio.sleep(STREAM_THROTTLE_INTERVAL - elapsed)
                    async with push_lock:
                        nonlocal last_pushed_text, last_push_time
                        if accumulated_text != last_pushed_text:
                            await self._reply_stream(req_id, stream_id, accumulated_text, finish=False)
                            last_pushed_text = accumulated_text
                            last_push_time = time.monotonic()

                throttle_task = asyncio.create_task(delayed_push())

        try:
            # 调用orchestrator处理（改造后接受on_stream_delta回调）
            result = await handler_coro
            # handler_coro的返回值在新模式下可忽略，流式内容已通过回调推送
        except Exception as e:
            logger.error("[Dispatcher:%s] 处理消息失败: %s", self.bot_key, e, exc_info=True)
            await self._reply_stream(req_id, stream_id, f"抱歉，处理出错：{e}", finish=True)

    # ---- 回复辅助方法 ----

    async def _reply_text(self, req_id: str, content: str, finish: bool = True):
        """回复纯文本消息"""
        stream_id = uuid.uuid4().hex[:12]
        await self._reply_stream(req_id, stream_id, content, finish)

    async def _reply_stream(self, req_id: str, stream_id: str, content: str, finish: bool):
        """通过WebSocket发送流式消息回复"""
        payload = {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": "stream",
                "stream": {
                    "id": stream_id,
                    "finish": finish,
                    "content": content,
                },
            },
        }
        await self.ws.send_reply(payload)

    # ---- 工具方法 ----

    def _cleanup_processed_msgids(self):
        """清理超过5分钟的已处理消息ID"""
        now = time.time()
        expired = [k for k, v in self._processed_msgids.items() if now - v > 300]
        for k in expired:
            del self._processed_msgids[k]
```

**Step 2: Commit**

```bash
git add src/transport/message_dispatcher.py
git commit -m "feat: add MessageDispatcher for WebSocket message routing and throttled stream push"
```

---

## Task 5: Modify ClaudeRelayOrchestrator - Remove STM, Add Stream Callback

**Files:**
- Modify: `src/core/claude_relay_orchestrator.py`

**Step 1: Refactor handle_text_message**

Key changes to `src/core/claude_relay_orchestrator.py`:

1. Remove all imports and usage of `StreamingThinkingManager`, `get_streaming_thinking_manager`, `ThinkingCollector`
2. Add `on_stream_delta: Optional[Callable[[str, bool], Awaitable[None]]] = None` parameter to `handle_text_message`, `handle_multimodal_message`, `handle_file_message`
3. In SSE loop, call `on_stream_delta(accumulated_text, False)` on each `TextDelta`
4. After loop ends, call `on_stream_delta(final_text, True)`
5. Remove `MessageBuilder.text()` return — return just the final text string
6. Remove `_stream_mgr` parameter (no longer needed)
7. Keep ThinkingDelta/ToolUseStart logging, optionally pass info through on_stream_delta
8. Keep session management, chat logging, AskUserQuestion handling

The return type changes from `Tuple[str, bool]` to `str` (just the final accumulated text).

**Step 2: Commit**

```bash
git add src/core/claude_relay_orchestrator.py
git commit -m "refactor: remove STM from orchestrator, add on_stream_delta callback for WS push"
```

---

## Task 6: Modify weixin_utils - Per-Message AES Key

**Files:**
- Modify: `src/utils/weixin_utils.py`

**Step 1: Update ImageUtils.download_and_decrypt_to_base64**

Change `encoding_aes_key` parameter semantics: in long connection mode this is the per-message `aeskey` (already base64-encoded, same as EncodingAESKey format). The existing decryption logic (AES-256-CBC, PKCS#7, IV = key[:16]) is identical, so the function body stays the same. Just update the docstring.

**Step 2: Update FileUtils.download_and_decrypt**

Same change as ImageUtils — the `encoding_aes_key` parameter now accepts per-message `aeskey`. Docstring update.

**Step 3: Remove StreamManager class**

Delete the entire `StreamManager` class (lines ~444-738). It is no longer needed — stream updates are pushed directly via WebSocket.

**Step 4: Keep MessageBuilder, TemplateCardBuilder, ProactiveReplyClient, FileUtils, ImageUtils**

- `MessageBuilder` — keep for now; may be used for constructing message bodies. Can be simplified later.
- `TemplateCardBuilder` — keep, used for AskUserQuestion cards.
- `ProactiveReplyClient` — remove (HTTP-based proactive reply, replaced by `aibot_send_msg` via WS). Actually, keep for now as a fallback if needed, but mark as deprecated.

**Step 5: Commit**

```bash
git add src/utils/weixin_utils.py
git commit -m "refactor: remove StreamManager, update media decryption for per-message aeskey"
```

---

## Task 7: Create main.py Entry Point

**Files:**
- Create: `main.py`

**Step 1: Implement main.py**

```python
#!/usr/bin/env python
# coding=utf-8
"""
ClawRelay WeCom Server - WebSocket Long Connection Mode

纯asyncio入口，为每个enabled bot建立WebSocket长连接。
"""

import asyncio
import logging
import os
import signal
from dotenv import load_dotenv

from config.bot_config import BotConfigManager
from src.transport.ws_client import WsClient
from src.transport.message_dispatcher import MessageDispatcher

# Version
VERSION = "v2.0.0"

# Load environment variables
load_dotenv(override=False)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from src.utils.logging_config import setup_business_logging
setup_business_logging()


async def run_bot(bot_config) -> None:
    """为单个bot运行WebSocket连接"""
    if not bot_config.secret:
        logger.error("机器人 %s 未配置secret，跳过", bot_config.bot_key)
        return

    ws_client = WsClient(
        bot_id=bot_config.bot_id,
        secret=bot_config.secret,
        bot_key=bot_config.bot_key,
    )

    dispatcher = MessageDispatcher(ws_client, bot_config)

    # 绑定回调
    ws_client._on_msg_callback = dispatcher.on_msg_callback
    ws_client._on_event_callback = dispatcher.on_event_callback

    logger.info("启动机器人: %s (%s)", bot_config.bot_key, bot_config.description)
    await ws_client.run()


async def main():
    logger.info("=" * 60)
    logger.info("ClawRelay WeCom Server %s (WebSocket Long Connection)", VERSION)
    logger.info("=" * 60)

    # 加载bot配置
    config_manager = BotConfigManager()
    all_configs = config_manager.get_all_bots()

    if not all_configs:
        logger.error("没有找到任何enabled的机器人配置，退出")
        return

    # 为每个bot启动一个Task
    tasks = []
    for bot_key, bot_config in all_configs.items():
        logger.info("  - %s: %s", bot_key, bot_config.description)
        task = asyncio.create_task(run_bot(bot_config), name=f"bot:{bot_key}")
        tasks.append(task)

    logger.info("=" * 60)
    logger.info("共启动 %d 个机器人", len(tasks))
    logger.info("=" * 60)

    # 等待所有task（任一异常退出则全部退出）
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    # 检查异常
    for task in done:
        if task.exception():
            logger.error("机器人任务异常退出: %s - %s", task.get_name(), task.exception())

    # 取消剩余任务
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("收到退出信号，关闭服务")
```

**Step 2: Commit**

```bash
git add main.py
git commit -m "feat: add main.py entry point for WebSocket long connection mode"
```

---

## Task 8: Update Dockerfile & docker-compose.yml

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`

**Step 1: Update Dockerfile**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p logs storage

CMD ["python", "main.py"]
```

Changes: Remove `EXPOSE 5000`, remove `static` from mkdir, change CMD to `main.py`.

**Step 2: Update docker-compose.yml**

Remove `ports` mapping from app service. Remove `ADMIN_API_KEY` env var. Keep other env vars.

```yaml
  app:
    build: .
    container_name: clawrelay-wecom-server
    restart: unless-stopped
    depends_on:
      mysql:
        condition: service_healthy
    environment:
      - ENVIRONMENT=${ENVIRONMENT:-production}
      - DB_HOST=mysql
      - DB_PORT=3306
      - DB_DATABASE=${DB_DATABASE:-clawrelay_wecom}
      - DB_USERNAME=${DB_USERNAME:-clawrelay}
      - DB_PASSWORD=${DB_PASSWORD:-clawrelay123}
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

**Step 3: Commit**

```bash
git add Dockerfile docker-compose.yml
git commit -m "chore: update Dockerfile and docker-compose for WebSocket mode (no HTTP port)"
```

---

## Task 9: Delete Obsolete Files

**Files:**
- Delete: `app.py`
- Delete: `src/bot/bot_instance.py`
- Delete: `src/bot/bot_manager.py`
- Delete: `src/utils/message_crypto.py`
- Delete: `src/utils/crypto_libs/WXBizJsonMsgCrypt.py`
- Delete: `src/utils/crypto_libs/ierror.py`
- Delete: `src/utils/crypto_libs/__init__.py`
- Delete: `src/core/streaming_thinking_manager.py`
- Delete: `src/core/thinking_collector.py`
- Delete: `static/` directory (if exists)

**Step 1: Remove obsolete files**

```bash
git rm app.py
git rm src/bot/bot_instance.py
git rm src/bot/bot_manager.py
git rm src/utils/message_crypto.py
git rm -r src/utils/crypto_libs/
git rm src/core/streaming_thinking_manager.py
git rm src/core/thinking_collector.py
git rm -r static/ 2>/dev/null || true
```

**Step 2: Clean up imports**

Grep for any remaining imports of deleted modules and fix them:
- `from src.bot.bot_manager import BotManager` — removed from any file
- `from src.utils.message_crypto import MessageCrypto` — removed
- `from src.core.streaming_thinking_manager import ...` — removed
- `from src.core.thinking_collector import ...` — removed
- `from src.utils.weixin_utils import StreamManager` — removed

**Step 3: Commit**

```bash
git add -A
git commit -m "chore: remove HTTP callback mode files (app.py, bot_instance, crypto, STM)"
```

---

## Task 10: Integration Test & Fix Remaining References

**Step 1: Run syntax check on all Python files**

```bash
python -m py_compile main.py
python -m py_compile src/transport/ws_client.py
python -m py_compile src/transport/message_dispatcher.py
python -m py_compile config/bot_config.py
python -m py_compile src/core/claude_relay_orchestrator.py
python -m py_compile src/utils/weixin_utils.py
```

Fix any import errors or syntax issues.

**Step 2: Run existing tests**

```bash
pytest tests/ -v 2>&1 || true
```

Review failures — most will be from tests referencing deleted modules. Fix or remove broken tests.

**Step 3: Dry-run startup test**

```bash
python -c "
from config.bot_config import BotConfigManager
from src.transport.ws_client import WsClient
from src.transport.message_dispatcher import MessageDispatcher
print('All imports successful')
"
```

**Step 4: Commit any fixes**

```bash
git add -A
git commit -m "fix: resolve import errors and broken references after migration"
```

---

## Task 11: Update CLAUDE.md & Documentation

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Update CLAUDE.md**

Update the architecture section:

```
## Architecture

Enterprise WeChat ←WSS→ main.py (asyncio) → clawrelay-api (Go :50009) → Claude Code CLI

Key modules:
- `main.py` — Entry point, bot lifecycle management
- `src/transport/ws_client.py` — WebSocket connection, heartbeat, reconnect
- `src/transport/message_dispatcher.py` — Message routing, throttled stream push
- `src/core/claude_relay_orchestrator.py` — SSE orchestration with clawrelay-api
- `src/adapters/claude_relay_adapter.py` — HTTP/SSE client adapter
- `src/utils/weixin_utils.py` — WeChat message builders and utilities
```

Update Quick Commands:

```
## Quick Commands

# Run locally
python main.py

# Docker Compose
docker compose up -d
```

Update Database section to mention the `secret` field.

**Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for WebSocket long connection architecture"
```

---

## Execution Order Summary

| Task | Description | Dependencies |
|------|-------------|--------------|
| 1 | DB migration + BotConfig secret field | None |
| 2 | Add websockets dependency | None |
| 3 | Create WsClient | Task 2 |
| 4 | Create MessageDispatcher | Task 3 |
| 5 | Refactor orchestrator (remove STM, add callback) | None |
| 6 | Modify weixin_utils (remove StreamManager, per-msg aeskey) | None |
| 7 | Create main.py | Tasks 1, 3, 4 |
| 8 | Update Dockerfile & docker-compose | Task 7 |
| 9 | Delete obsolete files | Tasks 5, 6, 7 |
| 10 | Integration test & fix references | Task 9 |
| 11 | Update CLAUDE.md | Task 10 |

Tasks 1, 2, 5, 6 can run in parallel. Tasks 3→4→7 are sequential. Task 9 should be last before testing.
