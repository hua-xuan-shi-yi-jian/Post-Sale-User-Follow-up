"""Offline checks for Feishu execution summaries and Bitable writes."""

import io
import json
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import feishu_sync as sync


# Synthetic fixtures keep format-sensitive tests useful without business identifiers.
REPORT_TABLE_ID = "tblReport123"
EU_TABLE_ID = "tblEurope123"
PRODUCT_TABLE_ID = "tblProduct456"
WIKI_TOKEN = "wikiDemo123"
FAKE_SETTINGS = {
    "feishu_report_table_id": REPORT_TABLE_ID,
    "feishu_eu_table_id": EU_TABLE_ID,
    "feishu_product_table_id": PRODUCT_TABLE_ID,
    "feishu_wiki_token": WIKI_TOKEN,
}


class FakeSettingsTest(unittest.TestCase):
    def setUp(self):
        settings_patch = patch.object(sync, "_local_settings", return_value=FAKE_SETTINGS)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        env_patch = patch.dict(sync.os.environ, {
            key: "" for key in (
                "FEISHU_APP_TOKEN", "FEISHU_WIKI_TOKEN", "FEISHU_EU_TABLE_ID",
                "FEISHU_REPORT_TABLE_ID", "FEISHU_PRODUCT_TABLE_ID",
                "FEISHU_PRODUCT_TABLE_URL",
            )
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)


class TestLocalSettings(unittest.TestCase):
    def test_source_reads_config_beside_module(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(FAKE_SETTINGS), encoding="utf-8")
            with patch.object(sync, "__file__", str(Path(directory) / "feishu_sync.py")), \
                 patch.object(sync.sys, "frozen", False, create=True):
                self.assertEqual(sync._local_settings()["feishu_eu_table_id"], EU_TABLE_ID)

    def test_packaged_app_reads_config_beside_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(FAKE_SETTINGS), encoding="utf-8")
            with patch.object(sync.sys, "frozen", True, create=True), \
                 patch.object(sync.sys, "executable", str(Path(directory) / "app.exe")):
                self.assertEqual(sync._local_settings()["feishu_wiki_token"], WIKI_TOKEN)

    def test_environment_overrides_local_table_id(self):
        with patch.object(sync, "_local_settings", return_value=FAKE_SETTINGS), \
             patch.dict(sync.os.environ, {"FEISHU_EU_TABLE_ID": "tblOverride123"}):
            self.assertEqual(sync._setting("feishu_eu_table_id", "FEISHU_EU_TABLE_ID"),
                             "tblOverride123")


def report(product_name="Pump"):
    return {
        "date": "2026-10-12", "store": "Store A",
        "date_range": "2026-09-01 ~ 2026-09-30",
        "pdf_path": "manual.pdf", "product_table_url": "",
        "items": [{
            "asin": "B0TEST0001", "product_name": product_name,
            "orders": 3, "success": 2, "failed": 1,
            "messages": ["amazon.com: Hello", "amazon.de: Guten Tag"],
            "remarks": ["111-1111111-1111111: 上传失败"],
        }],
    }


def schema(pdf_type=17):
    return [{"field_name": name, "type": (
        2 if name in ("订单数", "发送成功", "发送失败") else
        5 if name == "发送日期" else pdf_type if name == "PDF文档" else 1
    )} for name in sync.FIELDS]


class TestExecutionReport(FakeSettingsTest):
    def test_counts_attempted_orders_and_records_unconfirmed_stop(self):
        config = SimpleNamespace(
            store_name="Store A", asins=["B0TEST0001"], pdf_path="manual.pdf",
            start_date="2026-09-01", end_date="2026-09-30",
            follow_up_messages={"amazon.com": "Hello", "amazon.de": "Guten Tag"},
            follow_up_message="", product_table_url="",
        )
        failures = [SimpleNamespace(
            asin="B0TEST0001", order_id="111-1111111-1111111", reason="上传失败")]
        result = sync.build_report(
            config,
            {"B0TEST0001": {"111-1111111-1111111", "222-2222222-2222222", "333-3333333-3333333"}},
            {"B0TEST0001": {"333-3333333-3333333"}},
            failures, "用户终止任务",
        )
        item = result["items"][0]
        self.assertEqual((item["orders"], item["success"], item["failed"]), (3, 1, 2))
        self.assertEqual(item["remarks"], [
            "上传失败", "执行中断，发送结果未确认", "执行结束原因: 用户终止任务",
        ])
        self.assertEqual(item["messages"], ["amazon.com: Hello", "amazon.de: Guten Tag"])
        self.assertEqual(result["report_table_id"], REPORT_TABLE_ID)
        self.assertEqual(result["product_table_id"], PRODUCT_TABLE_ID)

    def test_report_keeps_selected_table_ids_for_later_retry(self):
        config = SimpleNamespace(
            store_name="Store A", asins=["B0TEST0001"], pdf_path="manual.pdf",
            start_date="2026-09-01", end_date="2026-09-30",
            follow_up_messages={}, follow_up_message="",
            feishu_report_table_id="tblReport123",
            feishu_product_table_id="tblProduct456",
        )
        result = sync.build_report(config, {}, {}, [])
        self.assertEqual(result["report_table_id"], "tblReport123")
        self.assertEqual(result["product_table_id"], "tblProduct456")

    def test_report_remarks_remove_order_ids_and_deduplicate_reasons(self):
        config = SimpleNamespace(
            store_name="Store A", asins=["B0TEST0001"], pdf_path="manual.pdf",
            start_date="2026-09-01", end_date="2026-09-30",
            follow_up_messages={}, follow_up_message="",
        )
        failures = [
            SimpleNamespace(asin="B0TEST0001", order_id=order_id,
                            reason=f"订单 {order_id}：联系主题已被 Amazon 禁用")
            for order_id in ("111-1111111-1111111", "222-2222222-2222222")
        ]
        execution = sync.build_report(config, {
            "B0TEST0001": {failure.order_id for failure in failures}
        }, {}, failures)
        self.assertEqual(execution["items"][0]["remarks"], ["联系主题已被 Amazon 禁用"])

    def test_duplicate_text_is_not_appended_twice(self):
        self.assertEqual(
            sync._append_missing("amazon.com: Hello", ["amazon.com: Hello", "amazon.de: Guten Tag"]),
            "amazon.com: Hello\namazon.de: Guten Tag",
        )
        self.assertEqual(
            sync._append_missing("amazon.com: Hello World", ["amazon.com: Hello"]),
            "amazon.com: Hello World\namazon.com: Hello",
        )


class TestBitableWrite(FakeSettingsTest):
    def setUp(self):
        super().setUp()
        self.client = sync.FeishuBitable("app-id", "app-secret", app_token="app-token")
        self.client._authorize = MagicMock()
        self.client._json = MagicMock(return_value={"code": 0, "data": {}})

    def test_http_error_exposes_feishu_status_and_error_code(self):
        body = io.BytesIO(json.dumps({
            "code": 99991672, "msg": "No permission to access this bitable",
        }).encode())
        error = HTTPError("https://open.feishu.cn/fields", 403, "Forbidden", {}, body)
        with patch.object(sync, "urlopen", side_effect=error):
            with self.assertRaises(sync.FeishuSyncError) as caught:
                sync.FeishuBitable._json(
                    self.client, "GET", "/bitable/v1/apps/app-token/tables/tblReport123/fields")
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertIn("99991672", str(caught.exception))
        self.assertIn("No permission", str(caught.exception))

    def test_connection_error_exposes_underlying_network_reason(self):
        with patch.object(sync, "urlopen", side_effect=URLError(
                ConnectionRefusedError(10061, "connection refused"))):
            with self.assertRaisesRegex(sync.FeishuSyncError, "connection refused"):
                sync.FeishuBitable._json(
                    self.client, "GET", "/bitable/v1/apps/app-token/tables/tblReport123/fields")

    def test_access_check_reads_both_field_lists_without_writes(self):
        self.client._authorize = MagicMock()
        self.client._items = MagicMock(side_effect=[
            [{"field_name": "ASIN"}],
            [{"field_name": "ASIN"}, {"field_name": "产品名称"}],
        ])
        self.assertEqual(self.client.check_access("tblProduct456"), (1, 2))
        self.client._items.assert_any_call("fields")
        self.client._items.assert_any_call("fields", "tblProduct456")
        self.client._json.assert_not_called()

    def test_access_check_includes_european_report_table(self):
        self.client._items = MagicMock(side_effect=[
            schema(), schema(), [{"field_name": "产品名称"}],
        ])
        self.assertEqual(self.client.check_access("tblProduct456", include_eu=True),
                         (len(sync.FIELDS), len(sync.FIELDS), 1))
        self.client._items.assert_any_call("fields", EU_TABLE_ID)
        self.client._json.assert_not_called()

    def test_missing_european_table_stops_before_write(self):
        with patch.object(sync, "_local_settings", return_value={}):
            with self.assertRaisesRegex(sync.FeishuSyncError, "欧规回传表 ID"):
                self.client.sync(report("Pump 欧规"))
        self.client._json.assert_not_called()

    def test_missing_app_and_wiki_tokens_stops_before_auth_request(self):
        with patch.object(sync, "_local_settings", return_value={}):
            client = sync.FeishuBitable("app-id", "app-secret",
                                       report_table_id=REPORT_TABLE_ID)
            client._json = MagicMock()
            with self.assertRaisesRegex(sync.FeishuSyncError, "App Token 或 Wiki Token"):
                client._authorize()
        client._json.assert_not_called()

    def test_product_suffix_routes_mixed_report_to_separate_tables(self):
        execution = report("Demo pump（欧规）")
        execution["items"].extend([
            {**execution["items"][0], "asin": "B0USA00001",
             "product_name": "Demo pump（美规）"},
            {**execution["items"][0], "asin": "B0OTHER001", "product_name": "Pump"},
        ])
        self.client._items = MagicMock(side_effect=lambda kind, table_id: (
            schema() if kind == "fields" else []))
        self.client._upload_pdf = MagicMock(return_value="file-token")
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            execution["pdf_path"] = str(pdf)
            self.assertEqual(self.client.sync(execution), (3, 0))

        post_paths = [call.args[1] for call in self.client._json.call_args_list
                      if call.args[0] == "POST"]
        self.assertEqual(sum(f"/tables/{EU_TABLE_ID}/records" in path
                             for path in post_paths), 1)
        self.assertEqual(sum(f"/tables/{REPORT_TABLE_ID}/records" in path
                             for path in post_paths), 2)
        self.client._items.assert_any_call("records", EU_TABLE_ID)
        self.client._items.assert_any_call("records", REPORT_TABLE_ID)

    def test_european_suffix_accepts_plain_and_parenthesized_names(self):
        for product_name in ("Pump 欧规", "Pump（欧规）", "Pump (欧规)  "):
            with self.subTest(product_name=product_name):
                self.assertEqual(sync._report_table_for_product(product_name, REPORT_TABLE_ID),
                                 EU_TABLE_ID)

    def test_all_target_tables_are_checked_before_any_write(self):
        execution = report("Pump 欧规")
        execution["items"].append({**execution["items"][0],
                                   "asin": "B0USA00001", "product_name": "Pump 美规"})
        self.client._items = MagicMock(side_effect=lambda kind, table_id: (
            schema() if kind == "fields" and table_id == EU_TABLE_ID else
            schema()[:-1] if kind == "fields" else []))
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            execution["pdf_path"] = str(pdf)
            with self.assertRaisesRegex(sync.FeishuSyncError, "缺少字段"):
                self.client.sync(execution)
        self.client._json.assert_not_called()

    def test_duplicate_asin_across_product_types_is_rejected(self):
        execution = report("Pump 欧规")
        execution["items"].append({**execution["items"][0], "product_name": "Pump 美规"})
        with self.assertRaisesRegex(sync.FeishuSyncError, "重复 ASIN"):
            self.client.sync(execution)
        self.client._json.assert_not_called()

    def test_existing_european_row_is_updated_in_european_table(self):
        existing = {"record_id": "rec-eu", "fields": {
            "店铺": "Store A", "ASIN": "B0TEST0001",
            "发送日期": "2026-10-12", "订单日期范围": "2026-09-01 ~ 2026-09-30",
            "订单数": 4, "发送成功": 3, "发送失败": 1,
        }}
        self.client._items = MagicMock(side_effect=[schema(), [existing]])
        self.assertEqual(self.client.sync(report("Pump 欧规")), (0, 1))
        self.assertIn(f"/tables/{EU_TABLE_ID}/records/rec-eu",
                      self.client._json.call_args.args[1])

    def test_wiki_link_resolves_bitable_app_token(self):
        client = sync.FeishuBitable("app-id", "app-secret")
        client._json = MagicMock(side_effect=[
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 0, "data": {"node": {"obj_token": "app-token"}}},
        ])
        client._authorize()
        self.assertEqual((client.token, client.app_token), ("tenant-token", "app-token"))
        self.assertIn(WIKI_TOKEN, client._json.call_args.args[1])

    def test_existing_store_and_asin_updates_only_five_fields(self):
        existing = {"record_id": "rec-1", "fields": {
            "店铺": [{"text": "Store A"}], "ASIN": "B0TEST0001",
            "发送日期": "2026-10-12",
            "订单日期范围": "2026-09-01 ~ 2026-09-30",
            "订单数": 4, "发送成功": 3, "发送失败": 1,
            "邮件内容": "amazon.com: Hello", "备注": "旧备注",
        }}
        self.client._items = MagicMock(side_effect=[schema(), [existing]])
        self.client._upload_pdf = MagicMock()

        self.assertEqual(self.client.sync(report()), (0, 1))
        method, path, body = self.client._json.call_args.args
        self.assertEqual(method, "PUT")
        self.assertTrue(path.endswith("/rec-1"))
        fields = body["fields"]
        self.assertEqual(set(fields), {"订单数", "发送成功", "发送失败", "邮件内容", "备注"})
        self.assertEqual((fields["订单数"], fields["发送成功"], fields["发送失败"]), (7, 5, 2))
        self.assertEqual(fields["邮件内容"], "amazon.com: Hello\namazon.de: Guten Tag")
        self.assertEqual(fields["备注"], "旧备注\n上传失败")
        self.client._upload_pdf.assert_not_called()

    def test_pending_report_remarks_do_not_send_embedded_order_ids(self):
        existing = {"record_id": "rec-1", "fields": {
            "店铺": "Store A", "ASIN": "B0TEST0001",
            "发送日期": "2026-10-12",
            "订单日期范围": "2026-09-01 ~ 2026-09-30",
            "订单数": 4, "发送成功": 3, "发送失败": 1,
        }}
        self.client._items = MagicMock(side_effect=[schema(), [existing]])
        pending = report()
        pending["items"][0]["remarks"] = [
            "111-1111111-1111111: 上传失败",
            "222-2222222-2222222: 上传失败",
            "执行结束原因: 订单 333-3333333-3333333 回访发送未确认",
        ]
        self.assertEqual(self.client.sync(pending), (0, 1))
        self.assertEqual(self.client._json.call_args.args[2]["fields"]["备注"],
                         "上传失败\n执行结束原因: 回访发送未确认")

    def test_existing_feishu_remarks_are_cleaned_when_updating(self):
        existing = {"record_id": "rec-1", "fields": {
            "店铺": "Store A", "ASIN": "B0TEST0001",
            "发送日期": "2026-10-12",
            "订单日期范围": "2026-09-01 ~ 2026-09-30",
            "订单数": 4, "发送成功": 3, "发送失败": 1,
            "备注": "111-1111111-1111111: 上传失败",
        }}
        self.client._items = MagicMock(side_effect=[schema(), [existing]])
        pending = report()
        pending["items"][0]["remarks"] = ["222-2222222-2222222: 上传失败"]
        self.assertEqual(self.client.sync(pending), (0, 1))
        self.assertEqual(self.client._json.call_args.args[2]["fields"]["备注"], "上传失败")

    def test_unconfirmed_follow_up_remark_keeps_order_id_on_write(self):
        self.client._items = MagicMock(side_effect=[schema(), []])
        self.client._upload_pdf = MagicMock(return_value="file-token")
        pending = report()
        pending["items"][0]["remarks"] = [
            "订单 111-1111111-1111111：回访发送未确认，需人工核对"]
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            pending["pdf_path"] = str(pdf)
            self.assertEqual(self.client.sync(pending), (1, 0))
        self.assertEqual(self.client._json.call_args.args[2]["fields"]["备注"],
                         pending["items"][0]["remarks"][0])

    def test_same_store_and_asin_on_new_date_creates_new_record(self):
        existing = {"record_id": "rec-old", "fields": {
            "店铺": "Store A", "ASIN": "B0TEST0001", "发送日期": "2026-10-11",
            "订单数": 4, "发送成功": 3, "发送失败": 1,
        }}
        self.client._items = MagicMock(side_effect=[schema(), [existing]])
        self.client._upload_pdf = MagicMock(return_value="file-token")
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            execution = report()
            execution["pdf_path"] = str(pdf)
            self.assertEqual(self.client.sync(execution), (1, 0))
        method, _, body = self.client._json.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(body["fields"]["订单数"], 3)

    def test_same_day_date_timestamp_updates_existing_record(self):
        existing = {"record_id": "rec-today", "fields": {
            "店铺": "Store A", "ASIN": "B0TEST0001",
            "发送日期": 1791734400000,  # 2026-10-12 00:00 in Shanghai
            "订单日期范围": "2026-09-01 ~ 2026-09-30",
            "订单数": 4, "发送成功": 3, "发送失败": 1,
        }}
        self.client._items = MagicMock(side_effect=[schema(), [existing]])
        self.assertEqual(self.client.sync(report()), (0, 1))
        self.assertEqual(self.client._json.call_args.args[0], "PUT")
        self.assertEqual(self.client._json.call_args.args[2]["fields"]["订单数"], 7)

    def test_same_day_different_order_date_range_creates_new_record(self):
        existing = {"record_id": "rec-other-range", "fields": {
            "店铺": "Store A", "ASIN": "B0TEST0001",
            "发送日期": "2026-10-12",
            "订单日期范围": "2026-08-01 ~ 2026-08-31",
            "订单数": 4, "发送成功": 3, "发送失败": 1,
        }}
        self.client._items = MagicMock(side_effect=[schema(), [existing]])
        self.client._upload_pdf = MagicMock(return_value="file-token")
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            execution = report()
            execution["pdf_path"] = str(pdf)
            self.assertEqual(self.client.sync(execution), (1, 0))
        method, _, body = self.client._json.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(body["fields"]["订单日期范围"], "2026-09-01 ~ 2026-09-30")
        self.assertEqual(body["fields"]["订单数"], 3)

    def test_same_day_only_updates_matching_order_date_range(self):
        rows = [
            {"record_id": "rec-other-range", "fields": {
                "店铺": "Store A", "ASIN": "B0TEST0001",
                "发送日期": "2026-10-12",
                "订单日期范围": "2026-08-01 ~ 2026-08-31",
                "订单数": 100,
            }},
            {"record_id": "rec-matching-range", "fields": {
                "店铺": "Store A", "ASIN": "B0TEST0001",
                "发送日期": "2026-10-12",
                "订单日期范围": "2026/09/01至2026/09/30",
                "订单数": 4, "发送成功": 3, "发送失败": 1,
            }},
        ]
        self.client._items = MagicMock(side_effect=[schema(), rows])
        self.assertEqual(self.client.sync(report()), (0, 1))
        self.assertIn("/rec-matching-range", self.client._json.call_args.args[1])
        self.assertEqual(self.client._json.call_args.args[2]["fields"]["订单数"], 7)

    def test_multiple_dates_for_same_asin_only_update_matching_day(self):
        rows = [
            {"record_id": "rec-yesterday", "fields": {
                "店铺": "Store A", "ASIN": "B0TEST0001",
                "发送日期": "2026-10-11", "订单数": 100,
                "发送成功": 90, "发送失败": 10,
            }},
            {"record_id": "rec-today", "fields": {
                "店铺": "Store A", "ASIN": "B0TEST0001",
                "发送日期": "2026/10/12", "订单数": 4,
                "订单日期范围": "2026-09-01 ~ 2026-09-30",
                "发送成功": 3, "发送失败": 1,
            }},
        ]
        self.client._items = MagicMock(side_effect=[schema(), rows])
        self.assertEqual(self.client.sync(report()), (0, 1))
        self.assertIn("/rec-today", self.client._json.call_args.args[1])
        self.assertEqual(self.client._json.call_args.args[2]["fields"]["订单数"], 7)

    def test_duplicate_historical_dates_do_not_block_current_day_update(self):
        rows = [
            {"record_id": f"rec-old-{index}", "fields": {
                "店铺": "Store A", "ASIN": "B0TEST0001",
                "发送日期": "2026-08-18",
            }} for index in (1, 2)
        ] + [{"record_id": "rec-today", "fields": {
            "店铺": "Store A", "ASIN": "B0TEST0001",
            "发送日期": "2026-10-12", "订单数": 4,
            "订单日期范围": "2026-09-01 ~ 2026-09-30",
            "发送成功": 3, "发送失败": 1,
        }}]
        self.client._items = MagicMock(side_effect=[schema(), rows])

        self.assertEqual(self.client.sync(report()), (0, 1))
        self.assertIn("/rec-today", self.client._json.call_args.args[1])

    def test_duplicate_historical_dates_do_not_block_new_day_record(self):
        rows = [{"record_id": f"rec-old-{index}", "fields": {
            "店铺": "Store A", "ASIN": "B0TEST0001",
            "发送日期": "2026-08-18",
        }} for index in (1, 2)]
        self.client._items = MagicMock(side_effect=[schema(), rows])
        self.client._upload_pdf = MagicMock(return_value="file-token")
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            execution = report()
            execution["pdf_path"] = str(pdf)
            self.assertEqual(self.client.sync(execution), (1, 0))
        self.assertEqual(self.client._json.call_args.args[0], "POST")

    def test_same_day_duplicate_records_are_rejected(self):
        rows = [{"record_id": f"rec-{index}", "fields": {
            "店铺": "Store A", "ASIN": "B0TEST0001",
            "发送日期": date,
            "订单日期范围": "2026-09-01 ~ 2026-09-30",
        }} for index, date in ((1, "2026-10-12"), (2, 1791734400000))]
        self.client._items = MagicMock(side_effect=[schema(), rows])
        with self.assertRaisesRegex(sync.FeishuSyncError, "重复"):
            self.client.sync(report())
        self.client._json.assert_not_called()

    def test_historical_row_without_date_does_not_block_current_day(self):
        existing = {"record_id": "rec-no-date", "fields": {
            "店铺": "Store A", "ASIN": "B0TEST0001",
        }}
        today = {"record_id": "rec-today", "fields": {
            "店铺": "Store A", "ASIN": "B0TEST0001",
            "发送日期": "2026-10-12", "订单数": 4,
            "订单日期范围": "2026-09-01 ~ 2026-09-30",
            "发送成功": 3, "发送失败": 1,
        }}
        self.client._items = MagicMock(side_effect=[schema(), [existing, today]])
        self.assertEqual(self.client.sync(report()), (0, 1))
        self.assertIn("/rec-today", self.client._json.call_args.args[1])

    def test_selected_report_table_id_controls_read_and_write(self):
        client = sync.FeishuBitable("app-id", "app-secret", app_token="app-token",
                                    report_table_id="tblReport123")
        client._authorize = MagicMock()
        client._json = MagicMock(return_value={"code": 0, "data": {}})
        client._items = MagicMock(side_effect=[schema(), []])
        client._upload_pdf = MagicMock(return_value="file-token")
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            execution = report()
            execution["pdf_path"] = str(pdf)
            self.assertEqual(client.sync(execution), (1, 0))
        self.assertIn("/tables/tblReport123/records", client._json.call_args.args[1])
        client._json.reset_mock()
        client._items = sync.FeishuBitable._items.__get__(client)
        client._json.return_value = {"data": {"items": [], "has_more": False}}
        client._items("records")
        self.assertIn("/tables/tblReport123/records", client._json.call_args.args[1])

    def test_new_record_uploads_pdf_and_writes_all_fields(self):
        self.client._items = MagicMock(side_effect=[schema(), []])
        self.client._upload_pdf = MagicMock(return_value="file-token")
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            execution = report()
            execution["pdf_path"] = str(pdf)
            self.assertEqual(self.client.sync(execution), (1, 0))
        method, _, body = self.client._json.call_args.args
        self.assertEqual(method, "POST")
        fields = body["fields"]
        self.assertEqual(set(fields), set(sync.FIELDS))
        self.assertEqual(fields["PDF文档"], [{"file_token": "file-token"}])
        self.assertEqual(fields["产品名称"], "Pump")
        self.assertEqual(fields["备注"], "上传失败")
        self.assertIsInstance(fields["发送日期"], int)

    def test_new_record_without_product_name_fails_before_write(self):
        self.client._items = MagicMock(return_value=[])
        with self.assertRaisesRegex(sync.FeishuSyncError, "产品名称"):
            self.client.sync(report(product_name=""))
        self.client._json.assert_not_called()

    def test_new_record_uses_linked_product_table_name(self):
        self.client._items = MagicMock(side_effect=[
            [{"fields": {"ASIN": "B0TEST0001", "产品名称": "Demo product"}}],
            schema(),
            [],
        ])
        self.client._upload_pdf = MagicMock(return_value="file-token")
        execution = report(product_name="")
        execution["product_table_url"] = (
            "https://example.feishu.cn/wiki/wikiDemo123"
            "?table=tblProduct456&view=viewDemo123"
        )
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            execution["pdf_path"] = str(pdf)
            self.assertEqual(self.client.sync(execution), (1, 0))
        self.client._items.assert_any_call("records", "tblProduct456")
        self.assertEqual(self.client._json.call_args.args[2]["fields"]["产品名称"],
                         "Demo product")

    def test_european_name_from_mapping_routes_to_european_table(self):
        self.client._items = MagicMock(side_effect=[
            [{"fields": {"ASIN": "B0TEST0001", "产品名称": "Round pump 欧规"}}],
            schema(), [],
        ])
        self.client._upload_pdf = MagicMock(return_value="file-token")
        execution = report(product_name="   ")
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            execution["pdf_path"] = str(pdf)
            self.assertEqual(self.client.sync(execution), (1, 0))
        self.assertIn(f"/tables/{EU_TABLE_ID}/records",
                      self.client._json.call_args.args[1])
        self.assertEqual(execution["items"][0]["product_name"], "Round pump 欧规")

    def test_selected_mapping_id_takes_priority_over_saved_link(self):
        self.client._items = MagicMock(side_effect=[
            [{"fields": {"ASIN": "B0TEST0001", "产品名称": "Mapped product"}}],
            schema(), [],
        ])
        self.client._upload_pdf = MagicMock(return_value="file-token")
        execution = report(product_name="")
        execution["product_table_id"] = "tblProduct456"
        execution["product_table_url"] = (
            "https://example.feishu.cn/wiki/wikiDemo123"
            "?table=tblOld789"
        )
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            execution["pdf_path"] = str(pdf)
            self.assertEqual(self.client.sync(execution), (1, 0))
        self.client._items.assert_any_call("records", "tblProduct456")
        self.assertEqual(self.client._json.call_args.args[2]["fields"]["产品名称"],
                         "Mapped product")

    def test_pdf_field_must_be_attachment(self):
        self.client._items = MagicMock(side_effect=[schema(pdf_type=1), []])
        with self.assertRaisesRegex(sync.FeishuSyncError, "附件类型"):
            self.client.sync(report())
        self.client._json.assert_not_called()

    def test_reads_product_name_from_linked_bitable_table(self):
        source_url = (
            "https://example.feishu.cn/wiki/wikiDemo123"
            "?table=tblProduct456&view=viewDemo123"
        )
        self.client._json.return_value = {"data": {"items": [
            {"fields": {"ASIN": [{"text": "B0TEST0001"}],
                        "产品名称": [{"text": "Demo product"}]}},
            {"fields": {"产品ASIN": "B0EXAMPLE1", "产品名称": "Second demo product"}},
        ], "has_more": False}}
        self.assertEqual(
            self.client._product_names(source_url),
            {"B0TEST0001": "Demo product", "B0EXAMPLE1": "Second demo product"},
        )
        method, path = self.client._json.call_args.args[:2]
        self.assertEqual(method, "GET")
        self.assertIn("/tables/tblProduct456/records", path)
        self.assertNotIn("/tables/" + REPORT_TABLE_ID + "/records", path)

    def test_reads_product_name_from_selected_table_id(self):
        self.client._json.return_value = {"data": {"items": [
            {"fields": {"ASIN": "B0TEST0001", "产品名称": "Demo product"}},
        ], "has_more": False}}
        self.assertEqual(self.client._product_names("tblProduct456"),
                         {"B0TEST0001": "Demo product"})
        self.assertIn("/tables/tblProduct456/records", self.client._json.call_args.args[1])

    def test_product_table_duplicate_asin_with_different_names_fails(self):
        self.client._json.return_value = {"data": {"items": [
            {"fields": {"ASIN": "B0TEST0001", "产品名称": "Pump A"}},
            {"fields": {"ASIN": "B0TEST0001", "产品名称": "Pump B"}},
        ], "has_more": False}}
        with self.assertRaisesRegex(sync.FeishuSyncError, "ASIN 重复"):
            self.client._product_names(
                "https://example.feishu.cn/wiki/wikiDemo123"
                "?table=tblProduct456")

    def test_pdf_upload_uses_bitable_file_parent(self):
        self.client.token = "test-token"
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            response = io.BytesIO(json.dumps({"code": 0, "data": {"file_token": "file-token"}}).encode())
            with patch.object(sync, "urlopen") as open_url:
                open_url.return_value.__enter__.return_value = response
                self.assertEqual(self.client._upload_pdf(str(pdf)), "file-token")
                request = open_url.call_args.args[0]
                self.assertIn(b"bitable_file", request.data)
                self.assertIn(b"%PDF-1.4", request.data)


class TestSavedReport(FakeSettingsTest):
    def test_sync_report_uses_table_id_saved_with_execution(self):
        execution = report()
        execution["report_table_id"] = "tblReport123"
        with patch.object(sync, "FeishuBitable") as client:
            client.return_value.sync.return_value = (1, 0)
            self.assertEqual(sync.sync_report(execution, app_id="saved-id",
                                              app_secret="saved-secret"), (1, 0))
        self.assertEqual(client.call_args.kwargs["report_table_id"], "tblReport123")

    def test_successful_sync_marks_local_report_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps({"sync_status": "pending", "items": []}), encoding="utf-8")
            with patch.object(sync, "sync_report", return_value=(1, 0)) as send:
                self.assertEqual(sync.sync_saved_report(path, app_id="saved-id",
                                                        app_secret="saved-secret"), (1, 0))
            self.assertEqual(send.call_args.kwargs,
                             {"app_id": "saved-id", "app_secret": "saved-secret"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["sync_status"], "success")

    def test_saved_gui_credentials_take_priority_over_environment(self):
        with patch.dict(sync.os.environ, {"FEISHU_APP_ID": "env-id",
                                          "FEISHU_APP_SECRET": "env-secret"}), \
             patch.object(sync, "FeishuBitable") as client:
            client.return_value.sync.return_value = (0, 1)
            self.assertEqual(sync.sync_report({"items": []}, app_id="saved-id",
                                              app_secret="saved-secret"), (0, 1))
        self.assertEqual(client.call_args.args[:2], ("saved-id", "saved-secret"))

    def test_successful_report_cannot_be_synced_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps({"sync_status": "success"}), encoding="utf-8")
            with self.assertRaisesRegex(sync.FeishuSyncError, "重复累加"):
                sync.sync_saved_report(path)


if __name__ == "__main__":
    unittest.main()
