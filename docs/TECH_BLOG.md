# 我把“按订单发说明书”做成了桌面任务：紫鸟、Playwright 和飞书如何串起来

> 发布前待补：脱敏截图、同批任务的人工计时。人工每单 1–2 分钟是操作经验估算；脚本数据取自执行日志，本文不把两种口径写成实测效率提升。

## 1. 为什么做

这个项目面对的不是“点一次发送”这么简单的动作。一个 ASIN 可能对应多页订单，操作员要筛日期和销售渠道、进入每个买家的联系页、上传正确的 PDF、再到消息页确认对话并发回访。只要中途页面没加载完、订单切换错了，或者上次发送结果不确定，手工继续操作就容易漏掉或重复处理。

我把这条重复链路放进一个 Windows 桌面任务里：输入店铺、ASIN、日期、PDF 和回访内容，程序逐单运行，并把“已确认”“未确认”“需要人工处理”分开记录。人工操作经验是每单约 1–2 分钟；**还没有同批订单的人工计时，不能据此宣称脚本节省了多少时间。**

## 2. 输入到输出：没有魔法的一条流水线

`Tkinter 表单 / 本地 config.json` → `紫鸟 API 查找并打开店铺` → `返回调试端口` → `Playwright connect_over_cdp` → `Seller Central 筛选订单` → `逐单上传 PDF` → `消息页搜索和回访` → `output/ 状态、失败 CSV、待同步报告` → `飞书多维表格`。

`TaskConfig` 集中本次任务输入，`TaskStatus`/`RunState` 让 GUI 能显示任务阶段；`run_task()` 负责顺序编排，`amazon_selectors.py` 收拢选择器，`feishu_sync.py` 处理独立的回传边界。这里没有刻意套一整套 GoF 类图：实际使用的是**状态对象**、按优先级的**选择器回退**，以及“本地报告先落盘、再尝试远端写入”的**待同步报告**做法。它与事务性 outbox 有相似意图，但本地文件和远端飞书之间没有数据库事务，因此不能宣称端到端恰好一次。

## 3. 三个最值得展开的模块

### 3.1 接入现有店铺浏览器，而不是新开一个空白会话

入口先用紫鸟本地 API 精确查找店铺，调用 `start_browser` 获得 `debuggingPort`。接下来是 `ziniao_browser.py` 里的真实连接代码：

```python
def connect_to_store_cdp(debugging_port: int) -> tuple:
    """
    通过 CDP (Chrome DevTools Protocol) 连接到紫鸟店铺浏览器。

    参数：
        debugging_port: startBrowser 返回的调试端口

    返回：
        (playwright, browser, context, page) 元组
    """
    cdp_url = f"http://127.0.0.1:{debugging_port}"
    print(f"[ziniao] CDP 连接: {cdp_url}")

    pw = sync_playwright().start()
    br = pw.chromium.connect_over_cdp(cdp_url)
    contexts = br.contexts
    if not contexts:
        raise RuntimeError("未找到浏览器上下文")
    ctx = contexts[0]
    pages = ctx.pages
    pg = pages[-1] if pages else ctx.new_page()
    print(f"[ziniao] CDP 连接成功: {pg.url}")
    return pw, br, ctx, pg
```

这样可以在店铺隔离环境里继续操作；如果没有浏览器上下文就明确报错。连接不等于绕过登录或安全验证，主流程仍会检查 Seller Central 状态，必要时等人工处理。使用已有会话也意味着本机紫鸟和账号权限是真实运行的前提，不能靠 `pip install` 构造一个完整演示。

### 3.2 消息页改版后，选择器“能命中”仍可能命中错地方

项目记录过一个很具体的回归：消息搜索框变成 `kat-input` 自定义组件，`placeholder` 留在组件上，真实输入框进了 shadow DOM；旧的宽泛兜底却命中了顶部全局搜索框。现在 `amazon_selectors.py` 将候选项限制在消息搜索组件，找不到就告警退出：

```python
    "message_search_input": (
        "kat-input[placeholder*='订单编号'] >> input",
        "input[placeholder*='订单编号']",
        "kat-input[placeholder*='电子邮件地址'] >> input",
        "input[placeholder*='电子邮件地址']",
        "[unique-id='katal-id-0'] >> input",
        "[unique-id='katal-id-0']",
    ),
```

`amazon_bulk_send.py` 的 `safe_find()` 顺序尝试这些候选项；它是“优先级回退”，不是把所有 `input` 都当成安全目标。消息页本身是 SPA，搜索结果列表、遮罩和对话输入框分阶段出现，所以流程还会等待遮罩消失、按订单号匹配结果并打开对话，再确认后续所需的输入区。失败时有有界重试和日志，而不是一次 `count()` 就断言邮件没发出去。这里踩过的另一个坑是 `page.locator()` 只收一个选择器字符串；把 Python 列表直接传进去会变成非法选择器，代码改为 CSS 并集字符串，并在 SPA 重渲染后重新获取 Locator。

项目保留的 2026 年 8 月 17 日旧版日志把这个问题量化了：一次运行开始处理 14 单，程序记录 14 次“邮件发送成功”，但消息页仅 3 次验证成功、11 次验证失败。紧接着的第二次运行处理 0 单，跳过 14 条历史记录。PDF 发送确认后就会保存已处理订单号，因此消息页验证失败不会触发自动重发 PDF。这是旧版故障与防重边界的证据，不能当作当前版本成功率或 Amazon 最终投递结果。

### 3.3 发送状态与飞书回传，重点是“不确定时别自动重发”

主流程将已发送、发送未确认和待人工回访的订单分别保存到 `output/`，每个店铺用独立文件；运行日志与失败 CSV 也在这个目录。飞书回传先生成 `sync_status: pending` 的本地 JSON 报告，随后才调用 API。`feishu_sync.py` 对成功报告的重试边界是这样的：

```python
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
```

这里的关键限制也要说清：远端已经写入但本地在响应前超时，报告可能仍是 `pending`。因此不能看到 `pending` 就机械重跑；先核对飞书表格，再决定是否重试，否则累加统计可能重复。HTTP 状态码、飞书错误码和错误信息会被包装成可排查的异常；本地报告保留了继续核对的入口。另一方面，GUI 会把紫鸟密码和飞书 App Secret 明文保存在忽略的 `config.json`，这是当前实现的安全边界，不应被包装成加密配置方案。

## 4. 技术选型：按现有约束复盘

以下比较基于代码可以确认的约束，**不表示当年逐个方案都做过完整实验**。

| 方案 | 在这个项目里的优势 | 代价 / 未采用原因 |
| --- | --- | --- |
| Playwright + CDP（当前） | 可附着紫鸟店铺 Chromium；Locator 适合动态页面重解析。 | 依赖本地调试端口，Amazon DOM 变化仍会破坏选择器。 |
| Selenium + 现有浏览器 | 也能做浏览器自动化，生态成熟。 | 本项目现有接入代码和重试方式已围绕 Playwright/CDP，切换需重新适配会话及选择器。 |
| Tkinter（当前） | 交付单机 Windows GUI，不用部署服务。 | 跨平台界面和复杂交互能力有限。 |
| Web 前端 + 后端服务 | 适合多人协作、集中管理。 | 对当前本地店铺操作增加部署、鉴权与状态管理成本。 |
| `urllib` 调飞书 API（当前） | 依赖少，HTTP 错误与表结构校验在源码中明确可见。 | API 细节与字段变更需自行维护；SDK 可能减少部分协议样板。 |

## 5. 效果展示：执行日志与人工估算

我按日志内每次“启动时间”切分任务，读取同一次任务的结果汇总。下面是最近三次**同日结束、有非零处理量和完整汇总**的运行；跨夜、被终止或缺少结果汇总的运行不在表中。失败订单通常耗在重试和容错等待上，因此整次运行的墙上耗时不能代表成功订单的执行耗时，这里不拿它与人工估算比快慢。

| 启动时间（日志本地时间） | 本次开始处理 | 程序汇总成功 | 程序汇总失败 | 人工估算（仅成功单数，1–2 分钟/单） | 脚本成功单耗时 |
| --- | ---: | ---: | ---: | ---: | --- |
| 2026-11-09 16:05 | 19 | 13 | 6 | 13–26 分钟 | 日志未记录 |
| 2026-11-10 10:04 | 37 | 16 | 21 | 16–32 分钟 | 日志未记录 |
| 2026-11-10 17:22 | 11 | 0 | 11 | 不适用 | 无成功样本 |

“处理”不包含历史跳过项；“成功/失败”照录程序汇总，不等于 Amazon 最终投递结果。人工栏只按提供的经验值乘以程序成功单数，是量级参考，不是这几批订单的人工实测，也没有计入失败单的人工处理。三次运行不能相加成去重订单量。

当前日志只有任务起止时间，没有逐单时间戳或单单耗时；这些完整运行也都含失败订单。**不能把整次耗时除以成功单数，也不能凭重试次数猜出该扣掉多少分钟。** 因此脚本成功路径耗时、节省时间和提升百分比仍未测得。

去重路径同样有可核对记录：11 月 11 日一次运行处理 0 单，日志分别记下 47 单历史已处理、32 单联系主题已禁用并跳过。这说明程序执行了跳过规则，不代表这些订单在本次成功发送。

要做可比的效率对照，下一次需要记录每单开始、成功确认和失败重试各自的时间，并对同批、同完成标准的人工操作计时。原始日志含业务信息，不随文章公开；脱敏截图/GIF也在发布前补齐。

## 6. 源码与下一步

源码仓库地址：[Post-Sale-User-Follow-up](https://github.com/hua-xuan-shi-yi-jian/Post-Sale-User-Follow-up)。公开版只收录脱敏源码与空值配置样例；真实账号、业务标识和执行日志留在本机。仓库暂未附开源许可证，源码可查看不等于已获复用授权。

下一步计划是给 GUI 增加真正不发送的预览模式，并补上脱敏运行截图、成功路径逐单计时与同批人工计时。它们是计划，不是现有功能。若这套“先确认状态、再处理重试”的思路对你有用，欢迎点赞、收藏，也欢迎查看仓库中的测试与实现细节。
