"""Sync Amazon execution summaries to Feishu Bitable tables by product type."""

import json
import os
import re
import sys
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


API_ROOT = "https://open.feishu.cn/open-apis"
SHANGHAI_TZ = timezone(timedelta(hours=8))
FIELDS = (
    "发送日期", "店铺", "产品名称", "ASIN", "订单日期范围", "订单数",
    "发送成功", "发送失败", "邮件内容", "PDF文档", "备注",
)


class FeishuSyncError(RuntimeError):
    pass


def _local_settings() -> dict:
    root = Path(sys.executable if getattr(sys, "frozen", False) else __file__).parent
    path = root / "config.json"
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FeishuSyncError("读取本地 config.json 失败") from exc
    if not isinstance(settings, dict):
        raise FeishuSyncError("本地 config.json 必须是 JSON 对象")
    return settings


def _setting(key: str, env_key: str) -> str:
    value = os.environ.get(env_key) or _local_settings().get(key, "")
    if not isinstance(value, str):
        raise FeishuSyncError(f"本地配置 {key} 必须是字符串")
    return value.strip()


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text(part) for part in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    return "" if value is None else str(value)


def _count(value) -> int:
    try:
        number = float(value or 0)
        if not number.is_integer() or number < 0:
            raise ValueError
        return int(number)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FeishuSyncError(f"飞书表格中的计数字段不是非负整数: {value!r}") from exc


def _date_key(value) -> str:
    """Normalize Bitable date timestamps and text to a Shanghai calendar day."""
    text = _text(value).strip()
    if not text:
        return ""
    if text.isdecimal() and len(text) == 8:
        try:
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        except ValueError:
            return ""
    if text.isdecimal():
        try:
            stamp = int(text)
            return datetime.fromtimestamp(
                stamp / 1000 if stamp >= 10_000_000_000 else stamp,
                tz=SHANGHAI_TZ,
            ).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return ""
    try:
        return datetime.strptime(text[:10].replace("/", "-"), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def _date_range_key(value) -> str:
    """Compare order ranges by their two dates, ignoring display separators."""
    text = _text(value).strip()
    dates = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text)
    if len(dates) == 2:
        try:
            return " ~ ".join(
                datetime.strptime(day.replace("/", "-"), "%Y-%m-%d").date().isoformat()
                for day in dates
            )
        except ValueError:
            pass
    return " ".join(text.split())


def _append_missing(existing, entries: list[str]) -> str:
    result = _text(existing).replace("\r\n", "\n").strip()
    for entry in entries:
        entry = entry.replace("\r\n", "\n").strip()
        recorded = (result == entry or result.startswith(entry + "\n") or
                    result.endswith("\n" + entry) or "\n" + entry + "\n" in result)
        if entry and not recorded:
            result = f"{result}\n{entry}".strip() if result else entry
    return result


def _remark_descriptions(entries: list[str]) -> list[str]:
    """Keep unique issue descriptions, retaining IDs for unconfirmed follow-ups."""
    descriptions = []
    for entry in entries:
        raw = " ".join(_text(entry).split()).strip(" :：")
        if re.match(r"^订单\s*\d{3}-\d{7}-\d{7}\s*[:：]\s*回访发送未确认", raw):
            if raw not in descriptions:
                descriptions.append(raw)
            continue
        text = re.sub(
            r"(?:订单\s*)?(?<!\d)\d{3}-\d{7}-\d{7}(?!\d)\s*[:：]?\s*",
            "", _text(entry),
        )
        text = " ".join(text.split()).strip(" :：")
        if text and text not in descriptions:
            descriptions.append(text)
    return descriptions


def email_contents(messages: dict[str, str], fallback: str) -> list[str]:
    """Return every configured site message, keeping each site's label."""
    if messages:
        return [f"{site}: {message.strip()}" for site, message in messages.items()
                if message and message.strip()]
    return [fallback.strip()] if fallback and fallback.strip() else []


def _report_table_for_product(product_name: str, default_table_id: str) -> str:
    """Only the 欧规 suffix overrides the configured (美规) report table."""
    eu_suffix = re.search(r"(?:欧规|（\s*欧规\s*）|\(\s*欧规\s*\))$", product_name.strip())
    if not eu_suffix:
        return default_table_id
    eu_table_id = _setting("feishu_eu_table_id", "FEISHU_EU_TABLE_ID")
    if not re.fullmatch(r"tbl[A-Za-z0-9]+", eu_table_id):
        raise FeishuSyncError("欧规回传表 ID 未配置或格式无效")
    return eu_table_id


def build_report(config, attempted: dict[str, set[str]],
                 successful: dict[str, set[str]], failed_records: list,
                 end_reason: str = "") -> dict:
    """Summarize only orders attempted in this execution; historical skips add nothing."""
    names = getattr(config, "product_names", {}) or {}
    messages = email_contents(config.follow_up_messages, config.follow_up_message)
    items = []
    for asin in dict.fromkeys(config.asins):
        orders = attempted.get(asin, set())
        successes = successful.get(asin, set()) & orders
        remarks = []
        explained = set()
        for failure in failed_records:
            if failure.asin != asin:
                continue
            if failure.order_id:
                explained.add(failure.order_id)
            if failure.reason == "回访发送未确认，需人工核对" and failure.order_id:
                remarks.append(f"订单 {failure.order_id}：{failure.reason}")
            else:
                remarks.append(failure.reason)
        if orders - successes - explained:
            remarks.append("执行中断，发送结果未确认")
        if end_reason:
            remarks.append(f"执行结束原因: {end_reason}")
        items.append({
            "asin": asin.upper(),
            "product_name": names.get(asin.upper(), ""),
            "orders": len(orders),
            "success": len(successes),
            "failed": len(orders - successes),
            "messages": messages,
            "remarks": _remark_descriptions(remarks),
        })
    return {
        "date": datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d"),
        "store": config.store_name,
        "date_range": f"{config.start_date} ~ {config.end_date}",
        "pdf_path": config.pdf_path,
        "report_table_id": getattr(config, "feishu_report_table_id", "") or
                           _setting("feishu_report_table_id", "FEISHU_REPORT_TABLE_ID"),
        "product_table_id": getattr(config, "feishu_product_table_id", "") or
                            _setting("feishu_product_table_id", "FEISHU_PRODUCT_TABLE_ID"),
        "product_table_url": getattr(config, "product_table_url", ""),
        "items": items,
        "sync_status": "pending",
    }


class FeishuBitable:
    def __init__(self, app_id: str, app_secret: str, app_token: str = "",
                 report_table_id: str = ""):
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token or _setting("feishu_app_token", "FEISHU_APP_TOKEN")
        report_table_id = report_table_id or _setting(
            "feishu_report_table_id", "FEISHU_REPORT_TABLE_ID")
        if not re.fullmatch(r"tbl[A-Za-z0-9]+", report_table_id):
            raise FeishuSyncError("执行数据回传表 ID 格式无效")
        self.report_table_id = report_table_id
        self.token = ""

    def _json(self, method: str, path: str, payload=None, *, authorized: bool = True) -> dict:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if authorized:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(
            API_ROOT + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=30) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                error = json.loads(exc.read(8192))
            except (ValueError, OSError):
                error = {}
            code = error.get("code", "?")
            message = str(error.get("msg", exc.reason))[:300]
            raise FeishuSyncError(
                f"飞书接口 HTTP {exc.code} ({path.split('?')[0]}): "
                f"飞书错误码 {code}, {message}") from exc
        except (URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise FeishuSyncError(
                f"飞书接口请求失败 ({path.split('?')[0]}): "
                f"{type(reason).__name__}: {str(reason)[:200]}") from exc
        if result.get("code") != 0:
            raise FeishuSyncError(
                f"飞书接口返回错误 ({path.split('?')[0]}): "
                f"{result.get('code')} {result.get('msg', '')}")
        return result

    def _authorize(self) -> None:
        wiki_token = "" if self.app_token else _setting("feishu_wiki_token", "FEISHU_WIKI_TOKEN")
        if not self.app_token and not wiki_token:
            raise FeishuSyncError("请配置飞书 App Token 或 Wiki Token")
        result = self._json("POST", "/auth/v3/tenant_access_token/internal", {
            "app_id": self.app_id, "app_secret": self.app_secret,
        }, authorized=False)
        self.token = result.get("tenant_access_token", "")
        if not self.token:
            raise FeishuSyncError("飞书未返回 tenant_access_token")
        if not self.app_token:
            node = self._json("GET", f"/wiki/v2/spaces/get_node?{urlencode({'token': wiki_token})}")
            self.app_token = (node.get("data", {}).get("node") or {}).get("obj_token", "")
            if not self.app_token:
                raise FeishuSyncError("无法从 Wiki 链接解析多维表格 App Token")

    def _items(self, kind: str, table_id: str = "") -> list[dict]:
        items = []
        page_token = ""
        while True:
            query = {"page_size": 100 if kind == "fields" else 500}
            if page_token:
                query["page_token"] = page_token
            path = (f"/bitable/v1/apps/{self.app_token}/tables/"
                    f"{table_id or self.report_table_id}/{kind}?{urlencode(query)}")
            data = self._json("GET", path).get("data", {})
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token", "")
            if not page_token:
                raise FeishuSyncError(f"飞书 {kind} 分页缺少 page_token")

    def check_access(self, product_table_id: str, include_eu: bool = False) -> tuple[int, ...]:
        """Read field metadata without creating or updating records."""
        if not re.fullmatch(r"tbl[A-Za-z0-9]+", product_table_id):
            raise FeishuSyncError("产品名称映射表 ID 格式无效")
        eu_table_id = _setting("feishu_eu_table_id", "FEISHU_EU_TABLE_ID") if include_eu else ""
        if include_eu and not re.fullmatch(r"tbl[A-Za-z0-9]+", eu_table_id):
            raise FeishuSyncError("欧规回传表 ID 未配置或格式无效")
        self._authorize()
        counts = [len(self._items("fields"))]
        if include_eu:
            counts.append(len(self._items("fields", eu_table_id)))
        counts.append(len(self._items("fields", product_table_id)))
        return tuple(counts)

    def _upload_pdf(self, path: str) -> str:
        pdf = Path(path)
        if not pdf.is_file():
            raise FeishuSyncError(f"PDF 文件不存在: {pdf.name}")
        boundary = f"----FeishuSync{uuid.uuid4().hex}"
        fields = {
            "file_name": pdf.name,
            "parent_type": "bitable_file",
            "parent_node": self.app_token,
            "size": str(pdf.stat().st_size),
        }
        parts = []
        for name, value in fields.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{pdf.name}\"\r\nContent-Type: application/pdf\r\n\r\n".encode("utf-8"))
        parts.extend((pdf.read_bytes(), f"\r\n--{boundary}--\r\n".encode()))
        request = Request(
            API_ROOT + "/drive/v1/medias/upload_all",
            data=b"".join(parts),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=60) as response:
                result = json.load(response)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise FeishuSyncError(f"PDF 上传到飞书失败: {type(exc).__name__}") from exc
        if result.get("code") != 0 or not result.get("data", {}).get("file_token"):
            raise FeishuSyncError(f"PDF 上传到飞书失败: {result.get('code')} {result.get('msg', '')}")
        return result["data"]["file_token"]

    def _product_names(self, table_ref: str) -> dict[str, str]:
        """Read ASIN and product names from a Bitable table ID or saved Wiki link."""
        table_id = table_ref
        if table_ref.startswith("https://"):
            url = urlsplit(table_ref)
            parts = [part for part in url.path.split("/") if part]
            if not url.hostname or not url.hostname.endswith(".feishu.cn"):
                raise FeishuSyncError("产品名称对照表必须使用飞书 HTTPS 链接")
            if (len(parts) < 2 or parts[0] != "wiki" or
                    parts[1] != _setting("feishu_wiki_token", "FEISHU_WIKI_TOKEN")):
                raise FeishuSyncError("产品名称对照表必须属于当前飞书多维表格")
            table_id = (parse_qs(url.query).get("table") or [""])[0]
        if not re.fullmatch(r"tbl[A-Za-z0-9]+", table_id):
            raise FeishuSyncError("产品名称映射表 ID 格式无效")

        names = {}
        for record in self._items("records", table_id):
            fields = record.get("fields") or {}
            asin = _text(fields.get("ASIN") or fields.get("产品ASIN")).strip().upper()
            name = _text(fields.get("产品名称")).strip()
            if asin and name:
                if asin in names and names[asin] != name:
                    raise FeishuSyncError(f"产品名称对照表中 ASIN 重复且名称不同: {asin}")
                names[asin] = name
        return names

    def sync(self, report: dict) -> tuple[int, int]:
        self._authorize()
        asins = [item["asin"].strip().upper() for item in report["items"]]
        if len(asins) != len(set(asins)):
            raise FeishuSyncError("本次执行报告中存在重复 ASIN")
        table_ref = (report.get("product_table_id") or report.get("product_table_url") or
                     report.get("product_sheet_url") or
                     os.environ.get("FEISHU_PRODUCT_TABLE_URL") or
                     _setting("feishu_product_table_id", "FEISHU_PRODUCT_TABLE_ID")).strip()
        if any(not item["product_name"].strip() for item in report["items"]):
            names = self._product_names(table_ref)
            for item in report["items"]:
                if not item["product_name"].strip():
                    item["product_name"] = names.get(item["asin"].strip().upper(), "")
        missing_names = [item["asin"] for item in report["items"]
                         if not item["product_name"].strip()]
        if missing_names:
            raise FeishuSyncError(
                f"飞书回传前请先配置产品名称: {', '.join(missing_names)}")

        groups: dict[str, list[dict]] = {}
        for item in report["items"]:
            table_id = _report_table_for_product(item["product_name"], self.report_table_id)
            groups.setdefault(table_id, []).append(item)

        prepared = [self._prepare_table(report, table_id, items)
                    for table_id, items in groups.items()]
        created = updated = 0
        for table_id, items, schema, records, report_date, report_range in prepared:
            added, changed = self._write_table(
                report, table_id, items, schema, records, report_date, report_range)
            created += added
            updated += changed
        return created, updated

    def _prepare_table(self, report: dict, table_id: str, items: list[dict]) -> tuple:
        schema = {item.get("field_name"): item.get("type")
                  for item in self._items("fields", table_id)}
        missing = [name for name in FIELDS if name not in schema]
        if missing:
            raise FeishuSyncError(f"飞书表格缺少字段: {', '.join(missing)}")
        for name in ("订单数", "发送成功", "发送失败"):
            if schema[name] != 2:
                raise FeishuSyncError(f"飞书字段“{name}”必须是数字类型")
        for name in ("店铺", "产品名称", "ASIN", "订单日期范围", "邮件内容", "备注"):
            if schema[name] != 1:
                raise FeishuSyncError(f"飞书字段“{name}”必须是文本类型")
        if schema["发送日期"] not in (1, 5):
            raise FeishuSyncError("飞书字段“发送日期”须为文本或日期类型")
        if schema["PDF文档"] != 17:
            raise FeishuSyncError("飞书字段“PDF文档”须为附件类型，才能上传 PDF 文件")

        report_date = _date_key(report["date"])
        if not report_date:
            raise FeishuSyncError("本次执行报告的发送日期无效")
        report_range = _date_range_key(report["date_range"])
        if not report_range:
            raise FeishuSyncError("本次执行报告的订单日期范围为空")
        item_keys = [(report["store"].strip().casefold(), item["asin"].strip().upper(),
                      report_date, report_range) for item in items]
        if len(item_keys) != len(set(item_keys)):
            raise FeishuSyncError("本次执行报告中存在重复 ASIN")

        records = {}
        target_pairs = {(store, asin) for store, asin, _, _ in item_keys}
        for record in self._items("records", table_id):
            fields = record.get("fields") or {}
            pair = (_text(fields.get("店铺")).strip().casefold(),
                    _text(fields.get("ASIN")).strip().upper())
            if pair not in target_pairs:
                continue
            sent_date = _date_key(fields.get("发送日期"))
            if sent_date != report_date:
                continue
            order_range = _date_range_key(fields.get("订单日期范围"))
            if order_range != report_range:
                continue
            key = (*pair, sent_date, order_range)
            if key in records:
                raise FeishuSyncError(
                    f"飞书表格中同店铺+ASIN+发送日期+订单日期范围重复: "
                    f"{key[1]} {sent_date} {order_range}")
            records[key] = record
        if any(key not in records for key in item_keys) and not Path(report["pdf_path"]).is_file():
            raise FeishuSyncError("新建飞书记录前 PDF 文件必须存在")

        return table_id, items, schema, records, report_date, report_range

    def _write_table(self, report: dict, table_id: str, items: list[dict],
                     schema: dict, records: dict, report_date: str,
                     report_range: str) -> tuple[int, int]:
        created = updated = 0
        base = f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records"
        for item in items:
            key = (report["store"].strip().casefold(), item["asin"].strip().upper(),
                   report_date, report_range)
            remarks = _remark_descriptions(item["remarks"])
            old = records.get(key)
            if old:
                current = old.get("fields") or {}
                existing_remarks = _remark_descriptions(
                    _text(current.get("备注")).splitlines())
                fields = {
                    "订单数": _count(current.get("订单数")) + _count(item["orders"]),
                    "发送成功": _count(current.get("发送成功")) + _count(item["success"]),
                    "发送失败": _count(current.get("发送失败")) + _count(item["failed"]),
                    "邮件内容": _append_missing(current.get("邮件内容"), item["messages"]),
                    "备注": _append_missing("\n".join(existing_remarks), remarks),
                }
                self._json("PUT", f"{base}/{old['record_id']}", {"fields": fields})
                updated += 1
                continue

            sent_date = report_date
            if schema["发送日期"] == 5:
                sent_date = int(datetime.combine(
                    datetime.strptime(report_date, "%Y-%m-%d").date(), time.min,
                    tzinfo=SHANGHAI_TZ,
                ).timestamp() * 1000)
            pdf_value = [{"file_token": self._upload_pdf(report["pdf_path"])}]
            fields = {
                "发送日期": sent_date,
                "店铺": report["store"],
                "产品名称": item["product_name"],
                "ASIN": item["asin"],
                "订单日期范围": report["date_range"],
                "订单数": _count(item["orders"]),
                "发送成功": _count(item["success"]),
                "发送失败": _count(item["failed"]),
                "邮件内容": _append_missing("", item["messages"]),
                "PDF文档": pdf_value,
                "备注": _append_missing("", remarks),
            }
            self._json("POST", base, {"fields": fields})
            created += 1
        return created, updated


def sync_report(report: dict, app_id: str = "", app_secret: str = "") -> tuple[int, int]:
    app_id = (app_id or os.environ.get("FEISHU_APP_ID", "")).strip()
    app_secret = (app_secret or os.environ.get("FEISHU_APP_SECRET", "")).strip()
    if not app_id or not app_secret:
        raise FeishuSyncError("请在基础环境配置中填写飞书 App ID 和 App Secret")
    return FeishuBitable(
        app_id, app_secret, _setting("feishu_app_token", "FEISHU_APP_TOKEN"),
        report_table_id=report.get("report_table_id") or
                        _setting("feishu_report_table_id", "FEISHU_REPORT_TABLE_ID"),
    ).sync(report)


def sync_saved_report(path: str | Path, app_id: str = "", app_secret: str = "") -> tuple[int, int]:
    """Sync a locally saved report once; never automatically replay an ambiguous write."""
    report_path = Path(path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("sync_status") == "success":
        raise FeishuSyncError("此执行报告已经同步成功，禁止重复累加")
    created, updated = sync_report(report, app_id=app_id, app_secret=app_secret)
    report["sync_status"] = "success"
    report["synced_at"] = datetime.now().isoformat(timespec="seconds")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return created, updated


def save_run_report(report: dict, output_dir: str | Path) -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"飞书同步_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync one saved execution report to Feishu")
    parser.add_argument("report", nargs="?", help="Path to an output/飞书同步_*.json report")
    parser.add_argument("--check", action="store_true", help="只读检查两张表的访问权限")
    args = parser.parse_args()
    settings = _local_settings()
    if args.check:
        app_id = settings.get("feishu_app_id") or os.environ.get("FEISHU_APP_ID", "")
        app_secret = settings.get("feishu_app_secret") or os.environ.get("FEISHU_APP_SECRET", "")
        if not app_id or not app_secret:
            parser.error("请在基础环境配置中填写飞书 App ID 和 App Secret")
        client = FeishuBitable(
            app_id, app_secret, _setting("feishu_app_token", "FEISHU_APP_TOKEN"),
            report_table_id=_setting("feishu_report_table_id", "FEISHU_REPORT_TABLE_ID"))
        counts = client.check_access(
            _setting("feishu_product_table_id", "FEISHU_PRODUCT_TABLE_ID"), include_eu=True)
        print(f"只读检查通过：美规回传表 {counts[0]} 个字段，"
              f"欧规回传表 {counts[1]} 个字段，产品映射表 {counts[2]} 个字段")
    else:
        if not args.report:
            parser.error("请提供要同步的报告文件，或使用 --check")
        print(sync_saved_report(args.report, app_id=settings.get("feishu_app_id", ""),
                                app_secret=settings.get("feishu_app_secret", "")))
