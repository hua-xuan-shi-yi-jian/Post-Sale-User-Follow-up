#!/usr/bin/env python3
"""
项目测试套件 — 亚马逊批量发送产品说明书邮件
============================================
测试范围：数据模型、工具函数、正则表达式、配置管理、URL 构造、CDP 诊断工具
"""

import csv
import json
import os
import re
import sys
import time
import unittest
import tempfile
from datetime import datetime
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch, MagicMock

# 将项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================================
# 测试数据模型 (从 amazon_bulk_send 导入)
# ============================================================================

class TestDataModels(unittest.TestCase):
    """测试 dataclass 数据模型"""

    def test_task_config_defaults(self):
        """TaskConfig 默认值"""
        from amazon_bulk_send import TaskConfig
        tc = TaskConfig()
        self.assertEqual(tc.company, "")
        self.assertEqual(tc.socket_port, 18888)
        self.assertTrue(tc.dry_run)
        self.assertEqual(tc.asins, [])

    def test_task_config_custom(self):
        """TaskConfig 自定义值"""
        from amazon_bulk_send import TaskConfig
        tc = TaskConfig(
            company="测试公司",
            username="testuser",
            password="testpass",
            store_name="EXAMPLE-STORE-UK",
            asins=["B0TEST1", "B0TEST2"],
            pdf_path="/path/to/manual.pdf",
            start_date="2026-01-01",
            end_date="2026-06-17",
            follow_up_message="感谢购买",
            socket_port=9222,
            dry_run=False,
        )
        self.assertEqual(tc.company, "测试公司")
        self.assertEqual(tc.asins, ["B0TEST1", "B0TEST2"])
        self.assertFalse(tc.dry_run)

    def test_order_info(self):
        """OrderInfo 数据模型"""
        from amazon_bulk_send import OrderInfo
        o = OrderInfo(
            order_id="123-4567890-1234567",
            buyer_name="张三",
            asin="B0TEST01",
        )
        self.assertEqual(o.order_id, "123-4567890-1234567")
        self.assertEqual(o.buyer_name, "张三")

    def test_failed_record(self):
        """FailedRecord 数据模型"""
        from amazon_bulk_send import FailedRecord
        fr = FailedRecord(
            asin="B0TEST01",
            order_id="123-4567890-1234567",
            buyer_name="张三",
            reason="PDF 上传失败",
        )
        self.assertEqual(fr.reason, "PDF 上传失败")

    def test_task_status_defaults(self):
        """TaskStatus 默认值"""
        from amazon_bulk_send import TaskStatus
        ts = TaskStatus()
        self.assertEqual(ts.asin_index, 0)
        self.assertEqual(ts.success_count, 0)
        self.assertEqual(ts.fail_count, 0)

    def test_run_state_enum(self):
        """RunState 枚举值"""
        from amazon_bulk_send import RunState
        self.assertEqual(RunState.IDLE.value, "等待开始")
        self.assertEqual(RunState.FINISHED.value, "已完成")
        self.assertEqual(RunState.PAUSED.value, "已暂停")
        self.assertEqual(RunState.STOPPED.value, "已终止")


# ============================================================================
# 测试工具函数
# ============================================================================

class TestUtilityFunctions(unittest.TestCase):
    """测试工具函数"""

    def test_north_american_sales_channels_use_us_follow_up_template(self):
        from amazon_bulk_send import get_follow_up_message_for_order

        templates = {
            "amazon.com": "North America template",
            "amazon.ca": "old Canada template",
            "amazon.com.mx": "old Mexico template",
            "amazon.de": "Germany template",
        }
        for channel in ("Amazon.com", "Amazon.ca", "Amazon.com.mx"):
            with self.subTest(channel=channel):
                self.assertEqual(
                    get_follow_up_message_for_order(templates, channel),
                    "North America template",
                )
        self.assertEqual(
            get_follow_up_message_for_order(templates, "Amazon.de"),
            "Germany template",
        )

    def test_date_pattern_valid(self):
        """日期格式校验：合法输入"""
        from amazon_bulk_send import _DATE_PATTERN
        self.assertIsNotNone(_DATE_PATTERN.match("2026-06-17"))
        self.assertIsNotNone(_DATE_PATTERN.match("2026-01-01"))
        self.assertIsNotNone(_DATE_PATTERN.match("2025-12-31"))

    def test_date_pattern_invalid(self):
        """日期格式校验：非法输入"""
        from amazon_bulk_send import _DATE_PATTERN
        self.assertIsNone(_DATE_PATTERN.match("2026-6-17"))   # 单数字月
        self.assertIsNone(_DATE_PATTERN.match("2026/06/17"))  # 错误分隔符
        self.assertIsNone(_DATE_PATTERN.match("abc"))
        self.assertIsNone(_DATE_PATTERN.match(""))
        self.assertIsNone(_DATE_PATTERN.match("2026-06-17-extra"))

    def test_timestamp_str_format(self):
        """时间戳字符串格式"""
        from amazon_bulk_send import timestamp_str
        ts = timestamp_str()
        self.assertEqual(len(ts), 15)  # YYYYmmdd_HHMMSS
        self.assertIn("_", ts)
        # 可解析为日期时间
        dt = datetime.strptime(ts, "%Y%m%d_%H%M%S")
        self.assertIsInstance(dt, datetime)

    def test_order_id_pattern(self):
        """订单号正则提取"""
        pattern = re.compile(r"(\d{3}-\d{7}-\d{7})")

        # 有效格式
        self.assertIsNotNone(pattern.search("123-4567890-1234567"))
        self.assertIsNotNone(pattern.search("Order: 999-8888777-6666555"))

        # 无效格式
        self.assertIsNone(pattern.search("12-4567890-1234567"))    # 前缀不足3位
        self.assertIsNone(pattern.search("123-456789-1234567"))    # 中段不足7位
        self.assertIsNone(pattern.search("no-order-here"))

    def test_asin_parsing(self):
        """ASIN 列表解析"""
        # 单个 ASIN
        asins = [a.strip() for a in "B0TEST01".split(",") if a.strip()]
        self.assertEqual(asins, ["B0TEST01"])

        # 多个 ASIN（逗号分隔）
        asins = [a.strip() for a in "B0TEST01, B0TEST02, B0TEST03".split(",") if a.strip()]
        self.assertEqual(len(asins), 3)
        self.assertEqual(asins[0], "B0TEST01")

        # 空值处理
        asins = [a.strip() for a in "B0TEST01,,B0TEST02".split(",") if a.strip()]
        self.assertEqual(asins, ["B0TEST01", "B0TEST02"])

        # 全空
        asins = [a.strip() for a in "".split(",") if a.strip()]
        self.assertEqual(asins, [])

    def test_random_delay_range(self):
        """随机延迟范围"""
        import random
        from amazon_bulk_send import MIN_DELAY, MAX_DELAY
        self.assertGreater(MAX_DELAY, MIN_DELAY)
        self.assertGreater(MIN_DELAY, 0)


# ============================================================================
# 测试配置管理
# ============================================================================

class TestConfigManagement(unittest.TestCase):
    """测试配置文件的读写"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / "test_config.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_and_load_config(self):
        """配置保存和加载"""
        test_data = {
            "superbrowser_path": r"D:\test\starter.exe",
            "company": "测试公司",
            "username": "testuser",
            "password": "testpass",
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(test_data, f, ensure_ascii=False, indent=2)

        with open(self.config_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)

        self.assertEqual(loaded["company"], "测试公司")
        self.assertEqual(loaded["username"], "testuser")

    def test_load_missing_config(self):
        """加载不存在的配置文件"""
        nonexistent = Path(self.temp_dir) / "nonexistent.json"
        if nonexistent.exists():
            nonexistent.unlink()
        # 应该返回空 dict
        result = {}
        if nonexistent.exists():
            with open(nonexistent, "r") as f:
                result = json.load(f)
        self.assertEqual(result, {})

    def test_config_with_unicode(self):
        """配置文件包含中文"""
        test_data = {"company": "EXAMPLE-COMPANY"}
        config_path = Path(self.temp_dir) / "unicode_config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(test_data, f, ensure_ascii=False)

        with open(config_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        self.assertEqual(loaded["company"], "EXAMPLE-COMPANY")

    def test_gui_saves_feishu_credentials_with_login_config(self):
        import amazon_bulk_send as app

        pdf = Path(self.temp_dir) / "manual.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        dialog = object.__new__(app.InputDialog)
        values = {
            "entry_company": "company", "entry_username": "user",
            "entry_password": "password", "entry_feishu_app_id": "app-id",
            "entry_feishu_app_secret": "app-secret", "entry_store1": "store",
            "entry_feishu_report_table_id": "tblReport123",
            "entry_feishu_product_table_id": "tblProduct456",
            "entry_superbrowser": "", "entry_asins1": "B0TEST1234",
            "entry_pdf1": str(pdf), "entry_start_date1": "2026-09-01",
            "entry_end_date1": "2026-09-30", "entry_port": "18888",
        }
        for name, value in values.items():
            entry = MagicMock()
            entry.get.return_value = value
            setattr(dialog, name, entry)
        dialog.text_followup1 = MagicMock()
        dialog.text_followup1.get.return_value = "Hello"
        dialog.marketplace_var1 = MagicMock()
        dialog.marketplace_var1.get.return_value = ""
        dialog.sales_channel_vars1 = {
            "Amazon.de": MagicMock(), "Amazon.fr": MagicMock(),
        }
        dialog.sales_channel_vars1["Amazon.de"].get.return_value = True
        dialog.sales_channel_vars1["Amazon.fr"].get.return_value = False
        dialog.root = MagicMock()
        dialog._transition_to_exec_mode = MagicMock()
        dialog._poll_status = MagicMock()
        saved = {}
        with patch.object(app, "load_config", return_value={}), \
             patch.object(app, "save_config", side_effect=lambda data: saved.update(data)), \
             patch.object(app, "set_tk_root"), \
             patch.object(app.threading, "Thread") as worker, \
             patch.object(app.messagebox, "showerror") as error:
            dialog._on_start()

        error.assert_not_called()
        self.assertEqual(saved["feishu_app_id"], "app-id")
        self.assertEqual(saved["feishu_app_secret"], "app-secret")
        self.assertEqual(saved["feishu_report_table_id"], "tblReport123")
        self.assertEqual(saved["feishu_product_table_id"], "tblProduct456")
        self.assertEqual(saved["sales_channels"], ["Amazon.de"])
        self.assertEqual(dialog._status_holder.store_name, "store")
        worker.return_value.start.assert_called_once_with()


# ============================================================================
# 测试 URL 和网络常量
# ============================================================================

class TestConstants(unittest.TestCase):
    """测试常量和 URL 构造"""

    def test_amazon_urls(self):
        """亚马逊卖家后台 URL"""
        from amazon_bulk_send import AMAZON_SELLER_CENTRAL, ORDERS_PAGE_URL, MESSAGES_PAGE_URL
        self.assertIn("sellercentral.amazon", AMAZON_SELLER_CENTRAL)
        self.assertIn("orders", ORDERS_PAGE_URL)
        self.assertIn("messaging", MESSAGES_PAGE_URL)
        self.assertTrue(AMAZON_SELLER_CENTRAL.startswith("https://"))

    def test_default_socket_port(self):
        """紫鸟默认 API 端口"""
        from amazon_bulk_send import DEFAULT_SOCKET_PORT
        self.assertEqual(DEFAULT_SOCKET_PORT, 18888)

    def test_zigniao_api_timeout(self):
        """紫鸟 API 超时设置"""
        from amazon_bulk_send import ZIGNIAO_API_TIMEOUT
        self.assertGreaterEqual(ZIGNIAO_API_TIMEOUT, 120)

    def test_delay_constants(self):
        """操作延迟范围"""
        from amazon_bulk_send import MIN_DELAY, MAX_DELAY
        self.assertLess(MIN_DELAY, MAX_DELAY)
        self.assertGreater(MIN_DELAY, 0)

    def test_superbrowser_search_paths(self):
        """紫鸟默认搜索路径"""
        from amazon_bulk_send import SUPERBROWSER_DEFAULT_SEARCH_PATHS
        self.assertGreater(len(SUPERBROWSER_DEFAULT_SEARCH_PATHS), 0)
        for path in SUPERBROWSER_DEFAULT_SEARCH_PATHS:
            self.assertTrue(path.endswith("starter.exe"), f"{path} 应该以 starter.exe 结尾")

    def test_output_dir(self):
        """输出目录常量"""
        from amazon_bulk_send import OUTPUT_DIR
        self.assertTrue(OUTPUT_DIR.exists())
        self.assertTrue(OUTPUT_DIR.is_dir())

    def test_epoch_conversion(self):
        """日期→Epoch 毫秒转换"""
        from datetime import datetime, timezone
        import calendar

        def _to_epoch_ms(date_str, is_end=False):
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            ts = calendar.timegm(dt.timetuple())
            if is_end:
                ts += 86399
            return ts * 1000

        # 2026-01-01 00:00:00 UTC
        start_ms = _to_epoch_ms("2026-01-01", is_end=False)
        self.assertGreater(start_ms, 1700000000000)

        # 同一天 end 应该比 start 大 86399000 毫秒 (23:59:59)
        end_ms = _to_epoch_ms("2026-01-01", is_end=True)
        self.assertEqual(end_ms - start_ms, 86399000)


# ============================================================================
# 测试 CDP 端口诊断工具
# ============================================================================

class TestCDPDiagnostic(unittest.TestCase):
    """测试 find_cdp_port.py 中的辅助函数"""

    def test_check_cdp_endpoint_import(self):
        """CDP 端点检测函数可导入"""
        from find_cdp_port import check_cdp_endpoint
        self.assertTrue(callable(check_cdp_endpoint))

    def test_check_cdp_endpoint_invalid_port(self):
        """无效端口的 CDP 检测返回 None"""
        from find_cdp_port import check_cdp_endpoint
        # 使用一个几乎肯定没有服务监听的端口
        result = check_cdp_endpoint(19999)
        self.assertIsNone(result)


# ============================================================================
# 测试 CSV 导出
# ============================================================================

class TestCSVExport(unittest.TestCase):
    """测试失败记录 CSV 导出"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_export_failed_records(self):
        """导出失败记录为 CSV"""
        from amazon_bulk_send import FailedRecord

        records = [
            FailedRecord(asin="B0A", order_id="111-1111111-1111111", buyer_name="买家A", reason="发送失败"),
            FailedRecord(asin="B0B", order_id="222-2222222-2222222", buyer_name="买家B", reason="验证失败"),
        ]

        filepath = Path(self.temp_dir) / "test_failed.csv"
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["ASIN", "订单编号", "买家名称", "失败原因"])
            for r in records:
                writer.writerow([r.asin, r.order_id, r.buyer_name, r.reason])

        self.assertTrue(filepath.exists())

        # 验证内容
        with open(filepath, "r", encoding="utf-8-sig") as f:
            content = f.read()
        self.assertIn("B0A", content)
        self.assertIn("发送失败", content)
        self.assertIn("B0B", content)


# ============================================================================
# 测试语法和导入
# ============================================================================

class TestSyntaxAndImports(unittest.TestCase):
    """测试语法正确性和模块导入"""

    def test_main_script_syntax(self):
        """主脚本语法检查"""
        import py_compile
        script_path = PROJECT_ROOT / "amazon_bulk_send.py"
        result = py_compile.compile(str(script_path), doraise=True)
        self.assertIsNotNone(result)

    def test_cdp_script_syntax(self):
        """CDP 诊断脚本语法检查"""
        import py_compile
        script_path = PROJECT_ROOT / "find_cdp_port.py"
        result = py_compile.compile(str(script_path), doraise=True)
        self.assertIsNotNone(result)

    def test_main_imports(self):
        """主脚本所有导入可用"""
        import amazon_bulk_send
        self.assertTrue(hasattr(amazon_bulk_send, 'TaskConfig'))
        self.assertTrue(hasattr(amazon_bulk_send, 'OrderInfo'))
        self.assertTrue(hasattr(amazon_bulk_send, 'FailedRecord'))
        self.assertTrue(hasattr(amazon_bulk_send, 'RunState'))
        self.assertTrue(hasattr(amazon_bulk_send, 'TaskStatus'))
        self.assertTrue(hasattr(amazon_bulk_send, 'ZigniaoAPI'))
        self.assertTrue(hasattr(amazon_bulk_send, 'run_task'))
        self.assertTrue(hasattr(amazon_bulk_send, 'timestamp_str'))
        self.assertTrue(hasattr(amazon_bulk_send, 'load_config'))
        self.assertTrue(hasattr(amazon_bulk_send, 'save_config'))
        self.assertTrue(hasattr(amazon_bulk_send, 'get_superbrowser_path'))

    def test_find_cdp_port_imports(self):
        """CDP 诊断脚本可导入"""
        import find_cdp_port
        self.assertTrue(hasattr(find_cdp_port, 'check_cdp_endpoint'))
        self.assertTrue(hasattr(find_cdp_port, 'SCAN_RANGE'))

    def test_selector_registry_keeps_ordered_fallbacks(self):
        """页面改版时更新注册表仍保留候选顺序和动态订单号。"""
        from amazon_selectors import format_selectors, get_selectors

        send = get_selectors("send_button")
        self.assertGreaterEqual(len(send), 2)
        self.assertEqual(send[0], 'kat-button[label="发送"]')
        items = format_selectors("message_item", order_id="123-4567890-1234567")
        self.assertIn("123-4567890-1234567", items[0])


# ============================================================================
# 测试 ZigniaoAPI URL 构造
# ============================================================================

class TestZigniaoAPI(unittest.TestCase):
    """测试紫鸟 API 客户端（不实际发起网络请求）"""

    def test_api_url_construction(self):
        """API URL 构造"""
        from amazon_bulk_send import ZigniaoAPI
        api = ZigniaoAPI(port=18888, company="test_co", username="user1", password="pass1")
        self.assertEqual(api.base_url, "http://127.0.0.1:18888")
        self.assertEqual(api.company, "test_co")

    def test_request_id_counter(self):
        """请求 ID 计数器递增"""
        from amazon_bulk_send import ZigniaoAPI
        api = ZigniaoAPI(port=18888, company="test", username="u", password="p")
        initial = api._request_id_counter
        # 构造请求数据（不实际发送）
        data = {"action": "test"}
        api._request_id_counter += 1
        data["requestId"] = f"req_{api._request_id_counter}_{int(time.time())}"
        self.assertGreater(api._request_id_counter, initial)


class TestStoreValidation(unittest.TestCase):
    def test_page_title_is_not_treated_as_store_name(self):
        from ziniao_browser import validate_store_name

        page = MagicMock()
        page.locator.return_value.first.count.return_value = 0
        page.title.return_value = "新的亚马逊销售体验"

        self.assertTrue(validate_store_name(page, "EXAMPLE-STORE-US"))
        page.title.assert_not_called()

    def test_explicit_page_store_mismatch_is_rejected(self):
        from ziniao_browser import validate_store_name

        page = MagicMock()
        page.locator.return_value.first.count.return_value = 1
        page.locator.return_value.first.inner_text.return_value = "OTHER-US"

        self.assertFalse(validate_store_name(page, "EXAMPLE-STORE-US"))

    def test_fuzzy_ziniao_store_match_does_not_start_browser(self):
        import amazon_bulk_send as app

        config = app.TaskConfig(store_name="EXAMPLE-STORE-US")
        with patch.object(app, "ZiniaoBrowser") as browser_class, \
             patch.object(app, "get_superbrowser_path", return_value="starter.exe"):
            browser = browser_class.return_value
            browser.launch.return_value = True
            browser.api.find_store.return_value = {
                "browserName": "EXAMPLE-STORE-US-OTHER", "browserOauth": "other-store"
            }
            with self.assertRaisesRegex(RuntimeError, "未精确匹配"):
                app.open_store_via_api(config)
            browser.api.start_browser.assert_not_called()


class TestPagewiseOrders(unittest.TestCase):
    def test_visible_order_is_processed_without_reloading_current_page(self):
        import amazon_bulk_send as app

        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/orders-v3/search?q=B0TEST123&qt=asin"
        page.locator.return_value.filter.return_value.count.return_value = 1
        order = app.OrderInfo(order_id="111-1111111-1111111")

        self.assertTrue(app._order_row_on_current_page(page, order))
        page.url = "https://sellercentral.amazon.com/messaging/inbox"
        self.assertFalse(app._order_row_on_current_page(page, order))

    def test_next_page_is_loaded_only_after_current_batch_is_consumed(self):
        import amazon_bulk_send as app

        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/orders-v3/search?q=B0TEST123&qt=asin"
        first = app.OrderInfo(order_id="111-1111111-1111111", page_num=1)
        second = app.OrderInfo(order_id="222-2222222-2222222", page_num=2)
        with patch.object(app, "_ensure_100_per_page", return_value=True), \
             patch.object(app, "_get_total_orders", return_value=200), \
             patch.object(app, "random_delay"), \
             patch.object(app, "_wait_orders_table_ready"), \
             patch.object(app, "_extract_orders_from_current_page",
                          side_effect=[[first], [second]]):
            batches = app._iter_paid_order_pages(page, "B0TEST123")
            self.assertEqual(next(batches), [first])
            page.goto.assert_not_called()
            self.assertEqual(next(batches), [second])
            page.goto.assert_called_once_with(
                page.url + "&page=2", timeout=30000, wait_until="domcontentloaded")

    def test_missing_total_does_not_stop_on_a_short_matching_page(self):
        import amazon_bulk_send as app

        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/orders-v3/search?q=B0TEST123&qt=asin"
        page.locator.return_value.all_text_contents.side_effect = [
            ["111-1111111-1111111"], ["222-2222222-2222222"], [], []
        ]
        first = app.OrderInfo(order_id="111-1111111-1111111", page_num=1)
        second = app.OrderInfo(order_id="222-2222222-2222222", page_num=2)
        with patch.object(app, "_ensure_100_per_page", return_value=True), \
             patch.object(app, "_get_total_orders", return_value=0), \
             patch.object(app, "random_delay"), \
             patch.object(app, "_wait_orders_table_ready"), \
             patch.object(app, "_extract_orders_from_current_page",
                          side_effect=[[first], [second], [], []]):
            iterator = app._iter_paid_order_pages(page, "B0TEST123")
            first_batch = next(iterator)
            self.assertEqual(page.locator.return_value.all_text_contents.call_count, 1)
            page.url = "https://sellercentral.amazon.com/messaging/inbox"
            batches = [first_batch, *iterator]

        self.assertEqual(batches[:2], [[first], [second]])
        self.assertEqual(page.goto.call_count, 3)

    def test_unpaid_pages_do_not_hide_later_matching_orders(self):
        import amazon_bulk_send as app

        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/orders-v3/search?q=B0TEST123&qt=asin"
        page.locator.return_value.all_text_contents.side_effect = [
            ["111-1111111-1111111"], ["222-2222222-2222222"],
            ["333-3333333-3333333"], [], [],
        ]
        later = app.OrderInfo(order_id="333-3333333-3333333", page_num=3)
        with patch.object(app, "_ensure_100_per_page", return_value=True), \
             patch.object(app, "_get_total_orders", return_value=0), \
             patch.object(app, "random_delay"), \
             patch.object(app, "_wait_orders_table_ready"), \
             patch.object(app, "_extract_orders_from_current_page",
                          side_effect=[[], [], [later], [], []]):
            batches = list(app._iter_paid_order_pages(page, "B0TEST123"))

        self.assertEqual(batches[2], [later])
        self.assertEqual(page.goto.call_count, 4)

    def test_uses_actual_page_size_when_switch_to_100_fails(self):
        import amazon_bulk_send as app

        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/orders-v3/search?q=B0TEST123&qt=asin"
        page.locator.return_value.first.input_value.return_value = "15"
        first = app.OrderInfo(order_id="111-1111111-1111111", page_num=1)
        second = app.OrderInfo(order_id="222-2222222-2222222", page_num=2)
        with patch.object(app, "_ensure_100_per_page", return_value=False), \
             patch.object(app, "_get_total_orders", return_value=30), \
             patch.object(app, "random_delay"), \
             patch.object(app, "_wait_orders_table_ready"), \
             patch.object(app, "_extract_orders_from_current_page",
                          side_effect=[[first], [second]]):
            batches = list(app._iter_paid_order_pages(page, "B0TEST123"))

        self.assertEqual(batches, [[first], [second]])
        page.goto.assert_called_once()

    def test_fully_processed_page_skips_send_and_advances(self):
        from contextlib import ExitStack
        import amazon_bulk_send as app

        old = app.OrderInfo(order_id="111-1111111-1111111", page_num=1)
        disabled = app.OrderInfo(order_id="333-3333333-3333333", page_num=1)
        new = app.OrderInfo(order_id="222-2222222-2222222", page_num=2)
        page = MagicMock()
        page.url = "https://sellercentral.amazon.co.uk/orders-v3/search?q=B0TEST123&qt=asin"
        playwright, browser, context = MagicMock(), MagicMock(), MagicMock()
        mocked_results = {
            "open_store_via_api": 9222,
            "connect_to_store_cdp": (playwright, browser, context, page),
            "_poll_login": True,
            "load_processed_orders": {old.order_id},
            "load_disabled_orders": {disabled.order_id},
            "load_unconfirmed_orders": set(),
            "navigate_to_orders": True,
            "_ensure_logged_in": True,
            "_build_date_range_param": "",
            "search_asin": True,
            "_iter_paid_order_pages": iter(([old, disabled], [new])),
            "_goto_order_page": True,
            "send_email_with_pdf": True,
            "verify_and_follow_up": True,
            "export_failed_records": "",
            "save_run_report": Path("ignored.json"),
            "sync_saved_report": (0, 0),
        }
        with ExitStack() as stack:
            mocks = {
                name: stack.enter_context(patch.object(app, name, return_value=result))
                for name, result in mocked_results.items()
            }
            for name in ("random_delay", "save_processed_order", "show_failure_summary"):
                mocks[name] = stack.enter_context(patch.object(app, name))
            sequence = []
            mocks["sync_saved_report"].side_effect = lambda _path, **_kwargs: (sequence.append("sync"), (0, 0))[1]
            mocks["show_failure_summary"].side_effect = lambda *_args, **_kwargs: sequence.append("summary")
            status = app.StatusHolder()
            app.run_task(app.TaskConfig(store_name="TEST", asins=["B0TEST123"]),
                         status_holder=status)

        self.assertEqual(mocks["_goto_order_page"].call_count, 1)
        self.assertEqual(mocks["send_email_with_pdf"].call_count, 1)
        self.assertEqual(mocks["send_email_with_pdf"].call_args.args[1], new)
        mocks["save_processed_order"].assert_called_once_with(new.order_id, "TEST")
        self.assertEqual(status.status.success_count, 1)
        self.assertEqual(sequence, ["sync", "summary"])

    def test_disabled_order_is_recorded_then_skipped_on_next_page(self):
        import amazon_bulk_send as app

        order = app.OrderInfo(order_id="111-1111111-1111111", asin="B0TEST123")
        page = MagicMock()
        page.url = "https://sellercentral.amazon.co.uk/orders-v3/search"
        send = MagicMock(return_value="disabled")
        save_disabled = MagicMock(return_value=True)
        verify = MagicMock()
        export = MagicMock(return_value="report.csv")
        with patch.multiple(
            app,
            open_store_via_api=MagicMock(return_value=9222),
            connect_to_store_cdp=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock(), page)),
            _poll_login=MagicMock(return_value=True),
            load_processed_orders=MagicMock(return_value=set()),
            load_disabled_orders=MagicMock(return_value=set()),
            load_unconfirmed_orders=MagicMock(return_value=set()),
            navigate_to_orders=MagicMock(return_value=True),
            _ensure_logged_in=MagicMock(return_value=True),
            _build_date_range_param=MagicMock(return_value=""),
            search_asin=MagicMock(return_value=True),
            _iter_paid_order_pages=MagicMock(return_value=iter([[order], [order]])),
            _order_row_on_current_page=MagicMock(return_value=True),
            send_email_with_pdf=send,
            save_disabled_order=save_disabled,
            verify_and_follow_up=verify,
            export_failed_records=export,
            show_failure_summary=MagicMock(),
            save_run_report=MagicMock(return_value=Path("ignored.json")),
            sync_saved_report=MagicMock(return_value=(0, 0)),
        ):
            app.run_task(app.TaskConfig(store_name="TEST", asins=["B0TEST123"]))

        send.assert_called_once()
        save_disabled.assert_called_once_with(order.order_id, "TEST")
        verify.assert_not_called()
        self.assertEqual(export.call_args.args[0][0].reason, "联系主题已被 Amazon 禁用")

    def test_previously_disabled_order_is_skipped_without_opening_contact(self):
        import amazon_bulk_send as app

        order = app.OrderInfo(order_id="111-1111111-1111111", asin="B0TEST123")
        page = MagicMock()
        page.url = "https://sellercentral.amazon.co.uk/orders-v3/search"
        send = MagicMock()
        with patch.multiple(
            app,
            open_store_via_api=MagicMock(return_value=9222),
            connect_to_store_cdp=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock(), page)),
            _poll_login=MagicMock(return_value=True),
            load_processed_orders=MagicMock(return_value=set()),
            load_disabled_orders=MagicMock(return_value={order.order_id}),
            load_unconfirmed_orders=MagicMock(return_value=set()),
            navigate_to_orders=MagicMock(return_value=True),
            _ensure_logged_in=MagicMock(return_value=True),
            _build_date_range_param=MagicMock(return_value=""),
            search_asin=MagicMock(return_value=True),
            _iter_paid_order_pages=MagicMock(return_value=iter([[order]])),
            send_email_with_pdf=send,
            export_failed_records=MagicMock(return_value=""),
            show_failure_summary=MagicMock(),
            save_run_report=MagicMock(return_value=Path("ignored.json")),
            sync_saved_report=MagicMock(return_value=(0, 0)),
        ):
            app.run_task(app.TaskConfig(store_name="TEST", asins=["B0TEST123"]))

        send.assert_not_called()

    def test_previously_unconfirmed_order_is_skipped_without_opening_contact(self):
        import amazon_bulk_send as app

        order = app.OrderInfo(order_id="111-1111111-1111111", asin="B0TEST123")
        page = MagicMock()
        page.url = "https://sellercentral.amazon.co.uk/orders-v3/search"
        send = MagicMock()
        with patch.multiple(
            app,
            open_store_via_api=MagicMock(return_value=9222),
            connect_to_store_cdp=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock(), page)),
            _poll_login=MagicMock(return_value=True),
            load_processed_orders=MagicMock(return_value=set()),
            load_disabled_orders=MagicMock(return_value=set()),
            load_unconfirmed_orders=MagicMock(return_value={order.order_id}),
            navigate_to_orders=MagicMock(return_value=True),
            _ensure_logged_in=MagicMock(return_value=True),
            _build_date_range_param=MagicMock(return_value=""),
            search_asin=MagicMock(return_value=True),
            _iter_paid_order_pages=MagicMock(return_value=iter([[order]])),
            send_email_with_pdf=send,
            export_failed_records=MagicMock(return_value=""),
            show_failure_summary=MagicMock(),
            save_run_report=MagicMock(return_value=Path("ignored.json")),
            sync_saved_report=MagicMock(return_value=(0, 0)),
        ):
            app.run_task(app.TaskConfig(store_name="TEST", asins=["B0TEST123"]))

        send.assert_not_called()

    def test_missing_follow_up_record_skips_order_on_next_run(self):
        import amazon_bulk_send as app

        order = app.OrderInfo(order_id="444-4444444-4444444", asin="B0TEST01")
        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/orders-v3/search"
        send = MagicMock()
        summary = MagicMock()
        with patch.multiple(
            app,
            open_store_via_api=MagicMock(return_value=9222),
            connect_to_store_cdp=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock(), page)),
            _poll_login=MagicMock(return_value=True),
            load_processed_orders=MagicMock(return_value=set()),
            load_disabled_orders=MagicMock(return_value=set()),
            load_unconfirmed_orders=MagicMock(return_value=set()),
            load_followup_not_sent_orders=MagicMock(return_value={order.order_id}),
            navigate_to_orders=MagicMock(return_value=True),
            _ensure_logged_in=MagicMock(return_value=True),
            _build_date_range_param=MagicMock(return_value=""),
            search_asin=MagicMock(return_value=True),
            _iter_paid_order_pages=MagicMock(return_value=iter([[order]])),
            send_email_with_pdf=send,
            export_failed_records=MagicMock(return_value=""),
            show_failure_summary=summary,
            save_run_report=MagicMock(return_value=Path("ignored.json")),
            sync_saved_report=MagicMock(return_value=(0, 0)),
        ):
            app.run_task(app.TaskConfig(store_name="TEST", asins=["B0TEST01"]))

        send.assert_not_called()
        self.assertEqual(summary.call_args.kwargs["followup_not_sent_ids"], [order.order_id])

    def test_unreadable_processed_history_stops_instead_of_sending_again(self):
        import amazon_bulk_send as app

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, "OUTPUT_DIR", Path(directory)):
            (Path(directory) / "processed_orders_TEST.json").write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "已发送订单记录"):
                app.load_processed_orders("TEST")

    def test_clicked_but_unconfirmed_email_is_not_retried(self):
        import amazon_bulk_send as app

        orders_page = MagicMock()
        contact_page = MagicMock()
        contact_page.url = "https://sellercentral.amazon.com/messaging/contact"
        order = app.OrderInfo(order_id="123-4567890-1234567")
        with patch.object(app, "_click_buyer_name", return_value=contact_page), \
             patch.object(app, "_safe_wait_page"), \
             patch.object(app, "_is_invalid_contact_page", return_value=False), \
             patch.object(app, "_wait_contact_page_recover", return_value=True), \
             patch.object(app, "_safe_select_radio", return_value=True), \
             patch.object(app, "_upload_pdf_safe", return_value=True), \
             patch.object(app, "_has_loading_overlay", return_value=False), \
             patch.object(app, "_click_send_button", return_value=True) as click_send, \
             patch.object(app, "_verify_send_success", return_value=False), \
             patch.object(app, "_is_page_navigated_away", return_value=True), \
             patch.object(app, "random_delay"):
            result = app.send_email_with_pdf(orders_page, order, "manual.pdf", test_mode=False)

        self.assertEqual(result, "unconfirmed")
        click_send.assert_called_once()

    def test_overlay_recovery_does_not_click_send_again(self):
        import amazon_bulk_send as app

        page = MagicMock()
        page.evaluate.return_value = {"success": True, "reason": "text:已发送"}
        with patch.object(app, "_check_stop_global"), \
             patch.object(app, "_wait_if_paused_global"), \
             patch.object(app, "_has_loading_overlay", return_value=True), \
             patch.object(app, "_wait_contact_page_recover", return_value=True), \
             patch.object(app, "_click_send_button") as click_send, \
             patch.object(app.time, "sleep"):
            self.assertTrue(app._verify_send_success(page, app.OrderInfo(), timeout=1))

        click_send.assert_not_called()

    def test_unconfirmed_email_is_recorded_and_next_order_continues(self):
        import amazon_bulk_send as app

        orders = [app.OrderInfo(order_id=f"123-4567890-123456{i}", asin="B0TEST01")
                  for i in (1, 2)]
        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/orders-v3/search"
        send = MagicMock(side_effect=["unconfirmed", True])
        save = MagicMock(return_value=True)
        save_unconfirmed = MagicMock(return_value=True)
        verify = MagicMock(return_value=True)
        saved_report = MagicMock(return_value=Path("ignored.json"))
        with patch.multiple(
            app,
            open_store_via_api=MagicMock(return_value=9222),
            connect_to_store_cdp=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock(), page)),
            _poll_login=MagicMock(return_value=True),
            load_processed_orders=MagicMock(return_value=set()),
            load_disabled_orders=MagicMock(return_value=set()),
            load_unconfirmed_orders=MagicMock(return_value=set()),
            navigate_to_orders=MagicMock(return_value=True),
            _ensure_logged_in=MagicMock(return_value=True),
            _build_date_range_param=MagicMock(return_value=""),
            search_asin=MagicMock(return_value=True),
            _iter_paid_order_pages=MagicMock(return_value=iter([orders])),
            _order_row_on_current_page=MagicMock(return_value=True),
            send_email_with_pdf=send,
            save_processed_order=save,
            save_unconfirmed_order=save_unconfirmed,
            verify_and_follow_up=verify,
            export_failed_records=MagicMock(return_value="report.csv"),
            show_failure_summary=MagicMock(),
            save_run_report=saved_report,
            sync_saved_report=MagicMock(return_value=(0, 0)),
        ):
            holder = app.StatusHolder()
            app.run_task(app.TaskConfig(store_name="TEST", asins=["B0TEST01"]),
                         status_holder=holder)

        self.assertEqual(send.call_count, 2)
        save_unconfirmed.assert_called_once_with(orders[0].order_id, "TEST")
        save.assert_called_once_with(orders[1].order_id, "TEST")
        verify.assert_called_once()
        self.assertEqual(verify.call_args.args[1], orders[1])
        self.assertEqual(holder.status.state, app.RunState.FINISHED)
        item = saved_report.call_args.args[0]["items"][0]
        self.assertEqual((item["orders"], item["success"], item["failed"]), (2, 1, 1))
        self.assertEqual(item["remarks"], ["PDF 邮件发送未确认，需人工核对"])

    def test_failed_history_save_stops_before_next_order(self):
        import amazon_bulk_send as app

        orders = [app.OrderInfo(order_id=f"123-4567890-123456{i}", asin="B0TEST01")
                  for i in (1, 2)]
        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/orders-v3/search"
        send = MagicMock(return_value=True)
        verify = MagicMock()
        with patch.multiple(
            app,
            open_store_via_api=MagicMock(return_value=9222),
            connect_to_store_cdp=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock(), page)),
            _poll_login=MagicMock(return_value=True),
            load_processed_orders=MagicMock(return_value=set()),
            load_disabled_orders=MagicMock(return_value=set()),
            load_unconfirmed_orders=MagicMock(return_value=set()),
            navigate_to_orders=MagicMock(return_value=True),
            _ensure_logged_in=MagicMock(return_value=True),
            _build_date_range_param=MagicMock(return_value=""),
            search_asin=MagicMock(return_value=True),
            _iter_paid_order_pages=MagicMock(return_value=iter([orders])),
            _order_row_on_current_page=MagicMock(return_value=True),
            send_email_with_pdf=send,
            save_processed_order=MagicMock(return_value=False),
            verify_and_follow_up=verify,
            export_failed_records=MagicMock(return_value="report.csv"),
            show_failure_summary=MagicMock(),
            save_run_report=MagicMock(return_value=Path("ignored.json")),
            sync_saved_report=MagicMock(return_value=(0, 0)),
        ):
            holder = app.StatusHolder()
            app.run_task(app.TaskConfig(store_name="TEST", asins=["B0TEST01"]),
                         status_holder=holder)

        send.assert_called_once()
        verify.assert_not_called()
        self.assertEqual(holder.status.state, app.RunState.ERROR)


# ============================================================================
# 测试买家联系页跳转恢复
# ============================================================================

class TestBuyerNavigationFallback(unittest.TestCase):
    """管理订单页买家链接的有界点击、刷新和直开 URL 恢复。"""

    def setUp(self):
        import amazon_bulk_send as app
        self.app = app
        self.order = app.OrderInfo(
            order_id="123-4567890-1234567",
            buyer_name="测试买家",
            asin="B0TEST01",
        )
        self.page = MagicMock(name="orders_page")
        self.page.url = "https://sellercentral.amazon.co.uk/orders-v3"
        self.page.context.pages = [self.page]

    def test_delayed_popup_is_observed_by_navigation_loop(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content("""
                    <button onclick="setTimeout(() => window.open('about:blank'), 300)">
                      联系买家
                    </button>
                """)
                before = list(page.context.pages)
                source_url = page.url
                page.get_by_role("button").click()
                result = self.app._wait_for_buyer_navigation(
                    page, before, source_url, timeout=2.0, poll_interval=0.1)
                self.assertIsNotNone(result)
                self.assertEqual(result[1], "new_page")
            finally:
                browser.close()

    def _configure_row_locators(self, primary=None, fallback_candidates=()):
        """构造接近 Playwright 惰性 Locator 链的测试替身。"""
        all_rows = MagicMock(name="all_rows")
        matching_rows = MagicMock(name="matching_rows")
        row = MagicMock(name="matching_order_row")
        primary_group = MagicMock(name="primary_buyer_links")
        fallback_group = MagicMock(name="no_href_links")

        self.page.locator.return_value = all_rows
        all_rows.filter.return_value = matching_rows
        matching_rows.count.return_value = 1
        matching_rows.first = row

        primary_group.count.return_value = 1 if primary is not None else 0
        if primary is not None:
            primary_group.first = primary
        fallback_candidates = list(fallback_candidates)
        fallback_group.count.return_value = len(fallback_candidates)
        fallback_group.nth.side_effect = fallback_candidates
        row.locator.side_effect = lambda selector: (
            primary_group
            if selector == 'a[data-test-id="buyer-name-with-link"]'
            else fallback_group
        )
        return all_rows, matching_rows

    def test_first_click_success_returns_contact_page(self):
        contact_page = MagicMock(name="contact_page")
        with patch.object(
            self.app, "_click_buyer_link_once", return_value=contact_page
        ) as click_once:
            result = self.app._click_buyer_name(self.page, self.order)

        self.assertIs(result, contact_page)
        click_once.assert_called_once_with(self.page, self.order)
        self.page.reload.assert_not_called()

    def test_failed_buyer_click_does_not_open_or_retry_a_tab(self):
        with patch.object(self.app, "_click_buyer_link_once", return_value=None) as click_once, \
             patch.object(self.app, "_wait_for_buyer_navigation", return_value=None):
            result = self.app._click_buyer_name(self.page, self.order)

        self.assertIsNone(result)
        click_once.assert_called_once_with(self.page, self.order)
        self.page.reload.assert_not_called()
        self.page.context.new_page.assert_not_called()

    def test_late_contact_popup_is_used_without_second_click(self):
        contact_page = MagicMock(name="late_contact_page")
        contact_page.url = (
            "https://sellercentral.amazon.co.uk/messaging/contact"
            f"?orderID={self.order.order_id}")

        def first_click_times_out(*_args):
            self.page.context.pages.append(contact_page)
            return None

        with patch.object(
            self.app, "_click_buyer_link_once", side_effect=first_click_times_out
        ) as click_once, patch.object(
            self.app, "_wait_and_validate_contact_page", return_value=contact_page
        ):
            result = self.app._click_buyer_name(self.page, self.order)

        self.assertIs(result, contact_page)
        click_once.assert_called_once_with(self.page, self.order)
        self.page.reload.assert_not_called()

    def test_non_orders_page_does_not_create_contact_tab(self):
        self.page.url = "https://sellercentral.amazon.co.uk/messaging/inbox"
        with patch.object(self.app, "_click_buyer_link_once") as click_once:
            result = self.app._click_buyer_name(self.page, self.order)

        self.assertIsNone(result)
        click_once.assert_not_called()
        self.page.context.new_page.assert_not_called()

    def test_locator_resolution_prefers_data_test_id(self):
        primary = MagicMock(name="primary_buyer_link")
        primary.inner_text.return_value = "测试买家"
        all_rows, _ = self._configure_row_locators(primary=primary)

        target, info = self.app._locate_buyer_link(self.page, self.order)

        self.assertIs(target, primary)
        self.assertEqual(info["kind"], "buyer-name-with-link")
        all_rows.filter.assert_called_once_with(has_text=self.order.order_id)
        self.page.evaluate.assert_not_called()

    def test_no_href_fallback_requires_exact_buyer_name(self):
        wrong = MagicMock(name="wrong_no_href_link")
        wrong.inner_text.return_value = "其他动作"
        matching = MagicMock(name="matching_no_href_link")
        matching.inner_text.return_value = self.order.buyer_name
        self._configure_row_locators(
            primary=None,
            fallback_candidates=(wrong, matching),
        )

        target, info = self.app._locate_buyer_link(self.page, self.order)

        self.assertIs(target, matching)
        self.assertEqual(info["kind"], "no-href-link")
        self.page.evaluate.assert_not_called()

    def test_inert_href_fallback_preserves_legacy_link_variants(self):
        matching = MagicMock(name="matching_inert_link")
        matching.inner_text.return_value = self.order.buyer_name
        _, matching_rows = self._configure_row_locators(
            primary=None,
            fallback_candidates=(matching,),
        )

        target, _ = self.app._locate_buyer_link(self.page, self.order)

        self.assertIs(target, matching)
        matching_rows.first.locator.assert_any_call(
            'a:not([href]), a[href=""], a[href="#"], '
            'a[href^="javascript:"]'
        )

    def test_no_href_fallback_rejects_different_buyer_name(self):
        wrong = MagicMock(name="wrong_no_href_link")
        wrong.inner_text.return_value = "其他买家"
        self._configure_row_locators(
            primary=None,
            fallback_candidates=(wrong,),
        )

        target, info = self.app._locate_buyer_link(self.page, self.order)

        self.assertIsNone(target)
        self.assertEqual(info["reason"], "buyer_name_mismatch")

    def test_real_locator_click_captures_new_page_without_javascript(self):
        locator = MagicMock(name="fresh_buyer_locator")
        contact_page = MagicMock(name="contact_page")
        contact_page.url = (
            "https://sellercentral.amazon.co.uk/messaging/contact"
            f"?orderID={self.order.order_id}")

        def open_contact_page(*_args, **_kwargs):
            self.page.context.pages.append(contact_page)

        locator.click.side_effect = open_contact_page
        with patch.object(
            self.app, "_locate_buyer_link",
            return_value=(locator, {
                "found": True,
                "kind": "buyer-name-with-link",
                "text": self.order.buyer_name,
            }),
        ), patch.object(self.app, "_check_stop_global"), patch.object(
            self.app, "_wait_if_paused_global"
        ):
            result = self.app._click_buyer_link_once(self.page, self.order)

        self.assertIs(result, contact_page)
        locator.click.assert_called_once_with(timeout=10000)
        self.page.evaluate.assert_not_called()
        self.page.context.expect_page.assert_not_called()

    def test_new_page_contact_html_redirect_is_waited_and_validated(self):
        locator = MagicMock(name="fresh_buyer_locator")
        contact_page = MagicMock(name="redirecting_contact_page")
        contact_page.url = (
            "https://sellercentral.amazon.co.uk/gp/help/contact/contact.html"
            f"?orderID={self.order.order_id}")

        def open_contact_page(*_args, **_kwargs):
            self.page.context.pages.append(contact_page)

        control_checks = 0

        def finish_redirect():
            nonlocal control_checks
            control_checks += 1
            # click 前一次、导航检测一次；统一联系页校验开始后完成重定向。
            if control_checks >= 3:
                contact_page.url = (
                    "https://sellercentral.amazon.co.uk/messaging/contact"
                    f"?orderID={self.order.order_id}")

        locator.click.side_effect = open_contact_page
        with patch.object(
            self.app, "_locate_buyer_link",
            return_value=(locator, {
                "found": True,
                "kind": "buyer-name-with-link",
                "text": self.order.buyer_name,
            }),
        ), patch.object(self.app, "_check_stop_global"), patch.object(
            self.app, "_wait_if_paused_global", side_effect=finish_redirect
        ), patch.object(self.app.time, "sleep"):
            result = self.app._click_buyer_link_once(self.page, self.order)

        self.assertIs(result, contact_page)
        locator.click.assert_called_once_with(timeout=10000)
        self.assertGreaterEqual(control_checks, 3)

    def test_navigation_prefers_contact_page_over_unrelated_new_popup(self):
        unrelated = MagicMock(name="unrelated_popup")
        unrelated.url = "about:blank"
        contact_page = MagicMock(name="contact_page")
        contact_page.url = (
            "https://sellercentral.amazon.co.uk/messaging/contact"
            f"?orderID={self.order.order_id}")
        self.page.context.pages = [self.page, unrelated, contact_page]

        with patch.object(self.app, "_check_stop_global"), patch.object(
            self.app, "_wait_if_paused_global"
        ):
            navigation = self.app._wait_for_buyer_navigation(
                self.page,
                [self.page],
                self.page.url,
                timeout=0.0,
            )

        self.assertEqual(navigation, (contact_page, "new_page"))

    def test_navigation_does_not_select_unresolved_popup_too_early(self):
        unrelated = MagicMock(name="unrelated_popup")
        unrelated.url = "about:blank"
        contact_page = MagicMock(name="delayed_contact_page")
        contact_page.url = (
            "https://sellercentral.amazon.co.uk/messaging/contact"
            f"?orderID={self.order.order_id}")
        self.page.context.pages = [self.page, unrelated]
        control_checks = 0

        def reveal_contact_page():
            nonlocal control_checks
            control_checks += 1
            if control_checks == 2:
                self.page.context.pages.append(contact_page)

        with patch.object(self.app, "_check_stop_global"), patch.object(
            self.app, "_wait_if_paused_global", side_effect=reveal_contact_page
        ):
            navigation = self.app._wait_for_buyer_navigation(
                self.page,
                [self.page],
                self.page.url,
                timeout=0.5,
                poll_interval=0.01,
            )

        self.assertEqual(navigation, (contact_page, "new_page"))
        self.assertEqual(control_checks, 2)

    def test_real_locator_click_captures_same_page_navigation(self):
        locator = MagicMock(name="fresh_buyer_locator")

        def navigate_in_source_page(*_args, **_kwargs):
            self.page.url = (
                "https://sellercentral.amazon.co.uk/messaging/contact"
                f"?orderID={self.order.order_id}")

        locator.click.side_effect = navigate_in_source_page
        with patch.object(
            self.app, "_locate_buyer_link",
            return_value=(locator, {
                "found": True,
                "kind": "buyer-name-with-link",
                "text": self.order.buyer_name,
            }),
        ), patch.object(self.app, "_check_stop_global"), patch.object(
            self.app, "_wait_if_paused_global"
        ):
            result = self.app._click_buyer_link_once(self.page, self.order)

        self.assertIs(result, self.page)
        locator.click.assert_called_once_with(timeout=10000)
        self.page.context.expect_page.assert_not_called()

    def test_dispatched_click_without_navigation_fails_within_bound(self):
        locator = MagicMock(name="fresh_buyer_locator")
        with patch.object(
            self.app, "_locate_buyer_link",
            return_value=(locator, {
                "found": True,
                "kind": "buyer-name-with-link",
                "text": self.order.buyer_name,
            }),
        ), patch.object(self.app, "_check_stop_global"), patch.object(
            self.app, "_wait_if_paused_global"
        ), patch.object(self.app.time, "sleep") as sleep:
            result = self.app._click_buyer_link_once(
                self.page,
                self.order,
                navigation_timeout=0.0,
            )

        self.assertIsNone(result)
        locator.click.assert_called_once_with(timeout=10000)
        sleep.assert_not_called()
        self.page.evaluate.assert_not_called()
        self.page.context.expect_page.assert_not_called()

    def test_missing_row_does_not_click_or_wait_for_navigation(self):
        with patch.object(
            self.app, "_locate_buyer_link",
            return_value=(None, {"found": False, "reason": "row_missing"}),
        ), patch.object(
            self.app, "_wait_for_buyer_navigation"
        ) as navigation_wait:
            result = self.app._click_buyer_link_once(self.page, self.order)

        self.assertIsNone(result)
        navigation_wait.assert_not_called()
        self.page.context.expect_page.assert_not_called()

    def test_target_wait_rechecks_and_responds_to_controls(self):
        ready_locator = MagicMock(name="refreshed_buyer_locator")
        with patch.object(
            self.app, "_locate_buyer_link",
            side_effect=[
                (None, {"found": False, "reason": "row_missing"}),
                (ready_locator, {
                    "found": True,
                    "kind": "buyer-name-with-link",
                    "text": "测试买家",
                }),
            ],
        ), patch.object(
            self.app, "_check_stop_global"
        ) as check_stop, patch.object(
            self.app, "_wait_if_paused_global"
        ) as wait_pause, patch.object(self.app.time, "sleep"):
            ready = self.app._wait_for_target_buyer_link(
                self.page, self.order, timeout=2.0,
                poll_interval=0.01)

        self.assertTrue(ready)
        self.assertEqual(check_stop.call_count, 2)
        self.assertEqual(wait_pause.call_count, 2)
        self.page.evaluate.assert_called_once()  # 首轮滚到底触发下一波懒加载

    def test_target_wait_finishes_when_row_exists_without_link(self):
        with patch.object(
            self.app, "_locate_buyer_link",
            return_value=(None, {
                "found": False,
                "reason": "link_missing",
            }),
        ), patch.object(self.app, "_check_stop_global"), patch.object(
            self.app, "_wait_if_paused_global"
        ):
            ready = self.app._wait_for_target_buyer_link(
                self.page,
                self.order,
                timeout=2.0,
            )

        self.assertTrue(ready)
        self.page.evaluate.assert_not_called()

    def test_contact_page_order_id_mismatch_is_rejected(self):
        contact_page = MagicMock(name="wrong_contact_page")
        contact_page.url = (
            "https://sellercentral.amazon.co.uk/messaging/contact"
            "?orderID=000-0000000-0000000")
        with patch.object(self.app, "_check_stop_global"), patch.object(
            self.app, "_wait_if_paused_global"
        ):
            result = self.app._wait_and_validate_contact_page(
                contact_page,
                self.page,
                self.order,
            )

        self.assertIsNone(result)
        contact_page.close.assert_called_once_with()


class TestInvalidContactPage(unittest.TestCase):
    def test_invalid_link_skips_without_upload_or_send(self):
        import amazon_bulk_send as app

        orders_page = MagicMock(name="orders_page")
        contact_page = MagicMock(name="invalid_contact_page")
        contact_page.url = "https://sellercentral.amazon.com/messaging/contact"
        contact_page.get_by_text.return_value.count.return_value = 1
        order = app.OrderInfo(order_id="123-4567890-1234567", asin="B0TEST01")

        with patch.object(app, "_click_buyer_name", return_value=contact_page), \
             patch.object(app, "random_delay"), \
             patch.object(app, "_safe_wait_page"), \
             patch.object(app, "_wait_contact_page_recover") as recover, \
             patch.object(app, "_upload_pdf_safe") as upload, \
             patch.object(app, "_click_send_button") as send:
            result = app.send_email_with_pdf(orders_page, order, "manual.pdf")

        self.assertIsNone(result)
        contact_page.get_by_text.assert_called_once_with("无效链接。", exact=True)
        contact_page.close.assert_called_once_with()
        recover.assert_not_called()
        upload.assert_not_called()
        send.assert_not_called()


class TestContactRadioReadiness(unittest.TestCase):
    def test_disabled_reason_skips_without_refresh_or_new_tab(self):
        import amazon_bulk_send as app

        orders_page = MagicMock(name="orders_page")
        contact_page = MagicMock(name="contact_page")
        contact_page.url = "https://sellercentral.amazon.com/messaging/contact"
        order = app.OrderInfo(order_id="123-4567890-1234567")
        with patch.object(app, "_click_buyer_name", return_value=contact_page) as click_buyer, \
             patch.object(app, "_is_invalid_contact_page", return_value=False), \
             patch.object(app, "_wait_contact_page_recover", return_value=True), \
             patch.object(app, "_safe_select_radio", return_value="disabled"), \
             patch.object(app, "_upload_pdf_safe") as upload, \
             patch.object(app, "_safe_wait_page"), \
             patch.object(app, "random_delay"):
            result = app.send_email_with_pdf(orders_page, order, "manual.pdf")

        self.assertEqual(result, "disabled")
        click_buyer.assert_called_once_with(orders_page, order)
        contact_page.reload.assert_not_called()
        contact_page.context.new_page.assert_not_called()
        contact_page.close.assert_called_once_with()
        upload.assert_not_called()

    def test_disabled_invoice_radio_is_reported_as_disabled(self):
        import amazon_bulk_send as app

        page = MagicMock()
        invoice = MagicMock()
        invoice.count.return_value = 1
        invoice.is_enabled.return_value = False

        def target(selector):
            group = MagicMock()
            group.first = invoice if selector == 'input[value="sendInvoice"]' else MagicMock()
            if selector != 'input[value="sendInvoice"]':
                group.first.count.return_value = 0
            return group

        page.locator.side_effect = target
        with patch.object(app.time, "sleep"):
            result = app._safe_select_radio(page, app.OrderInfo())

        self.assertEqual(result, "disabled")
        invoice.click.assert_not_called()

    def test_missing_reason_refreshes_existing_contact_tab_once(self):
        import amazon_bulk_send as app

        orders_page = MagicMock(name="orders_page")
        contact_page = MagicMock(name="contact_page")
        contact_page.url = "https://sellercentral.amazon.com/messaging/contact"
        order = app.OrderInfo(order_id="123-4567890-1234567")
        with patch.object(app, "_click_buyer_name", return_value=contact_page) as click_buyer, \
             patch.object(app, "_is_invalid_contact_page", return_value=False), \
             patch.object(app, "_wait_contact_page_recover", return_value=True), \
             patch.object(app, "_safe_select_radio", side_effect=[None, "ok:sendInvoice"]) as select, \
             patch.object(app, "_upload_pdf_safe", return_value=False), \
             patch.object(app, "_has_loading_overlay", return_value=False), \
             patch.object(app, "_safe_wait_page"), \
             patch.object(app, "random_delay"):
            result = app.send_email_with_pdf(orders_page, order, "manual.pdf")

        self.assertFalse(result)
        contact_page.reload.assert_called_once_with(
            wait_until="domcontentloaded", timeout=30000)
        self.assertEqual(select.call_count, 2)
        click_buyer.assert_called_once_with(orders_page, order)
        contact_page.context.new_page.assert_not_called()

    def test_missing_reason_after_refresh_stops_without_new_tab(self):
        import amazon_bulk_send as app

        orders_page = MagicMock(name="orders_page")
        contact_page = MagicMock(name="contact_page")
        contact_page.url = "https://sellercentral.amazon.com/messaging/contact"
        order = app.OrderInfo(order_id="123-4567890-1234567")
        with patch.object(app, "_click_buyer_name", return_value=contact_page) as click_buyer, \
             patch.object(app, "_is_invalid_contact_page", return_value=False), \
             patch.object(app, "_wait_contact_page_recover", return_value=True), \
             patch.object(app, "_safe_select_radio", return_value=None) as select, \
             patch.object(app, "_upload_pdf_safe") as upload, \
             patch.object(app, "_safe_wait_page"), \
             patch.object(app, "random_delay"):
            result = app.send_email_with_pdf(orders_page, order, "manual.pdf")

        self.assertFalse(result)
        contact_page.reload.assert_called_once()
        self.assertEqual(select.call_count, 2)
        click_buyer.assert_called_once_with(orders_page, order)
        upload.assert_not_called()

    def test_disabled_result_survives_contact_tab_close_error(self):
        import amazon_bulk_send as app

        orders_page = MagicMock(name="orders_page")
        contact_page = MagicMock(name="contact_page")
        contact_page.url = "https://sellercentral.amazon.com/messaging/contact"
        contact_page.close.side_effect = RuntimeError("tab already closed")
        order = app.OrderInfo(order_id="123-4567890-1234567")
        with patch.object(app, "_click_buyer_name", return_value=contact_page), \
             patch.object(app, "_is_invalid_contact_page", return_value=False), \
             patch.object(app, "_wait_contact_page_recover", return_value=True), \
             patch.object(app, "_safe_select_radio", return_value="disabled"), \
             patch.object(app, "_safe_wait_page"), \
             patch.object(app, "random_delay"):
            result = app.send_email_with_pdf(orders_page, order, "manual.pdf")

        self.assertEqual(result, "disabled")
        contact_page.context.new_page.assert_not_called()
        contact_page.close.assert_called_once_with()

    def test_disabled_orders_have_separate_store_file_with_processed_structure(self):
        import amazon_bulk_send as app

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, "OUTPUT_DIR", Path(directory)):
            self.assertTrue(app.save_disabled_order("123-4567890-1234567", "EXAMPLE-STORE-US"))
            self.assertEqual(app.load_disabled_orders("EXAMPLE-STORE-US"),
                             {"123-4567890-1234567"})
            path = Path(directory) / "processed_disabled_orders_EXAMPLE-STORE-US.json"
            contents = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(contents), {"order_ids", "last_updated", "count"})
            self.assertEqual(contents["count"], 1)
            self.assertFalse((Path(directory) / "processed_orders_EXAMPLE-STORE-US.json").exists())

    def test_unconfirmed_orders_have_separate_store_file(self):
        import amazon_bulk_send as app

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, "OUTPUT_DIR", Path(directory)):
            self.assertTrue(app.save_unconfirmed_order("123-4567890-1234567", "EXAMPLE-STORE-UK"))
            self.assertEqual(app.load_unconfirmed_orders("EXAMPLE-STORE-UK"),
                             {"123-4567890-1234567"})
            path = Path(directory) / "processed_unconfirmed_orders_EXAMPLE-STORE-UK.json"
            contents = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(contents["order_ids"], ["123-4567890-1234567"])
            self.assertFalse((Path(directory) / "processed_orders_EXAMPLE-STORE-UK.json").exists())
            self.assertFalse((Path(directory) / "processed_disabled_orders_EXAMPLE-STORE-UK.json").exists())

    def test_followup_not_sent_orders_have_separate_store_file(self):
        import amazon_bulk_send as app

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, "OUTPUT_DIR", Path(directory)):
            self.assertTrue(app.save_followup_not_sent_order(
                "444-4444444-4444444", "EXAMPLE-STORE-US"))
            self.assertEqual(app.load_followup_not_sent_orders("EXAMPLE-STORE-US"),
                             {"444-4444444-4444444"})
            path = Path(directory) / "processed_followup_not_sent_orders_EXAMPLE-STORE-US.json"
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["order_ids"],
                             ["444-4444444-4444444"])
            self.assertFalse((Path(directory) / "processed_unconfirmed_orders_EXAMPLE-STORE-US.json").exists())

    def test_waits_for_target_radio_to_be_enabled(self):
        import amazon_bulk_send as app

        page = MagicMock()
        invoice = MagicMock()
        invoice.count.return_value = 1
        invoice.is_enabled.side_effect = [False, True]
        invoice.is_checked.return_value = True
        icon = invoice.locator.return_value.locator.return_value.first
        icon.count.return_value = 1

        def target(selector):
            group = MagicMock()
            group.first = invoice if selector == 'input[value="sendInvoice"]' else MagicMock()
            if selector != 'input[value="sendInvoice"]':
                group.first.count.return_value = 0
            return group

        page.locator.side_effect = target
        order = app.OrderInfo(order_id="123-4567890-1234567")
        with patch.object(app.time, "sleep") as sleep:
            result = app._safe_select_radio(page, order)

        self.assertEqual(result, "ok:sendInvoice")
        icon.click.assert_called_once()
        invoice.click.assert_not_called()
        invoice.is_checked.assert_called_once()
        sleep.assert_called_once_with(1)

    def test_blocked_click_does_not_retry_and_scroll_repeatedly(self):
        import amazon_bulk_send as app

        page = MagicMock()
        radio = page.locator.return_value.first
        radio.count.return_value = 1
        radio.is_enabled.return_value = True
        icon = radio.locator.return_value.locator.return_value.first
        icon.count.return_value = 1
        icon.click.side_effect = RuntimeError("pointer intercepted")

        with patch.object(app.time, "sleep") as sleep:
            result = app._safe_select_radio(page, app.OrderInfo())

        self.assertIsNone(result)
        icon.click.assert_called_once()
        sleep.assert_not_called()

    def test_invoice_radio_waits_for_delayed_checked_state(self):
        from playwright.sync_api import sync_playwright
        import amazon_bulk_send as app

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content("""
                    <kat-radiobutton></kat-radiobutton>
                    <script>
                      customElements.define('kat-radiobutton', class extends HTMLElement {
                        connectedCallback() {
                          const root = this.attachShadow({mode: 'open'});
                          root.innerHTML = `<div><input type="radio" value="sendInvoice"
                            aria-label="发送发票消息模板"><span class="kat-radiobutton-icon">选择</span></div>`;
                          root.querySelector('.kat-radiobutton-icon').onclick = () =>
                            setTimeout(() => root.querySelector('input').checked = true, 1200);
                        }
                      });
                    </script>
                """)
                result = app._safe_select_radio(page, app.OrderInfo())
                self.assertTrue(result)
                self.assertTrue(page.locator('input[value="sendInvoice"]').is_checked())
            finally:
                browser.close()


class TestSalesChannelSelection(unittest.TestCase):
    def test_empty_selection_allows_all_and_selected_channels_match_exactly(self):
        import amazon_bulk_send as app

        order = app.OrderInfo(sales_channel=" amazon.FR ")
        self.assertTrue(app._matches_sales_channel(order, []))
        self.assertTrue(app._matches_sales_channel(order, ["Amazon.de", "Amazon.fr"]))
        self.assertFalse(app._matches_sales_channel(order, ["Amazon.de"]))
        self.assertFalse(app._matches_sales_channel(
            app.OrderInfo(sales_channel="Amazon.com.mx"), ["Amazon.com"]))
        self.assertFalse(app._matches_sales_channel(
            app.OrderInfo(sales_channel=""), ["Amazon.fr"]))

    def test_mixed_channel_orders_only_send_to_selected_channels(self):
        import amazon_bulk_send as app

        channels = ["Amazon.de", "Amazon.ca", "Amazon.fr", ""]
        orders = [app.OrderInfo(order_id=f"123-4567890-123456{i}",
                                asin="B0TEST01", sales_channel=channel)
                  for i, channel in enumerate(channels, 1)]
        page = MagicMock()
        page.url = "https://sellercentral.amazon.de/orders-v3/search"
        holder = app.StatusHolder()
        config = app.TaskConfig(store_name="TEST", asins=["B0TEST01"],
                                sales_channels=["Amazon.de", "Amazon.fr"])
        send = MagicMock(return_value=True)
        save = MagicMock()
        with patch.multiple(
            app,
            open_store_via_api=MagicMock(return_value=9222),
            connect_to_store_cdp=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock(), page)),
            _poll_login=MagicMock(return_value=True),
            load_processed_orders=MagicMock(return_value=set()),
            load_disabled_orders=MagicMock(return_value=set()),
            load_unconfirmed_orders=MagicMock(return_value=set()),
            navigate_to_orders=MagicMock(return_value=True),
            _ensure_logged_in=MagicMock(return_value=True),
            _build_date_range_param=MagicMock(return_value=""),
            search_asin=MagicMock(return_value=True),
            _iter_paid_order_pages=MagicMock(return_value=iter([orders])),
            _order_row_on_current_page=MagicMock(return_value=True),
            send_email_with_pdf=send,
            save_processed_order=save,
            verify_and_follow_up=MagicMock(return_value=True),
            export_failed_records=MagicMock(return_value="report.csv"),
            show_failure_summary=MagicMock(),
            save_run_report=MagicMock(return_value=Path("ignored.json")),
            sync_saved_report=MagicMock(return_value=(0, 0)),
            switch_to_marketplace=MagicMock(return_value=True),
        ):
            app.run_task(config, status_holder=holder)

        self.assertEqual([call.args[1].order_id for call in send.call_args_list],
                         [orders[0].order_id, orders[2].order_id])
        self.assertEqual([call.args[0] for call in save.call_args_list],
                         [orders[0].order_id, orders[2].order_id])
        self.assertEqual(holder.status.state, app.RunState.FINISHED)


class TestFollowUpComposer(unittest.TestCase):
    def test_contact_buyer_prompt_takes_priority_over_hidden_textarea(self):
        from playwright.sync_api import sync_playwright
        import amazon_bulk_send as app

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('''
                    <div id="case-message-composer" class="case-message-view-composer">
                      <div class="editor"><textarea class="editor-textarea" hidden></textarea></div>
                    </div>
                    <div class="navigate-to-contact-buyer">
                      <span>使用“联系买家”页面上的任一模板为此案例发送消息</span>
                      <kat-button class="navigate-button" label="联系买家"></kat-button>
                    </div>
                ''')
                self.assertFalse(app._wait_conversation_open(page, timeout=1))
            finally:
                browser.close()

    def test_collapsed_editor_textarea_is_revealed_and_filled(self):
        from playwright.sync_api import sync_playwright
        import amazon_bulk_send as app

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content("""
                    <div id="case-message-composer" class="case-message-view-composer">
                      <div class="case-message-view-composer-textarea-container">
                        <div class="editor" tabindex="-1"
                             onclick="this.querySelector('textarea').style.display='block'">
                          <textarea class="editor-textarea fake-kat-textarea" style="display:none"></textarea>
                          <div class="editor-shadow" style="width:534px;height:38px"></div>
                        </div>
                      </div>
                    </div>
                """)
                self.assertTrue(app._wait_conversation_open(page, timeout=1))
                with patch.object(app, "random_delay"), \
                     patch.object(app, "_wait_messaging_loading_done"), \
                     patch.object(app, "_click_send_button", return_value=True), \
                     patch.object(app, "_wait_for_follow_up_confirmation", return_value=True):
                    self.assertTrue(app._send_follow_up_message(page, "Follow-up text"))
                self.assertEqual(page.locator(".editor-textarea").input_value(), "Follow-up text")
            finally:
                browser.close()

    def test_visible_conversation_textarea_outside_composer_is_ready(self):
        from playwright.sync_api import sync_playwright
        import amazon_bulk_send as app

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('<div class="case-message-view"><textarea id="reply"></textarea></div>')
                self.assertTrue(app._wait_conversation_open(page, timeout=1))
            finally:
                browser.close()

    def test_matching_result_reports_missing_composer_without_sending(self):
        import amazon_bulk_send as app

        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/messaging/inbox?cc=case-id"
        order = app.OrderInfo(order_id="555-5555555-5555555", sales_channel="Amazon.com")
        with patch.object(app, "random_delay") as delay, \
             patch.object(app, "_wait_page_ready"), \
             patch.object(app, "_wait_messaging_loading_done"), \
             patch.object(app, "_trigger_messaging_search"), \
             patch.object(app, "_wait_conversation_open", return_value=False) as wait_input, \
             patch.object(app, "safe_find", return_value=MagicMock()), \
             patch.object(app, "_send_follow_up_message", return_value=True) as send:
            result = app.verify_and_follow_up(
                page, order, "", follow_up_messages={"amazon.com": "Follow-up text"})

        self.assertEqual(result, "input_unavailable")
        self.assertEqual(page.reload.call_count, 2)
        self.assertEqual(wait_input.call_count, 3)
        self.assertEqual(sum(c.args == (5, 5) for c in delay.call_args_list), 2)
        send.assert_not_called()

    def test_composer_ready_after_second_refresh_sends_follow_up(self):
        import amazon_bulk_send as app

        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/messaging/inbox?cc=case-id"
        order = app.OrderInfo(order_id="555-5555555-5555555", sales_channel="Amazon.com")
        with patch.object(app, "random_delay") as delay, \
             patch.object(app, "_wait_page_ready"), \
             patch.object(app, "_wait_messaging_loading_done"), \
             patch.object(app, "_trigger_messaging_search"), \
             patch.object(app, "_wait_conversation_open", side_effect=[False, False, True]), \
             patch.object(app, "safe_find", return_value=MagicMock()), \
             patch.object(app, "_send_follow_up_message", return_value=True) as send:
            result = app.verify_and_follow_up(
                page, order, "", follow_up_messages={"amazon.com": "Follow-up text"})

        self.assertTrue(result)
        self.assertEqual(page.reload.call_count, 2)
        self.assertEqual(sum(c.args == (5, 5) for c in delay.call_args_list), 2)
        send.assert_called_once()

    def test_contact_buyer_prompt_appearing_after_refresh_is_clicked(self):
        import amazon_bulk_send as app

        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/messaging/inbox?cc=case-id"
        order = app.OrderInfo(order_id="444-4444444-4444444", sales_channel="Amazon.com")
        with patch.object(app, "random_delay"), \
             patch.object(app, "_wait_page_ready"), \
             patch.object(app, "_wait_messaging_loading_done"), \
             patch.object(app, "_trigger_messaging_search"), \
             patch.object(app, "_wait_conversation_open", side_effect=[False, False, True]), \
             patch.object(app, "_return_from_contact_buyer", side_effect=[False, True]) as contact, \
             patch.object(app, "safe_find", return_value=MagicMock()), \
             patch.object(app, "_send_follow_up_message", return_value=True) as send:
            result = app.verify_and_follow_up(
                page, order, "", follow_up_messages={"amazon.com": "Follow-up text"})

        self.assertTrue(result)
        self.assertEqual(contact.call_count, 2)
        page.reload.assert_called_once()
        send.assert_called_once()

    def test_contact_buyer_prompt_returns_to_conversation_before_follow_up(self):
        import amazon_bulk_send as app

        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/messaging/inbox?cc=case-id"
        page.context.pages = [page]
        page.locator.return_value.first.count.return_value = 1
        page.locator.return_value.first.is_visible.return_value = True
        order = app.OrderInfo(order_id="666-6666666-6666666", sales_channel="Amazon.com")
        with patch.object(app, "random_delay"), \
             patch.object(app, "_wait_page_ready"), \
             patch.object(app, "_wait_messaging_loading_done"), \
             patch.object(app, "_trigger_messaging_search"), \
             patch.object(app, "_wait_conversation_open", side_effect=[False, True]), \
             patch.object(app, "_wait_for_buyer_navigation", return_value=(page, "same_page")), \
             patch.object(app, "safe_find", return_value=MagicMock()), \
             patch.object(app, "_send_follow_up_message", return_value=True) as send:
            result = app.verify_and_follow_up(
                page, order, "", follow_up_messages={"amazon.com": "Follow-up text"})

        self.assertTrue(result)
        page.go_back.assert_called_once()
        page.reload.assert_not_called()
        send.assert_called_once()

    def test_contact_buyer_prompt_new_tab_is_closed_before_follow_up(self):
        import amazon_bulk_send as app

        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/messaging/inbox?cc=case-id"
        page.context.pages = [page]
        page.locator.return_value.first.count.return_value = 1
        page.locator.return_value.first.is_visible.return_value = True
        contact_page = MagicMock()
        contact_page.url = "https://sellercentral.amazon.com/messaging/contact?orderID=666-6666666-6666666"
        order = app.OrderInfo(order_id="666-6666666-6666666", sales_channel="Amazon.com")
        with patch.object(app, "random_delay"), \
             patch.object(app, "_wait_page_ready"), \
             patch.object(app, "_wait_messaging_loading_done"), \
             patch.object(app, "_trigger_messaging_search"), \
             patch.object(app, "_wait_conversation_open", side_effect=[False, True]), \
             patch.object(app, "_wait_for_buyer_navigation", return_value=(contact_page, "new_page")), \
             patch.object(app, "safe_find", return_value=MagicMock()), \
             patch.object(app, "_send_follow_up_message", return_value=True):
            result = app.verify_and_follow_up(
                page, order, "", follow_up_messages={"amazon.com": "Follow-up text"})

        self.assertTrue(result)
        contact_page.close.assert_called_once()
        page.reload.assert_called_once()

    def test_contact_buyer_prompt_selector_matches_given_markup(self):
        from playwright.sync_api import sync_playwright
        from amazon_selectors import get_selectors

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('''
                    <div class="navigate-to-contact-buyer">
                      <span class="navigate-to-contact-buyer-header">使用“联系买家”页面上的任一模板为此案例发送消息</span>
                      <kat-button class="navigate-button" label="联系买家" variant="secondary"></kat-button>
                    </div>
                ''')
                self.assertEqual(
                    page.locator(get_selectors("contact_buyer_navigation")[0]).count(), 1)
            finally:
                browser.close()

    def test_contact_buyer_waits_for_page_and_keeps_same_conversation_after_back(self):
        import amazon_bulk_send as app

        order_id = "444-4444444-4444444"
        source_url = f"https://sellercentral.amazon.com/messaging/inbox?cc=case-id&fi=search&ss={order_id}"
        page = MagicMock()
        page.url = source_url
        page.context.pages = [page]
        button = page.locator.return_value.first
        button.count.return_value = 1
        button.is_visible.return_value = True
        events = []

        def clicked(**_kwargs):
            page.url = f"https://sellercentral.amazon.com/messaging/contact?orderId={order_id}"
            events.append("click")

        def backed(**_kwargs):
            page.url = "https://sellercentral.amazon.com/messaging/inbox?fi=search&cc=case-id"
            events.append("back")

        button.click.side_effect = clicked
        page.go_back.side_effect = backed
        with patch.object(app, "_wait_for_buyer_navigation", return_value=(page, "same_page")), \
             patch.object(app, "_wait_page_ready", side_effect=lambda *_args, **_kwargs: events.append("ready")):
            self.assertTrue(app._return_from_contact_buyer(page, order_id))

        self.assertEqual(events[:3], ["click", "ready", "back"])
        page.goto.assert_not_called()

    def test_contact_buyer_dispatches_click_when_host_click_fails(self):
        import amazon_bulk_send as app

        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/messaging/inbox?cc=case-id"
        page.context.pages = [page]
        button = page.locator.return_value.first
        button.count.return_value = 1
        button.is_visible.return_value = True
        button.click.side_effect = ValueError("custom element has no bounding box")
        with patch.object(app, "_wait_for_buyer_navigation", return_value=(page, "same_page")), \
             patch.object(app, "_wait_page_ready"):
            self.assertTrue(app._return_from_contact_buyer(page, "444-4444444-4444444"))

        button.dispatch_event.assert_called_once_with("click")

    def test_contact_buyer_reselects_order_if_prompt_remains_after_back(self):
        import amazon_bulk_send as app

        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/messaging/inbox?cc=case-id"
        page.context.pages = [page]
        page.locator.return_value.first.count.return_value = 1
        page.locator.return_value.first.is_visible.return_value = True
        order = app.OrderInfo(order_id="444-4444444-4444444", sales_channel="Amazon.com")
        with patch.object(app, "random_delay"), \
             patch.object(app, "_wait_page_ready"), \
             patch.object(app, "_wait_messaging_loading_done"), \
             patch.object(app, "_trigger_messaging_search"), \
             patch.object(app, "_wait_conversation_open", side_effect=[False, False, True]), \
             patch.object(app, "_wait_for_buyer_navigation", return_value=(page, "same_page")), \
             patch.object(app, "safe_find", return_value=MagicMock()), \
             patch.object(app, "_send_follow_up_message", return_value=True) as send:
            result = app.verify_and_follow_up(
                page, order, "", follow_up_messages={"amazon.com": "Follow-up text"})

        self.assertTrue(result)
        self.assertEqual(page.locator.return_value.first.click.call_count, 3)
        page.reload.assert_not_called()
        send.assert_called_once()

    def test_contact_buyer_round_trip_reveals_composer_in_browser(self):
        from playwright.sync_api import sync_playwright
        import amazon_bulk_send as app

        order_id = "444-4444444-4444444"
        inbox_url = f"https://example.test/messaging/inbox?cc=case-id&fi=search&ss={order_id}"
        inbox = f'''
            <input placeholder="订单编号" />
            <a class="case-list-item" data-ph-capture-attribute-order-id="{order_id}"
               onclick="if (sessionStorage.getItem('contactVisited'))
                 document.querySelector('.case-message-view').innerHTML =
                   '<div id=case-message-composer class=case-message-view-composer><textarea></textarea></div>'">
               Invoice for Amazon order {order_id}
            </a>
            <div class="case-message-view">
              <div class="navigate-to-contact-buyer">
                <kat-button class="navigate-button" label="联系买家"
                  style="display:inline-block;width:120px;height:30px"
                  onclick="location.href='/messaging/contact?orderId={order_id}'"></kat-button>
              </div>
            </div>
        '''

        def serve(route):
            if "/messaging/contact" in route.request.url:
                route.fulfill(content_type="text/html; charset=utf-8", body=(
                    "<input type='radio'>"
                    "<script>sessionStorage.setItem('contactVisited','1')</script>"))
            else:
                route.fulfill(content_type="text/html; charset=utf-8", body=inbox)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route("https://example.test/**", serve)
                with patch.object(app, "_get_messages_url", return_value=inbox_url), \
                     patch.object(app, "random_delay"), \
                     patch.object(app, "_send_follow_up_message", return_value=True) as send:
                    result = app.verify_and_follow_up(
                        page, app.OrderInfo(order_id=order_id, sales_channel="Amazon.com"),
                        "", follow_up_messages={"amazon.com": "Follow-up text"})
                self.assertTrue(result)
                self.assertEqual(page.locator("#case-message-composer textarea").count(), 1)
                send.assert_called_once()
            finally:
                browser.close()

    def test_matching_result_opens_when_native_click_fails(self):
        from playwright.sync_api import sync_playwright
        import amazon_bulk_send as app

        order_id = "555-5555555-5555555"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(f"""
                    <input placeholder="订单编号" />
                    <a class="case-list-item" data-ph-capture-attribute-order-id="{order_id}"
                       onclick="document.querySelector('.case-message-view').innerHTML='<textarea></textarea>'">
                       Invoice for Amazon order {order_id}
                    </a>
                    <div class="case-message-view"></div>
                """)
                with patch.object(page, "goto"), \
                     patch.object(app, "_wait_page_ready"), \
                     patch.object(app, "random_delay"), \
                     patch.object(app.Locator, "click", side_effect=RuntimeError("stale locator")):
                    result = app.verify_and_follow_up(page, app.OrderInfo(order_id=order_id), "")
                self.assertTrue(result)
                self.assertEqual(page.locator(".case-message-view textarea").count(), 1)
            finally:
                browser.close()

    def test_unconfirmed_follow_up_is_returned_to_order_workflow(self):
        import amazon_bulk_send as app

        page = MagicMock()
        order = app.OrderInfo(order_id="123-4567890-1234567", sales_channel="Amazon.com")
        with patch.object(app, "random_delay"), \
             patch.object(app, "_wait_page_ready"), \
             patch.object(app, "_wait_messaging_loading_done"), \
             patch.object(app, "_trigger_messaging_search"), \
             patch.object(app, "_wait_conversation_open", return_value=True), \
             patch.object(app, "safe_find", return_value=MagicMock()), \
             patch.object(app, "_send_follow_up_message", return_value=False) as send:
            result = app.verify_and_follow_up(
                page, order, "", follow_up_messages={"amazon.com": "Follow-up text"})

        self.assertIsNone(result)
        send.assert_called_once()

    def test_unconfirmed_follow_up_continues_and_reports_order(self):
        import amazon_bulk_send as app

        orders = [app.OrderInfo(order_id=f"123-4567890-123456{i}", asin="B0TEST01")
                  for i in (1, 2)]
        page = MagicMock()
        page.url = "https://sellercentral.amazon.co.uk/orders-v3/search"
        holder = app.StatusHolder()
        config = app.TaskConfig(store_name="TEST", asins=["B0TEST01"], dry_run=False)
        send = MagicMock(return_value=True)
        verify = MagicMock(side_effect=[None, True])
        export = MagicMock(return_value="report.csv")
        save_report = MagicMock(return_value=Path("ignored.json"))
        save_missing_followup = MagicMock()
        with patch.multiple(
            app,
            open_store_via_api=MagicMock(return_value=9222),
            connect_to_store_cdp=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock(), page)),
            _poll_login=MagicMock(return_value=True),
            load_processed_orders=MagicMock(return_value=set()),
            load_disabled_orders=MagicMock(return_value=set()),
            load_unconfirmed_orders=MagicMock(return_value=set()),
            navigate_to_orders=MagicMock(return_value=True),
            _ensure_logged_in=MagicMock(return_value=True),
            _build_date_range_param=MagicMock(return_value=""),
            search_asin=MagicMock(return_value=True),
            _iter_paid_order_pages=MagicMock(return_value=iter([orders])),
            _order_row_on_current_page=MagicMock(return_value=True),
            send_email_with_pdf=send,
            save_processed_order=MagicMock(),
            save_followup_not_sent_order=save_missing_followup,
            verify_and_follow_up=verify,
            export_failed_records=export,
            show_failure_summary=MagicMock(),
            save_run_report=save_report,
            sync_saved_report=MagicMock(return_value=(0, 0)),
        ):
            app.run_task(config, status_holder=holder)

        self.assertEqual(send.call_count, 2)
        self.assertEqual(verify.call_count, 2)
        save_missing_followup.assert_not_called()
        self.assertEqual(holder.status.state, app.RunState.FINISHED)
        self.assertEqual(export.call_args.args[0][0].reason, "回访发送未确认，需人工核对")
        item = save_report.call_args.args[0]["items"][0]
        self.assertEqual((item["orders"], item["success"], item["failed"]), (2, 1, 1))
        self.assertEqual(item["remarks"], [
            f"订单 {orders[0].order_id}：回访发送未确认，需人工核对"])

    def test_found_conversation_without_input_records_failure_and_continues(self):
        import amazon_bulk_send as app

        order = app.OrderInfo(order_id="555-5555555-5555555", asin="B0TEST01")
        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/orders-v3/search"
        saved_report = MagicMock(return_value=Path("ignored.json"))
        save_missing_followup = MagicMock(return_value=True)
        summary = MagicMock()
        with patch.multiple(
            app,
            open_store_via_api=MagicMock(return_value=9222),
            connect_to_store_cdp=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock(), page)),
            _poll_login=MagicMock(return_value=True),
            load_processed_orders=MagicMock(return_value=set()),
            load_disabled_orders=MagicMock(return_value=set()),
            load_unconfirmed_orders=MagicMock(return_value=set()),
            navigate_to_orders=MagicMock(return_value=True),
            _ensure_logged_in=MagicMock(return_value=True),
            _build_date_range_param=MagicMock(return_value=""),
            search_asin=MagicMock(return_value=True),
            _iter_paid_order_pages=MagicMock(return_value=iter([[order]])),
            _order_row_on_current_page=MagicMock(return_value=True),
            send_email_with_pdf=MagicMock(return_value=True),
            save_processed_order=MagicMock(return_value=True),
            save_followup_not_sent_order=save_missing_followup,
            verify_and_follow_up=MagicMock(return_value="input_unavailable"),
            export_failed_records=MagicMock(return_value="report.csv"),
            show_failure_summary=summary,
            save_run_report=saved_report,
            sync_saved_report=MagicMock(return_value=(0, 0)),
        ):
            holder = app.StatusHolder()
            app.run_task(app.TaskConfig(store_name="TEST", asins=["B0TEST01"],
                                        follow_up_message="Follow-up text"),
                         status_holder=holder)

        self.assertEqual(holder.status.state, app.RunState.FINISHED)
        save_missing_followup.assert_called_once_with(order.order_id, "TEST")
        self.assertEqual(summary.call_args.kwargs["followup_not_sent_ids"], [order.order_id])
        self.assertEqual(saved_report.call_args.args[0]["items"][0]["remarks"],
                         ["订单对话已找到，但回访输入框未就绪，需人工核对"])

    def test_missing_conversation_records_followup_not_sent(self):
        import amazon_bulk_send as app

        order = app.OrderInfo(order_id="444-4444444-4444444", asin="B0TEST01")
        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/orders-v3/search"
        save_missing_followup = MagicMock(return_value=True)
        summary = MagicMock()
        with patch.multiple(
            app,
            open_store_via_api=MagicMock(return_value=9222),
            connect_to_store_cdp=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock(), page)),
            _poll_login=MagicMock(return_value=True),
            load_processed_orders=MagicMock(return_value=set()),
            load_disabled_orders=MagicMock(return_value=set()),
            load_unconfirmed_orders=MagicMock(return_value=set()),
            load_followup_not_sent_orders=MagicMock(return_value=set()),
            navigate_to_orders=MagicMock(return_value=True),
            _ensure_logged_in=MagicMock(return_value=True),
            _build_date_range_param=MagicMock(return_value=""),
            search_asin=MagicMock(return_value=True),
            _iter_paid_order_pages=MagicMock(return_value=iter([[order]])),
            _order_row_on_current_page=MagicMock(return_value=True),
            send_email_with_pdf=MagicMock(return_value=True),
            save_processed_order=MagicMock(return_value=True),
            save_followup_not_sent_order=save_missing_followup,
            verify_and_follow_up=MagicMock(return_value=False),
            export_failed_records=MagicMock(return_value="report.csv"),
            show_failure_summary=summary,
            save_run_report=MagicMock(return_value=Path("ignored.json")),
            sync_saved_report=MagicMock(return_value=(0, 0)),
        ):
            app.run_task(app.TaskConfig(store_name="TEST", asins=["B0TEST01"],
                                        follow_up_message="Follow-up text"))

        save_missing_followup.assert_called_once_with(order.order_id, "TEST")
        self.assertEqual(summary.call_args.kwargs["followup_not_sent_ids"], [order.order_id])

    def test_no_followup_template_does_not_create_manual_followup_record(self):
        import amazon_bulk_send as app

        order = app.OrderInfo(order_id="444-4444444-4444444", asin="B0TEST01")
        page = MagicMock()
        page.url = "https://sellercentral.amazon.com/orders-v3/search"
        save_missing_followup = MagicMock()
        with patch.multiple(
            app,
            open_store_via_api=MagicMock(return_value=9222),
            connect_to_store_cdp=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock(), page)),
            _poll_login=MagicMock(return_value=True),
            load_processed_orders=MagicMock(return_value=set()),
            load_disabled_orders=MagicMock(return_value=set()),
            load_unconfirmed_orders=MagicMock(return_value=set()),
            load_followup_not_sent_orders=MagicMock(return_value=set()),
            navigate_to_orders=MagicMock(return_value=True),
            _ensure_logged_in=MagicMock(return_value=True),
            _build_date_range_param=MagicMock(return_value=""),
            search_asin=MagicMock(return_value=True),
            _iter_paid_order_pages=MagicMock(return_value=iter([[order]])),
            _order_row_on_current_page=MagicMock(return_value=True),
            send_email_with_pdf=MagicMock(return_value=True),
            save_processed_order=MagicMock(return_value=True),
            save_followup_not_sent_order=save_missing_followup,
            verify_and_follow_up=MagicMock(return_value="input_unavailable"),
            export_failed_records=MagicMock(return_value="report.csv"),
            show_failure_summary=MagicMock(),
            save_run_report=MagicMock(return_value=Path("ignored.json")),
            sync_saved_report=MagicMock(return_value=(0, 0)),
        ):
            app.run_task(app.TaskConfig(store_name="TEST", asins=["B0TEST01"]))

        save_missing_followup.assert_not_called()

    def test_summary_lists_orders_requiring_manual_followup(self):
        import amazon_bulk_send as app

        order_id = "444-4444444-4444444"
        dialog = MagicMock()
        with patch.object(app, "_run_on_tk_thread", side_effect=lambda factory: factory(MagicMock())), \
             patch.object(app.tk, "Toplevel", return_value=dialog), \
             patch.object(app.tk, "Text") as text_box, \
             patch.object(app.ttk, "Label"), \
             patch.object(app.ttk, "Frame"), \
             patch.object(app.ttk, "Treeview"), \
             patch.object(app.ttk, "Button"):
            app.show_failure_summary(
                [app.FailedRecord(asin="B0TEST01", order_id=order_id,
                                  reason="订单对话已找到，但回访输入框未就绪，需人工核对")],
                total_processed=1, followup_not_sent_ids=[order_id])

        text_box.return_value.insert.assert_called_once_with("1.0", order_id)

    def test_new_sent_bubble_must_persist_after_reload(self):
        from playwright.sync_api import sync_playwright
        import amazon_bulk_send as app

        persisted = {"count": 1}

        def serve(route):
            bubbles = "".join(
                '<div class="sent-message-component"><div class="message-body-text">'
                '<p>Follow-up</p><p>text</p></div></div>' for _ in range(persisted["count"])
            )
            route.fulfill(content_type="text/html", body=f'<div class="case-message-view">{bubbles}</div>')

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route("**/inbox?cc=1", serve)
                page.goto("https://example.test/inbox?cc=1")
                before = app._sent_follow_up_count(page, "Follow-up text")
                persisted["count"] = 2
                page.evaluate("""() => setTimeout(() => {
                    document.querySelector('.case-message-view').insertAdjacentHTML(
                        'beforeend', '<div class="sent-message-component"><div class="message-body-text">Follow-up text</div></div>');
                }, 100)""")

                self.assertTrue(app._wait_for_follow_up_confirmation(
                    page, "Follow-up text", before, timeout=2))
                self.assertEqual(app._sent_follow_up_count(page, "Follow-up text"), 2)
                page.evaluate("""() => document.querySelector('.case-message-view').insertAdjacentHTML(
                    'beforeend', '<div class="sent-message-component"><div class="message-body-text">Follow-up text</div></div>')""")
                self.assertFalse(app._wait_for_follow_up_confirmation(
                    page, "Follow-up text", 2, timeout=1))
                self.assertEqual(app._sent_follow_up_count(page, "Follow-up text"), 2)
            finally:
                browser.close()

    def test_click_without_confirmed_message_is_not_success(self):
        import amazon_bulk_send as app

        page = MagicMock()
        with patch.object(app, "random_delay"), \
             patch.object(app, "_wait_messaging_loading_done"), \
             patch.object(app, "_click_send_button", return_value=True), \
             patch.object(app, "_wait_for_follow_up_confirmation", return_value=False):
            result = app._send_follow_up_message(page, "Follow-up text")

        self.assertFalse(result)

    def test_failed_send_click_does_not_report_success_or_wait(self):
        import amazon_bulk_send as app

        page = MagicMock()
        with patch.object(app, "random_delay"), \
             patch.object(app, "_wait_messaging_loading_done"), \
             patch.object(app, "_click_send_button", return_value=False):
            result = app._send_follow_up_message(page, "Follow-up text")

        self.assertFalse(result)
        page.wait_for_timeout.assert_not_called()

    def test_hidden_exit_survey_does_not_capture_follow_up_message(self):
        from playwright.sync_api import sync_playwright
        import amazon_bulk_send as app

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content("""
                    <textarea id="ngstrim-exit-survey-textarea" hidden></textarea>
                    <div class="case-message-view">
                      <div class="case-message-view-composer">
                        <div class="case-message-view-composer-textarea-container">
                          <textarea id="reply"></textarea>
                        </div>
                      </div>
                    </div>
                """)
                with patch.object(app, "random_delay"), \
                     patch.object(app, "_wait_messaging_loading_done"), \
                     patch.object(app, "_wait_for_follow_up_confirmation", return_value=True) as confirm, \
                     patch.object(app, "_click_send_button", return_value=True) as send:
                    result = app._send_follow_up_message(page, "Follow-up text")

                self.assertTrue(result)
                self.assertEqual(page.locator("#reply").input_value(), "Follow-up text")
                self.assertEqual(page.locator("#ngstrim-exit-survey-textarea").input_value(), "")
                send.assert_called_once_with(page)
                confirm.assert_called_once_with(page, "Follow-up text", 0)
            finally:
                browser.close()

    def test_expanded_amazon_composer_textarea_is_selected(self):
        from playwright.sync_api import sync_playwright
        import amazon_bulk_send as app

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content("""
                    <div class="case-message-composer-experience-container">
                      <div class="case-message-view-composer" id="case-message-composer">
                        <div class="case-message-view-composer-textarea-container expanded">
                          <textarea class="undefined fake-kat-textarea"></textarea>
                        </div>
                        <kat-button label="填写留言" onclick="document.querySelector('#case-message-composer textarea').remove()"></kat-button>
                        <kat-button class="send-message-button" label="发送"></kat-button>
                      </div>
                    </div>
                """)
                with patch.object(app, "random_delay"), \
                     patch.object(app, "_wait_messaging_loading_done"), \
                     patch.object(app, "_click_send_button", return_value=True), \
                     patch.object(app, "_wait_for_follow_up_confirmation", return_value=True):
                    self.assertTrue(app._send_follow_up_message(page, "Follow-up text"))
                self.assertEqual(
                    page.locator("#case-message-composer textarea").input_value(),
                    "Follow-up text",
                )
            finally:
                browser.close()


class TestGuiExecutionButtons(unittest.TestCase):
    def test_stop_button_reenabled_for_second_run(self):
        import amazon_bulk_send as app

        dialog = object.__new__(app.InputDialog)
        for name in ("btn_start", "btn_cancel", "btn_pause", "btn_stop_btn", "btn_return"):
            setattr(dialog, name, MagicMock())

        dialog._switch_buttons_to_done_mode()
        dialog._switch_buttons_to_input_mode()
        dialog._switch_buttons_to_exec_mode()

        dialog.btn_stop_btn.configure.assert_called_with(state="normal")


class TestFeishuSyncOnExit(unittest.TestCase):
    def test_window_close_waits_for_worker_sync(self):
        import threading
        import amazon_bulk_send as app

        window = object.__new__(app.InputDialog)
        window._closing = False
        window._exec_mode = True
        window._threads = [MagicMock()]
        window._threads[0].is_alive.return_value = True
        window._status_holder = app.StatusHolder()
        window._status_holder.status.state = app.RunState.SENDING
        window._stop_event = threading.Event()
        window._pause_event = threading.Event()
        window.root = MagicMock()
        with patch.object(app.messagebox, "askyesno", return_value=True), \
             patch.object(window, "_safe_close") as close:
            window._on_window_close()
            self.assertTrue(window._stop_event.is_set())
            close.assert_not_called()
            window._threads[0].is_alive.return_value = False
            window._close_when_workers_done()
            close.assert_called_once_with()

    def test_manual_stop_saves_and_syncs_report(self):
        import threading
        import amazon_bulk_send as app

        stopped = threading.Event()
        stopped.set()
        holder = app.StatusHolder()
        config = app.TaskConfig(store_name="TEST", asins=["B0TEST123"],
                                feishu_app_id="saved-id", feishu_app_secret="saved-secret")
        with patch.object(app, "_set_global_events"), \
             patch.object(app, "save_run_report", return_value=Path("report.json")) as save, \
             patch.object(app, "sync_saved_report", return_value=(0, 1)) as sync:
            app.run_task(config, stop_event=stopped, status_holder=holder)

        self.assertEqual(holder.status.state, app.RunState.STOPPED)
        self.assertEqual(save.call_args.args[0]["items"][0]["orders"], 0)
        self.assertNotIn("feishu_app_secret", save.call_args.args[0])
        sync.assert_called_once_with(Path("report.json"), app_id="saved-id",
                                     app_secret="saved-secret")

    def test_exception_saves_and_syncs_report(self):
        import amazon_bulk_send as app

        holder = app.StatusHolder()
        config = app.TaskConfig(store_name="TEST", asins=["B0TEST123"])
        with patch.object(app, "_set_global_events"), \
             patch.object(app, "open_store_via_api", side_effect=ValueError("startup failed")), \
             patch("traceback.print_exc"), \
             patch.object(app, "save_run_report", return_value=Path("report.json")) as save, \
             patch.object(app, "sync_saved_report", return_value=(0, 1)) as sync:
            app.run_task(config, status_holder=holder)

        self.assertEqual(holder.status.state, app.RunState.ERROR)
        self.assertIn("startup failed", save.call_args.args[0]["items"][0]["remarks"][0])
        sync.assert_called_once_with(Path("report.json"), app_id="",
                                     app_secret="")


class TestLoginReadinessPolling(unittest.TestCase):
    def test_retries_every_five_seconds_until_login_is_ready(self):
        import amazon_bulk_send as app

        page = MagicMock()
        with patch.object(app, "handle_login", side_effect=[False, False, True]) as login, \
             patch.object(app, "random_delay") as delay:
            result = app._poll_login(page)

        self.assertTrue(result)
        self.assertEqual(login.call_count, 3)
        self.assertEqual(delay.call_count, 2)
        delay.assert_any_call(5, 5)

    def test_stops_after_four_checks(self):
        import amazon_bulk_send as app

        with patch.object(app, "handle_login", return_value=False) as login, \
             patch.object(app, "random_delay") as delay:
            result = app._poll_login(MagicMock())

        self.assertFalse(result)
        self.assertEqual(login.call_count, 4)
        self.assertEqual(delay.call_count, 3)


class TestSkippedBuyerName(unittest.TestCase):
    """指定买家名称只在精确匹配时跳过。"""

    def test_exact_business_account_name_is_skipped(self):
        from amazon_bulk_send import OrderInfo, _should_skip_buyer_order

        order = OrderInfo(
            order_id="123-4567890-1234567",
            buyer_name="Amazon Business EU SARL",
        )
        self.assertTrue(_should_skip_buyer_order(order))

    def test_other_names_are_not_skipped(self):
        from amazon_bulk_send import OrderInfo, _should_skip_buyer_order

        for buyer_name in (
            "Amazon Business EU SARL ",
            "amazon business eu sarl",
            "普通买家",
            "",
        ):
            with self.subTest(buyer_name=buyer_name):
                self.assertFalse(_should_skip_buyer_order(
                    OrderInfo(order_id="123-4567890-1234567", buyer_name=buyer_name)
                ))


# ============================================================================
# 边缘情况测试
# ============================================================================

class TestEdgeCases(unittest.TestCase):
    """边缘情况测试"""

    def test_empty_asin_list(self):
        """空 ASIN 列表"""
        from amazon_bulk_send import TaskConfig
        tc = TaskConfig(asins=[])
        self.assertEqual(len(tc.asins), 0)

    def test_duplicate_asin(self):
        """重复 ASIN"""
        asins = ["B0A", "B0A", "B0B"]
        unique = list(set(asins))
        self.assertEqual(len(unique), 2)

    def test_long_store_name(self):
        """长店铺名称"""
        from amazon_bulk_send import TaskConfig
        long_name = "A" * 200
        tc = TaskConfig(store_name=long_name)
        self.assertEqual(len(tc.store_name), 200)

    def test_special_chars_in_message(self):
        """回访消息中的特殊字符"""
        from amazon_bulk_send import TaskConfig
        msg = "感谢购买！\n如有问题请联系我们。\n邮箱: support@example.com"
        tc = TaskConfig(follow_up_message=msg)
        self.assertIn("support@example.com", tc.follow_up_message)

    def test_store_name_normalization(self):
        """店铺名称标准化匹配"""
        expected = "EXAMPLE-STORE-UK"
        found = "EXAMPLE STORE UK"
        norm_expected = expected.lower().replace("-", " ").replace("_", " ").strip()
        norm_found = found.lower().replace("-", " ").replace("_", " ").strip()
        self.assertIn(norm_expected, norm_found)
        self.assertIn("example store uk", norm_found)


# ============================================================================
# 运行入口
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  亚马逊批量发送 — 项目测试套件")
    print("=" * 60)
    print()

    # 使用 unittest 运行器
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # 添加所有测试类
    suite.addTests(loader.loadTestsFromTestCase(TestDataModels))
    suite.addTests(loader.loadTestsFromTestCase(TestUtilityFunctions))
    suite.addTests(loader.loadTestsFromTestCase(TestConfigManagement))
    suite.addTests(loader.loadTestsFromTestCase(TestConstants))
    suite.addTests(loader.loadTestsFromTestCase(TestCDPDiagnostic))
    suite.addTests(loader.loadTestsFromTestCase(TestCSVExport))
    suite.addTests(loader.loadTestsFromTestCase(TestSyntaxAndImports))
    suite.addTests(loader.loadTestsFromTestCase(TestZigniaoAPI))
    suite.addTests(loader.loadTestsFromTestCase(TestBuyerNavigationFallback))
    suite.addTests(loader.loadTestsFromTestCase(TestSkippedBuyerName))
    suite.addTests(loader.loadTestsFromTestCase(TestEdgeCases))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # 总结
    print()
    print("=" * 60)
    print(f"  测试结果: {result.testsRun} 个测试")
    print(f"  通过: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"  失败: {len(result.failures)}")
    print(f"  错误: {len(result.errors)}")
    print("=" * 60)

    # 返回退出码
    sys.exit(0 if result.wasSuccessful() else 1)
