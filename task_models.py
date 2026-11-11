"""Data contracts shared by the GUI and Amazon task workflow."""

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class TaskConfig:
    """Configuration for one store task."""

    company: str = ""
    username: str = ""
    password: str = ""
    store_name: str = ""
    asins: list[str] = field(default_factory=list)
    pdf_path: str = ""
    start_date: str = ""
    end_date: str = ""
    follow_up_message: str = ""
    follow_up_messages: dict = field(default_factory=dict)
    product_table_url: str = ""
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_report_table_id: str = ""
    feishu_product_table_id: str = ""
    socket_port: int = 18888
    dry_run: bool = True
    marketplace: str = ""
    sales_channels: list[str] = field(default_factory=list)


@dataclass
class OrderInfo:
    """Order data extracted from Seller Central."""

    order_id: str = ""
    buyer_name: str = ""
    asin: str = ""
    sales_channel: str = ""
    page_num: int = 1


@dataclass
class FailedRecord:
    """A single order that could not be completed."""

    asin: str = ""
    order_id: str = ""
    buyer_name: str = ""
    reason: str = ""


class RunState(Enum):
    IDLE = "等待开始"
    LAUNCHING = "启动紫鸟"
    CONNECTING = "连接浏览器"
    LOGGING_IN = "登录亚马逊"
    SEARCHING = "搜索订单"
    SENDING = "发送邮件"
    VERIFYING = "验证消息"
    FINISHED = "已完成"
    PAUSED = "已暂停"
    STOPPED = "已终止"
    ERROR = "出错"


@dataclass
class TaskStatus:
    state: RunState = RunState.IDLE
    current_asin: str = ""
    current_order_id: str = ""
    asin_index: int = 0
    asin_total: int = 0
    order_index: int = 0
    order_total: int = 0
    success_count: int = 0
    fail_count: int = 0
    message: str = ""


class StatusHolder:
    """Mutable status shared by the worker thread and GUI polling."""

    def __init__(self, store_name: str = ""):
        self.status = TaskStatus()
        self.store_name = store_name
