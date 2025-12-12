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
