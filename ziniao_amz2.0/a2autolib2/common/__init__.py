"""通用工具模块 — 配置加载、日志管理。"""

from .config import load_config
from .logging import setup_file_logging

__all__ = ["load_config", "setup_file_logging"]
