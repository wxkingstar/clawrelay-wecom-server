"""
简洁日志配置

只显示关键业务日志:
- 用户输入
- AI决策
- 最终输出
- 错误信息

屏蔽技术细节日志:
- 加密解密
- 流式刷新
- 数据库连接
"""

import logging


class BusinessLogFilter(logging.Filter):
    """业务日志过滤器 - 只保留关键业务信息"""

    # 必须显示的关键业务日志
    MUST_SHOW = [
        # === 数据流追踪 ===
        '[用户输入]',
        '[LLM决策]',
        '[工具执行]',
        '[最终输出]',
        '[Agent模式]',

        # === 文件处理 ===
        '[FileMerge]',
        '[文件消息]',
        '[文件合并',
        '文件消息原始数据',

        # === 连接状态 ===
        '[WsClient:',

        # === 启动信息 ===
        '企业微信机器人服务启动',
        '已注册的机器人:',

        # === 错误信息 ===
        'ERROR',
        'WARNING',
    ]

    # 必须隐藏的技术细节
    MUST_HIDE = [
        # 数据库
        '数据库连接成功',

        # 初始化细节
        '使用默认命令集',

        # 其他技术日志
        '[后台LLM任务] 开始执行',
    ]

    def filter(self, record):
        msg = record.getMessage()

        # 优先级1: 检查是否必须隐藏(最高优先级)
        for keyword in self.MUST_HIDE:
            if keyword in msg:
                return False

        # 优先级2: 检查是否必须显示
        for keyword in self.MUST_SHOW:
            if keyword in msg:
                return True

        # 优先级3: ERROR/WARNING永远显示
        if record.levelno >= logging.ERROR:
            return True

        # 默认: 隐藏其他所有日志
        return False


def setup_business_logging():
    """设置业务日志过滤器"""
    # 获取根logger
    root_logger = logging.getLogger()

    # 添加过滤器到所有handler
    for handler in root_logger.handlers:
        handler.addFilter(BusinessLogFilter())

    print("[日志配置] 已启用简洁业务日志模式")


def disable_business_logging():
    """禁用业务日志过滤器(恢复完整日志)"""
    root_logger = logging.getLogger()

    for handler in root_logger.handlers:
        # 移除所有BusinessLogFilter
        handler.filters = [
            f for f in handler.filters
            if not isinstance(f, BusinessLogFilter)
        ]

    print("[日志配置] 已恢复完整日志模式")
