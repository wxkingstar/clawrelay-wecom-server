"""
ClaudeRelay适配器模块

封装clawrelay-api的HTTP/SSE接口，将Claude Code CLI的流式输出
解析为结构化事件（TextDelta、ThinkingDelta、ToolUseStart）。

clawrelay-api是一个Go服务（默认端口50009），为每个请求fork一个
claude CLI子进程，并以OpenAI兼容的SSE格式流式输出结果。
"""

import json
import logging
from dataclasses import dataclass
from typing import AsyncGenerator, Dict, Optional, Union

import aiohttp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stream event dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TextDelta:
    """文本内容增量

    Attributes:
        text: 新增的文本片段
    """
    text: str


@dataclass
class ThinkingDelta:
    """思考/推理内容增量

    Attributes:
        text: 新增的思考片段
    """
    text: str


@dataclass
class ToolUseStart:
    """工具调用开始事件

    Attributes:
        name: 被调用的工具名称（如 bash、read_file 等）
    """
    name: str


@dataclass
class AskUserQuestionEvent:
    """AskUserQuestion 工具调用事件

    Claude 暂停执行，向用户提问并提供选项。
    clawrelay-api 在发出此事件后会终止当前进程，
    等待用户回答后通过相同 session_id 恢复会话。

    Attributes:
        tool_call_id: 工具调用ID（暂留，目前未使用）
        questions: 问题列表，每个问题包含 question/options/multiSelect 等字段
    """
    tool_call_id: str
    questions: list


# 联合类型，方便类型标注
StreamEvent = Union[TextDelta, ThinkingDelta, ToolUseStart, AskUserQuestionEvent]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ClaudeRelayAdapter:
    """ClawRelay API适配器

    通过HTTP/SSE连接clawrelay-api服务，将Claude Code CLI的流式输出
    解析为结构化的StreamEvent事件流。

    核心特性：
    - SSE流式解析，支持TextDelta、ThinkingDelta、ToolUseStart、AskUserQuestionEvent四种事件
    - 可配置的relay地址、模型和工作目录
    - 健康检查接口

    Example:
        >>> adapter = ClaudeRelayAdapter(
        ...     relay_url="http://localhost:50009",
        ...     model="claude-sonnet-4-6",
        ...     working_dir="/path/to/project"
        ... )
        >>> async for event in adapter.stream_chat(messages):
        ...     if isinstance(event, TextDelta):
        ...         print(event.text, end="")
    """

    def __init__(self, relay_url: str, model: str, working_dir: str, env_vars: Optional[Dict[str, str]] = None):
        """初始化ClaudeRelay适配器

        Args:
            relay_url: clawrelay-api服务地址（如 http://localhost:50009）
            model: 使用的模型标识（如 claude-sonnet-4-6）
            working_dir: Claude Code CLI的工作目录
            env_vars: 传递给Claude子进程的环境变量（可选）
        """
        self.relay_url = relay_url.rstrip("/")
        self.model = model
        self.working_dir = working_dir
        self.env_vars = env_vars or {}
        logger.info(
            f"[ClaudeRelay] 初始化适配器: relay_url={self.relay_url}, "
            f"model={self.model}, working_dir={self.working_dir}, "
            f"env_vars_count={len(self.env_vars)}"
        )

    async def stream_chat(
        self,
        messages: list[dict],
        system_prompt: str = "",
        session_id: str = "",
    ) -> AsyncGenerator[StreamEvent, None]:
        """流式聊天请求

        向clawrelay-api发送聊天请求，解析SSE流并逐个yield事件。
        文件和图片通过 messages 中的 content blocks 传递（OpenAI 多模态格式）。

        Args:
            messages: 消息列表，content 可以是字符串或 content blocks 数组
            system_prompt: 系统提示词（可选，会prepend到messages中）
            session_id: 会话ID（可选，非空时启用服务端会话管理，自动resume）

        Yields:
            StreamEvent: TextDelta | ThinkingDelta | ToolUseStart | AskUserQuestionEvent

        Raises:
            Exception: HTTP非200响应或连接错误
        """
        # 构建请求消息列表，将system_prompt作为system消息放在最前面
        request_messages = list(messages)
        if system_prompt:
            request_messages.insert(0, {
                "role": "system",
                "content": system_prompt,
            })

        # 构建请求体
        request_body = {
            "model": self.model,
            "messages": request_messages,
            "stream": True,
            "working_dir": self.working_dir,
        }
        if session_id:
            request_body["session_id"] = session_id
        if self.env_vars:
            request_body["env_vars"] = self.env_vars

        url = f"{self.relay_url}/v1/chat/completions"
        logger.info(
            f"[ClaudeRelay] POST {url}, model={self.model}, "
            f"messages_count={len(request_messages)}, "
            f"session_id={session_id or '(none)'}, stream=True"
        )

        # 超时配置：
        # - total=3600s：匹配企业微信 response_url 的最长有效期
        # - sock_read=90s：clawrelay-api 每 30s 发 SSE 心跳（: keepalive），
        #   留 3 倍余量即可快速检测上游 Claude CLI 卡死，而不必等满 total
        timeout = aiohttp.ClientTimeout(total=3600, sock_read=90)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url,
                    json=request_body,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    # 检查HTTP状态
                    if response.status != 200:
                        error_text = await response.text()
                        truncated = error_text[:500] if len(error_text) > 500 else error_text
                        raise Exception(
                            f"[ClaudeRelay] HTTP {response.status} error: {truncated}"
                        )

                    # AskUserQuestion 累积缓冲区
                    _ask_tool_call_id: str = ""
                    _ask_args_buffer: str = ""

                    # 逐行解析SSE流
                    async for raw_line in response.content:
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")

                        # 跳过空行
                        if not line:
                            continue

                        # 跳过SSE注释/心跳（以 : 开头）
                        if line.startswith(":"):
                            continue

                        # 提取 data: 前缀后的内容
                        if not line.startswith("data: "):
                            continue

                        data = line[6:]  # len("data: ") == 6

                        # 流结束标志
                        if data == "[DONE]":
                            # 流结束前，flush 累积的 AskUserQuestion
                            if _ask_args_buffer:
                                yield self._flush_ask_event(
                                    _ask_tool_call_id, _ask_args_buffer
                                )
                            logger.info("[ClaudeRelay] SSE stream ended: [DONE]")
                            return

                        # 解析JSON
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError as e:
                            logger.warning(
                                f"[ClaudeRelay] JSON parse error, skipping line: {e} | data={data[:200]}"
                            )
                            continue

                        # 从 choices[0].delta 提取事件
                        choices = chunk.get("choices")
                        if not choices:
                            continue

                        delta = choices[0].get("delta")
                        if not delta:
                            continue

                        # DEBUG: 记录 delta 中的所有 key（排除高频的 content/thinking）
                        delta_keys = set(delta.keys()) - {"content", "thinking", "role"}
                        if delta_keys:
                            logger.info(
                                f"[ClaudeRelay] delta extra keys: {delta_keys}, "
                                f"raw={json.dumps(delta, ensure_ascii=False)[:500]}"
                            )

                        # TextDelta: content字段
                        content = delta.get("content")
                        if content:
                            yield TextDelta(text=content)

                        # ThinkingDelta: thinking字段
                        thinking = delta.get("thinking")
                        if thinking:
                            yield ThinkingDelta(text=thinking)

                        # ToolUseStart / AskUserQuestionEvent: tool_calls字段
                        tool_calls = delta.get("tool_calls")
                        if tool_calls:
                            for tool_call in tool_calls:
                                func = tool_call.get("function", {})
                                name = func.get("name")

                                if name == "AskUserQuestion":
                                    # 首个 chunk：记录 tool_call_id，开始累积
                                    _ask_tool_call_id = tool_call.get("id", "")
                                    _ask_args_buffer = func.get("arguments", "")
                                    logger.info(
                                        f"[ClaudeRelay] AskUserQuestion started, "
                                        f"tool_call_id={_ask_tool_call_id}"
                                    )
                                elif name:
                                    # 其他工具调用
                                    logger.info(f"[ClaudeRelay] Tool call detected: {name}")
                                    yield ToolUseStart(name=name)
                                elif _ask_args_buffer is not None and not name:
                                    # 后续 chunk：无 name，累积 arguments 片段
                                    args_fragment = func.get("arguments", "")
                                    if args_fragment:
                                        _ask_args_buffer += args_fragment

                    # 流正常结束（无 [DONE] 标志），flush 累积的 AskUserQuestion
                    if _ask_args_buffer:
                        yield self._flush_ask_event(
                            _ask_tool_call_id, _ask_args_buffer
                        )

        except aiohttp.ClientError as e:
            raise Exception(
                f"[ClaudeRelay] Connection error to {self.relay_url}: {e}"
            ) from e

    @staticmethod
    def _flush_ask_event(tool_call_id: str, args_buffer: str) -> AskUserQuestionEvent:
        """解析累积的 AskUserQuestion arguments 并构造事件"""
        try:
            args = json.loads(args_buffer) if args_buffer else {}
        except json.JSONDecodeError:
            logger.warning(
                f"[ClaudeRelay] AskUserQuestion arguments parse error: "
                f"{args_buffer[:200]}"
            )
            args = {}
        questions = args.get("questions", [])
        logger.info(
            f"[ClaudeRelay] AskUserQuestion complete: "
            f"{len(questions)} questions, tool_call_id={tool_call_id}"
        )
        return AskUserQuestionEvent(
            tool_call_id=tool_call_id,
            questions=questions,
        )

    async def check_health(self) -> bool:
        """检查clawrelay-api服务健康状态

        Returns:
            bool: 服务是否健康
        """
        url = f"{self.relay_url}/health"
        timeout = aiohttp.ClientTimeout(total=5)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.warning(
                            f"[ClaudeRelay] Health check failed: HTTP {response.status}"
                        )
                        return False

                    data = await response.json()
                    healthy = data.get("status") == "healthy"
                    if healthy:
                        logger.info("[ClaudeRelay] Health check passed")
                    else:
                        logger.warning(
                            f"[ClaudeRelay] Health check: unexpected status={data.get('status')}"
                        )
                    return healthy

        except (aiohttp.ClientError, Exception) as e:
            logger.warning(f"[ClaudeRelay] Health check error: {e}")
            return False
