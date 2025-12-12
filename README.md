# xhs-api（小红书笔记解析服务）

这是一个基于 FastAPI 的简单 HTTP 服务，用于在 Docker 环境中通过 **Chromium（无头）** 打开小红书笔记页面，并从页面脚本里提取初始数据（`__INITIAL_STATE__` / `__NEXT_DATA__`），再解析出笔记信息。

> 注意：小红书存在风控/登录/反爬策略。在触发风控时，页面可能不会返回笔记数据，本服务会返回 400 并给出更明确的诊断信息。

## 接口说明

### POST `/scrape`

- **请求**

```json
{
  "url": "https://www.xiaohongshu.com/explore/64ec28ee000000001f01476d"
}
```

- **成功返回**

```json
{
  "status": "success",
  "data": {
    "title": "xxx",
    "desc": "xxx",
    "type": "normal",
    "image_list": ["https://..."],
    "user": "作者昵称",
    "raw_url": "原始URL"
  }
}
```

- **失败返回（HTTP 400）**
当无法从页面提取到初始数据（例如风控、未登录、页面结构变化）时，返回：

```json
{
  "detail": "错误原因（包含页面标题、script预览、HTML预览）"
}
```

## 本地调用示例

```bash
curl -X POST "http://localhost:8000/scrape" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.xiaohongshu.com/explore/64ec28ee000000001f01476d"}'
```

## 登录态（绕过“需要登录/风控弹层”的关键）

当你看到错误里包含 `reds-lock-scroll` 或提示“疑似触发登录/风控弹层”，说明页面只返回“壳页面”，**必须带登录态**才能拿到笔记数据。

本项目支持通过 **环境变量注入 cookies**（不改变接口参数）：

- **XHS_COOKIES_JSON**：JSON 字符串（推荐，支持浏览器导出的 cookies 列表/对象）
- **XHS_COOKIES**：普通 Cookie 字符串（形如 `a=b; c=d`）
- **XHS_USER_DATA_PATH**：Chromium 用户数据目录（可挂载 volume 持久化登录态）
- **XHS_USER_PROFILE**：Profile 名称（可选，默认 `Default`，建议不要改）
- **XHS_HEADLESS**：默认 `1`（无头）；设置为 `0` 可用于调试（需要你自己的容器图形方案）
- **XHS_COOKIES_FILE**：可选。cookies 持久化文件路径（默认：`$XHS_USER_DATA_PATH/xhs_cookies.json`）。服务会在扫码成功后把 cookies 写到这里，`/scrape` 会自动加载并注入。

### 方式 A：注入 cookies（推荐，最简单）

1) 在你电脑的浏览器里登录小红书后，导出 `www.xiaohongshu.com` 的 cookies  
2) 把导出的 JSON 内容粘贴到环境变量 `XHS_COOKIES_JSON`

示例（把 `<你的JSON>` 换成你自己的）：

```bash
docker run --rm -p 8000:8000 \
  -e XHS_COOKIES_JSON='<你的JSON>' \
  xhs-api
```

### 方式 B：持久化浏览器 Profile（适合长期运行）

把用户数据目录挂载出来，后续可以复用登录态：

```bash
docker run --rm -p 8000:8000 \
  -e XHS_USER_DATA_PATH=/data/chrome \
  -v $(pwd)/chrome-data:/data/chrome \
  xhs-api
```

## 无 GUI 环境扫码登录（二维码截图接口）

当你不方便在容器里打开可交互浏览器时，可以用下面的接口“拿二维码截图 → 手机扫码 → 等待登录完成”。

1）获取二维码截图（保存成 png）：

```bash
curl -o xhs_login_qr.png "http://localhost:8000/login/qr"
```

如果生成的图片里没有二维码，说明当前页面没有弹出登录弹窗。你可以把 `url` 改成你抓取失败的笔记链接，
更容易触发登录/风控弹层（二维码一般会在弹层里）：

```bash
curl -o xhs_login_qr.png "http://localhost:8000/login/qr?url=https://www.xiaohongshu.com/explore/64ec28ee000000001f01476d"
```

2）用手机小红书 App 扫描 `xhs_login_qr.png` 里的二维码并确认登录。

3）等待登录完成（最多等 120 秒）：

```bash
curl "http://localhost:8000/login/wait?timeout=120"
```

返回 `status=success` 后，登录态会写入你挂载的 `XHS_USER_DATA_PATH` 目录；然后就可以继续调用 `/scrape`。

如果你扫码后长时间还是 `status=waiting`，通常意味着二维码过期或未确认登录，重新获取一次 `/login/qr` 即可。
如果返回里看到 `cookies_count` 已经有值但仍是 `waiting`，说明“登录态可能已写入但弹窗未自动关闭/页面未刷新”，
此版本会在服务端自动尝试关闭弹窗并刷新页面后再确认。
如果仍然 `waiting`，说明“cookie 写入≠笔记可访问”。服务端会用你传入的笔记链接做验证：只有当能从笔记页提取到详情数据，
才会判定登录成功。

## 常见问题（排障）

### 1）返回 “数据结构中未找到笔记详情…”
这通常意味着：页面初始数据里没有包含 `note` 详情，常见原因是 **触发风控/需要登录**。

### 2）返回 “无法从页面脚本中提取初始数据…”
常见原因：
- 页面结构变化（脚本不再包含 `__INITIAL_STATE__`）
- 被风控拦截，返回的是通用页面

另外一种更常见的情况是：页面只返回“壳页面 + 外链脚本”，数据需要等 JS 执行后才会挂到 `window.__INITIAL_STATE__`
或插入 `id="__NEXT_DATA__"` 的 script 标签中。本项目已优先从运行时读取这两种数据；如果仍为空，基本可以判断为
**需要登录/风控/验证码**。

错误信息里会包含 `页面标题`、`script预览`、`HTML预览`，你可以把这些片段发给我，我就能进一步判断是结构变化还是风控导致。


# xhs-api
