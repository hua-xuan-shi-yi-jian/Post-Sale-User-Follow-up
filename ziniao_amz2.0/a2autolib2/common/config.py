#!/usr/bin/env python3
"""
配置加载模块
============
统一的 YAML 配置文件加载与校验。
"""

from pathlib import Path
import yaml


def load_config(config_path: str = "config.yaml") -> dict:
    """
    加载 YAML 配置文件。

    参数:
        config_path: 配置文件路径，支持相对/绝对路径

    返回:
        dict — 解析后的配置字典

    异常:
        FileNotFoundError: 配置文件不存在
        yaml.YAMLError: 格式错误
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path.resolve()}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
