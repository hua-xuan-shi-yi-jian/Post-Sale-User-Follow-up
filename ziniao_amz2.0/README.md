# ziniao_amz2.0

本目录是项目内使用的紫鸟浏览器与 Amazon Seller Central 辅助包。根目录的 `amazon_bulk_send.py` 使用其中的登录、站点切换和站点常量；该包还包含独立的浏览器与付款模块，批量发送流程不调用付款模块。

## 安装

在项目根目录执行：

```powershell
py -3.11 -m pip install -e .\ziniao_amz2.0
```

主程序通过 Playwright CDP 连接紫鸟打开的店铺浏览器，不会使用 Playwright 新建的空白浏览器会话。运行前仍需准备紫鸟账号、店铺权限和 Amazon Seller Central 访问权限。完整配置与启动步骤见根目录 `README.md`。

## 代码位置

- `a2autolib2/amazon/login.py`：登录状态检查与登录处理。
- `a2autolib2/amazon/marketplace.py`：账户选择页的站点切换。
- `a2autolib2/amazon/constants.py`：站点域名和页面常量。
- `a2autolib2/browser/`：紫鸟本地 API、进程启动和 CDP 封装。
- `a2autolib2/common/`：配置与日志辅助函数。
- `a2autolib2/amazon/payment.py`：独立付款处理模块，当前批量发送入口不使用。
