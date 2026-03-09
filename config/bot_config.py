"""
机器人配置管理器
从 YAML 配置文件加载机器人配置，首次启动引导用户交互式填写
"""

import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# 默认配置文件路径
DEFAULT_CONFIG_PATH = Path(__file__).parent / "bots.yaml"

# 占位符前缀，用于检测未配置的字段
PLACEHOLDER_PREFIX = "YOUR_"


class BotConfig:
    """单个机器人的配置"""

    def __init__(
        self,
        bot_key: str,
        bot_id: str,
        secret: str = "",
        name: str = "",
        description: str = "",
        relay_url: str = "",
        working_dir: str = "",
        model: str = "",
        system_prompt: str = "",
        allowed_users: Optional[List[str]] = None,
        custom_commands: Optional[List[str]] = None,
        env_vars: Optional[Dict[str, str]] = None,
    ):
        self.bot_key = bot_key
        self.bot_id = bot_id
        self.secret = secret
        self.name = name
        self.description = description
        self.relay_url = relay_url
        self.working_dir = working_dir
        self.model = model
        self.system_prompt = system_prompt
        self.allowed_users = allowed_users or []
        self.custom_commands = custom_commands or []
        self.env_vars = env_vars or {}

    def __repr__(self):
        return (
            f"BotConfig(bot_key='{self.bot_key}', "
            f"name='{self.name}', "
            f"description='{self.description}')"
        )


class BotConfigManager:
    """机器人配置管理器 — 从 YAML 文件加载"""

    def __init__(self, config_path: str = ""):
        self.bots: Dict[str, BotConfig] = {}
        self._config_path = config_path or os.getenv("BOT_CONFIG_PATH", "") or str(DEFAULT_CONFIG_PATH)
        self._load_from_yaml(self._config_path)

    def _load_from_yaml(self, path: str):
        """从 YAML 文件加载配置"""
        config_file = Path(path)
        if not config_file.exists():
            logger.warning("配置文件不存在: %s", config_file)
            return

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error("读取配置文件失败: %s — %s", config_file, e)
            return

        bots_data = data.get("bots", {})
        if not bots_data:
            logger.warning("配置文件中没有找到 bots 配置: %s", config_file)
            return

        for bot_key, bot_data in bots_data.items():
            if not isinstance(bot_data, dict):
                continue

            bot_id = bot_data.get("bot_id", "")
            secret = bot_data.get("secret", "")

            if not bot_id or not secret:
                logger.warning("机器人 %s 配置不完整（需要 bot_id 和 secret），跳过", bot_key)
                continue

            # 跳过占位符配置
            if self._is_placeholder(bot_id) or self._is_placeholder(secret):
                continue

            bot_config = BotConfig(
                bot_key=bot_key,
                bot_id=bot_id,
                secret=secret,
                name=bot_data.get("name", ""),
                description=bot_data.get("description", ""),
                relay_url=bot_data.get("relay_url", ""),
                working_dir=bot_data.get("working_dir", ""),
                model=bot_data.get("model", ""),
                system_prompt=bot_data.get("system_prompt", ""),
                allowed_users=bot_data.get("allowed_users"),
                custom_commands=bot_data.get("custom_commands"),
                env_vars=bot_data.get("env_vars"),
            )
            self.bots[bot_key] = bot_config
            logger.info("加载机器人配置: %s", bot_config)

        if self.bots:
            logger.info("从配置文件加载 %d 个机器人: %s", len(self.bots), config_file)

    @staticmethod
    def _is_placeholder(value: str) -> bool:
        return value.upper().startswith(PLACEHOLDER_PREFIX)

    def needs_setup(self) -> bool:
        """检查是否需要引导配置"""
        return len(self.bots) == 0

    def run_setup_wizard(self) -> bool:
        """交互式引导用户配置第一个机器人，返回是否成功"""
        config_file = Path(self._config_path)

        print()
        print("=" * 58)
        print("  ClawRelay WeCom Server — 首次配置向导")
        print("=" * 58)
        print()
        print("  未检测到有效的机器人配置，请按提示填写。")
        print("  配置将保存到:", config_file)
        print()
        print("  如需获取 bot_id 和 secret，请前往：")
        print("  企业微信管理后台 → 应用管理 → 智能机器人")
        print()
        print("-" * 58)

        # bot_id
        bot_id = self._prompt("  bot_id（企业微信机器人 ID）: ").strip()
        if not bot_id:
            print("\n  [错误] bot_id 不能为空，配置中止。")
            return False

        # secret
        secret = self._prompt("  secret（企业微信机器人密钥）: ").strip()
        if not secret:
            print("\n  [错误] secret 不能为空，配置中止。")
            return False

        # relay_url
        relay_url = self._prompt(
            "  relay_url（clawrelay-api 地址）[http://localhost:50009]: "
        ).strip() or "http://localhost:50009"

        # bot name
        name = self._prompt(
            "  机器人名称（可选，用于群聊 @过滤）[AI Assistant]: "
        ).strip() or "AI Assistant"

        # description
        description = self._prompt(
            "  描述（可选）[My AI assistant]: "
        ).strip() or "My AI assistant"

        print()
        print("-" * 58)
        print("  配置预览：")
        print(f"    bot_id:    {bot_id}")
        print(f"    secret:    {secret[:6]}{'*' * (len(secret) - 6)}")
        print(f"    relay_url: {relay_url}")
        print(f"    name:      {name}")
        print()

        confirm = self._prompt("  确认保存？[Y/n]: ").strip().lower()
        if confirm and confirm not in ("y", "yes", ""):
            print("\n  已取消。")
            return False

        # 写入 YAML
        config_data = {
            "bots": {
                "default": {
                    "bot_id": bot_id,
                    "secret": secret,
                    "relay_url": relay_url,
                    "name": name,
                    "description": description,
                }
            }
        }

        try:
            config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(config_file, "w", encoding="utf-8") as f:
                f.write("# ClawRelay WeCom Server - Bot Configuration\n")
                f.write("# Generated by setup wizard. Edit as needed.\n\n")
                yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            print(f"\n  配置已保存到 {config_file}")
        except Exception as e:
            print(f"\n  [错误] 写入配置文件失败: {e}")
            return False

        # 重新加载
        self.bots.clear()
        self._load_from_yaml(self._config_path)
        return len(self.bots) > 0

    @staticmethod
    def _prompt(message: str) -> str:
        """读取用户输入，非交互式环境返回空字符串"""
        if not sys.stdin.isatty():
            return ""
        try:
            return input(message)
        except (EOFError, KeyboardInterrupt):
            print()
            return ""

    def get_bot(self, bot_key: str) -> Optional[BotConfig]:
        return self.bots.get(bot_key)

    def get_all_bots(self) -> Dict[str, BotConfig]:
        return self.bots
