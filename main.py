#!/usr/bin/env python
# coding=utf-8
"""
ClawRelay WeCom Server - WebSocket Long Connection Mode

纯asyncio入口，为每个enabled bot建立WebSocket长连接。
"""

import asyncio
import logging
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

    if config_manager.needs_setup():
        if not config_manager.run_setup_wizard():
            logger.error("配置未完成，退出")
            return
        print()

    all_configs = config_manager.get_all_bots()
    if not all_configs:
        logger.error("没有找到任何有效的机器人配置，退出")
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
