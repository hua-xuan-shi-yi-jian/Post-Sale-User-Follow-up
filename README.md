# 亚马逊卖家后台回访信息发送

一个面向已购订单的 Windows 桌面工具：按 ASIN 和日期筛选订单，逐单发送说明书 PDF，并记录发送与回访结果，减少手工重复查单、上传附件和核对状态的工作。

> **隐私说明：** 公开源码只提供不含真实业务值的示例配置。真实账号、飞书标识、订单记录、日志和产品 PDF 请保留在本机，不要提交到仓库。

## 能做什么

- 通过紫鸟本地 API 打开指定店铺，再用 Playwright 的 CDP 连接到该店铺浏览器。
- 在 Seller Central 按 ASIN、日期和销售渠道筛选订单，只处理符合条件的订单。
- 在联系买家页面发送说明书 PDF，在消息页按订单号查找对话并发送回访文本。
- 按店铺保存已处理、发送结果未确认、联系主题禁用和待人工回访的订单状态，避免直接重发不确定订单。
- 将本次尝试、成功和失败汇总到本地报告与失败 CSV，并尝试同步飞书多维表格；同步失败时保留待核对报告。

## 数据流

`Tkinter 表单 / config.json` → `紫鸟 API 打开店铺` → `Playwright CDP` → `订单筛选与发送` → `消息页复核及回访` → `output/ 本地记录` → `飞书多维表格`。

程序在 `task_models.py` 定义任务与运行状态，在 `amazon_selectors.py` 维护页面选择器，主流程在 `amazon_bulk_send.py`，飞书同步在 `feishu_sync.py`。`ziniao_amz2.0/` 提供登录和站点切换模块。

## 技术选型

| 技术 | 在项目中的用途与取舍 |
| --- | --- |
| Python 3.11+ | 主流程、数据模型、JSON/CSV 持久化与测试共用一种语言；标准库已覆盖大部分非浏览器需求。 |
| Playwright + CDP | 直接接入紫鸟已打开的 Chromium 店铺环境；相较为 Selenium 单独维护驱动与会话，更贴合当前连接方式，也能用 Locator 重新解析动态 DOM。页面选择器仍需随 Seller Central 改版维护。 |
| Tkinter | Windows 本地表单和运行日志无需部署 Web 服务；界面能力较 Web 前端简单，但符合单机操作场景。 |
| 紫鸟本地 API | 根据店铺名称打开隔离浏览器并取得调试端口；项目因此依赖已安装且可登录的紫鸟，不是独立浏览器。 |
| 飞书开放 API + `urllib` | 直接处理表格读写、附件上传和 HTTP 错误，不增加飞书 SDK 依赖；代价是 API 协议与字段校验由本项目维护。 |

这些是根据当前实现解释的技术取舍，不代表所有替代方案都经过实测对比。

## 快速开始（Windows PowerShell）

前提：Windows、Python 3.11+、可用的紫鸟店铺与 Amazon Seller Central 账号。飞书回传还需要应用凭据、目标多维表格权限和表结构；没有这些外部资源只能运行离线测试，不能完成真实发送。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .\ziniao_amz2.0
Copy-Item .\examples\config.example.json .\config.json
.\.venv\Scripts\python.exe -m unittest test_project test_feishu_sync
.\.venv\Scripts\python.exe .\amazon_bulk_send.py
```

离线测试是第一个可复现的示例，不会连接店铺或发送邮件。打开 GUI 后，填写紫鸟登录信息、店铺、ASIN、日期范围、自己的 PDF、回访文本、飞书应用凭据、美规回传表 ID 和产品映射表 ID。若要同步名称以“欧规”结尾的产品，还需在本机 `config.json` 填写 `feishu_eu_table_id`；飞书多维表格的 `feishu_app_token` 与 `feishu_wiki_token` 至少配置一个，也可分别使用 `FEISHU_APP_TOKEN`、`FEISHU_WIKI_TOKEN` 环境变量。完整空值字段见[配置样例](examples/config.example.json)。这些值会以**明文**保存在本机 `config.json`，此文件已被 Git 忽略；请限制本机访问并自行备份。

**发送前务必确认：GUI 的“干跑”仍会真正发送 PDF，只在发送回访文本前停止；它不是零发送的沙盒。** 初次运行请使用授权的测试店铺与测试订单，并在发送前核对收件对象和附件。程序还会将订单号、买家信息、日志和同步报告写入 `output/`，不要把该目录上传或用作未经脱敏的截图。

飞书表格须具备 `发送日期`、`店铺`、`产品名称`、`ASIN`、`订单日期范围`、`订单数`、`发送成功`、`发送失败`、`邮件内容`、`PDF文档`、`备注` 字段；产品映射表须提供 ASIN 与产品名称。同步失败后先核对远端是否写入，再手动重试本地待同步报告，避免重复累加。

## 测试与打包

```powershell
.\.venv\Scripts\python.exe -m unittest test_project test_feishu_sync
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm "亚马逊批量发送邮件.spec"
```

打包需要 `packaging_hooks/` 与图标文件。生成的 `build/`、`dist/`、`.exe` 均不进入源码仓库。

## 文档与许可证

- [技术文章草稿](docs/TECH_BLOG.md)
- [配置样例](examples/config.example.json)

许可证建议：在确认代码、图标及 `ziniao_amz2.0/` 的再分发权利后，若希望允许商业使用，可选择 MIT 并添加正式 `LICENSE`；在权利未核清前暂不声明开源许可证。README 中未把“可查看源码”误写成“已获开源授权”。
