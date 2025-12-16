"""
XHS-API Service
---------------
基于 FastAPI 和 DrissionPage 构建的高性能小红书采集服务。

核心特性：
1. **高性能自动化**：利用 DrissionPage 直接控制浏览器内核，兼具 requests 的速度和 selenium 的渲染能力。
2. **智能数据提取**：多维度的提取策略（JSON变量解析 -> 平衡括号匹配 -> 正则兜底），极大提高成功率。
3. **登录态持久化**：支持扫码登录并自动维护 Cookie 池，通过 Docker Volume 实现重启不掉线。
4. **易于集成**：提供标准的 RESTful API，可轻松接入 n8n、Dify 或其他工作流系统。

Author: Your Name
Version: 1.0.0
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel
from DrissionPage import ChromiumPage, ChromiumOptions
import json
import uvicorn
import re
import os
import time
import threading
import os.path
from typing import Any, Optional, Dict

app = FastAPI(
    title="XHS Scraper API",
    description="基于 DrissionPage 的小红书无头浏览器采集服务",
    version="1.0.0"
)

@app.get("/")
def root():
    """服务健康检查与简介"""
    return {
        "service": "XHS-API Scraper",
        "status": "running",
        "docs": "/docs",
        "powered_by": ["FastAPI", "DrissionPage"]
    }

class URLItem(BaseModel):
    url: str

# ----------------------------
# 登录会话管理 / Login Session
# ----------------------------

_LOGIN_LOCK = threading.Lock()
_LOGIN_PAGE: Optional[ChromiumPage] = None
_LOGIN_CREATED_AT: float = 0.0
_LOGIN_LAST_URL: str = ""

def _extract_initial_state_expr(script_text: str) -> Optional[str]:
    """从 script 文本中提取 __INITIAL_STATE__ 的赋值表达式（可能是 JSON 或 JSON.parse(...)）。"""
    patterns = [
        # window.__INITIAL_STATE__ = ...
        r'window\.__INITIAL_STATE__\s*=\s*(.*?);',
        # window["__INITIAL_STATE__"] = ... / window['__INITIAL_STATE__'] = ...
        r'window\[(?:"__INITIAL_STATE__"|\'__INITIAL_STATE__\')\]\s*=\s*(.*?);',
    ]
    for pat in patterns:
        m = re.search(pat, script_text, flags=re.DOTALL)
        if m:
            return m.group(1).strip()
    return None


def _parse_initial_state_expr(expr: str) -> dict[str, Any]:
    """把 __INITIAL_STATE__ 的赋值表达式解析成 dict。"""
    # 兼容 undefined
    expr = expr.replace("undefined", "null").strip()

    # 兼容 JSON.parse("...") / JSON.parse('...')
    m = re.match(r"JSON\.parse\(\s*([\"'])(.*)\1\s*\)\s*$", expr, flags=re.DOTALL)
    if m:
        raw = m.group(2)
        # JS 字符串转义 -> Python 字符串
        try:
            unescaped = bytes(raw, "utf-8").decode("unicode_escape")
        except Exception:
            # decode 失败时退化为原始字符串尝试 json.loads
            unescaped = raw
        return json.loads(unescaped)

    # 直接 JSON
    return json.loads(expr)


def _try_get_initial_data_from_runtime(page: ChromiumPage, timeout_sec: float = 8.0) -> Optional[dict[str, Any]]:
    """等待页面 JS 执行完成后，从运行时环境提取初始数据。

    说明：部分站点不会把数据直接写进 HTML 内联 script，而是通过外链脚本执行后挂到 window 上，
    或者把 Next.js 的数据放在 id="__NEXT_DATA__" 的 script 标签中（可能在稍后插入）。
    """
    step = 0.5
    tries = max(1, int(timeout_sec / step))

    for _ in range(tries):
        # 1) window.__INITIAL_STATE__（序列化为字符串再解析，避免 run_js 返回复杂对象类型差异）
        try:
            state_json = page.run_js(
                'return (window.__INITIAL_STATE__ ? JSON.stringify(window.__INITIAL_STATE__) : "");'
            )
            if isinstance(state_json, str) and state_json.strip():
                return json.loads(state_json)
        except Exception:
            pass

        # 2) __NEXT_DATA__（Next.js）
        try:
            next_json = page.run_js(
                'var e=document.getElementById("__NEXT_DATA__"); return e? (e.textContent || "") : "";'
            )
            if isinstance(next_json, str) and next_json.strip():
                return json.loads(next_json)
        except Exception:
            pass

        page.wait(step)

    return None


def _parse_cookie_text(cookie_text: str) -> dict[str, str]:
    """把 'a=b; c=d' 形式的 Cookie 字符串解析为 dict。"""
    result: dict[str, str] = {}
    for part in cookie_text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k:
            result[k] = v
    return result


def _extract_object_by_balance(text: str, start_marker: str) -> Optional[str]:
    """从文本中提取平衡的大括号对象字符串。"""
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return None
    
    # 找到第一次出现 { 的位置
    brace_start = text.find("{", start_idx)
    if brace_start == -1:
        return None
        
    count = 0
    # 简单的括号计数，未处理字符串内的括号，但对于 __INITIAL_STATE__ 这种大对象通常足够
    for i in range(brace_start, len(text)):
        char = text[i]
        if char == "{":
            count += 1
        elif char == "}":
            count -= 1
            if count == 0:
                return text[brace_start:i+1]
    return None


def _load_xhs_cookies_from_env() -> Optional[object]:
    """从环境变量读取 cookies。

    支持：
    - XHS_COOKIES_JSON：JSON 字符串（可以是 dict 或 list，建议直接粘贴浏览器导出的 cookies.json）
    - XHS_COOKIES：普通 Cookie 字符串，如 'a=b; c=d'
    """
    cookies_json = os.getenv("XHS_COOKIES_JSON", "").strip()
    if cookies_json:
        try:
            return json.loads(cookies_json)
        except Exception as e:
            raise Exception(f"环境变量 XHS_COOKIES_JSON 不是合法 JSON：{e}")

    cookies_text = os.getenv("XHS_COOKIES", "").strip()
    if cookies_text:
        return _parse_cookie_text(cookies_text)

    return None


def _cookies_file_path() -> str:
    """cookies 持久化文件路径（用于在容器内复用登录态）。"""
    explicit = os.getenv("XHS_COOKIES_FILE", "").strip()
    if explicit:
        return explicit
    user_data_path = os.getenv("XHS_USER_DATA_PATH", "").strip()
    if user_data_path:
        return os.path.join(user_data_path, "xhs_cookies.json")
    return ""


def _load_xhs_cookies_from_file() -> Optional[object]:
    """从持久化文件读取 cookies（dict 或 list）。"""
    path = _cookies_file_path()
    if not path:
        return None
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_xhs_cookies_to_file(cookies_obj: object) -> None:
    """把 cookies 写入持久化文件。"""
    path = _cookies_file_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cookies_obj, f, ensure_ascii=False)
    except Exception:
        # 写失败不影响主流程
        pass


def _build_chromium_options() -> ChromiumOptions:
    """构建 ChromiumOptions（集中管理，避免 /scrape 与 /login 使用不一致配置）。"""
    co = ChromiumOptions()

    # 针对 Docker 环境的优化参数
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-dev-shm-usage")  # 防止内存不足崩溃
    co.set_argument("--lang=zh-CN")
    co.set_argument("--window-size=1280,720")
    # 常见反自动化参数（尽量减少被直接重定向到 /login 的概率）
    co.set_argument("--disable-blink-features=AutomationControlled")
    try:
        co.set_pref("excludeSwitches", ["enable-automation"])
        co.set_pref("useAutomationExtension", False)
    except Exception:
        # 不同版本 DrissionPage/ChromiumOptions 可能不支持该 pref，忽略即可
        pass

    # 默认 headless；如需调试可设置 XHS_HEADLESS=0
    if os.getenv("XHS_HEADLESS", "1").strip() not in ("0", "false", "False", "no", "NO"):
        co.set_argument("--headless=new")

    # 模拟更加真实的 Mac 浏览器
    co.set_user_agent(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    # 可选：持久化浏览器 Profile（用于保存登录态）
    user_data_path = os.getenv("XHS_USER_DATA_PATH", "").strip()
    if user_data_path:
        co.set_user_data_path(user_data_path)
        # 关键：固定 profile，避免登录写进一个 profile，而 /scrape 又打开另一个 profile
        profile = os.getenv("XHS_USER_PROFILE", "").strip() or "Default"
        try:
            co.set_user(profile)
        except Exception:
            pass

    # 关键：避免固定使用 9222 导致连接失败（端口占用/残留进程/并发冲突）
    # DrissionPage 支持 auto_port 自动选择可用端口；不同版本可能行为不同，这里做兼容调用。
    try:
        co.auto_port(True)
    except Exception:
        # 旧版本没有 auto_port 或不需要显式设置
        pass

    return co


def _page_has_lock_class(page: ChromiumPage, html_fallback: str = "") -> bool:
    """判断页面是否还处于登录/风控弹层（壳页面）状态。

    优先用 JS 判断 `document.documentElement.classList.contains('reds-lock-scroll')`，
    避免仅靠 HTML 字符串匹配产生误判。
    """
    try:
        val = page.run_js("return document.documentElement.classList.contains('reds-lock-scroll');")
        if isinstance(val, bool):
            return val
    except Exception:
        pass
    return "reds-lock-scroll" in (html_fallback or "")


def _try_open_login_modal(page: ChromiumPage, timeout_sec: float = 8.0) -> None:
    """尽量触发登录弹窗（用于展示二维码）。"""
    # 已经有弹层（常见是 reds-lock-scroll），不需要再点
    if _page_has_lock_class(page, page.html or ""):
        return

    # 试着点“登录”按钮（不同版本可能是 a/button/div）
    candidates = [
        't:button@text()=登录',
        't:a@text()=登录',
        'xpath://button[contains(normalize-space(.), "登录")]',
        'xpath://a[contains(normalize-space(.), "登录")]',
        'xpath://div[contains(normalize-space(.), "登录")]',
    ]

    clicked = False
    for sel in candidates:
        try:
            ele = page.ele(sel, timeout=0)
            if ele:
                ele.click()
                clicked = True
                break
        except Exception:
            continue

    # 如果没点到，也可能当前页本身不要求登录；交给后续等待
    _ = clicked

    # 等待弹窗出现：优先看 lock class，其次看页面是否出现“扫码/二维码”等文本
    step = 0.5
    tries = max(1, int(timeout_sec / step))
    for _ in range(tries):
        html = page.html or ""
        if _page_has_lock_class(page, html):
            return
        if any(k in html for k in ("扫码", "二维码", "qrcode", "QR")):
            return
        page.wait(step)


def _has_note_detail(data: dict[str, Any]) -> bool:
    """判断数据中是否包含笔记详情（用于确认登录是否真的生效）。"""
    try:
        note_data = data.get("note", {}).get("noteDetailMap", {})
        if isinstance(note_data, dict) and note_data:
            return True
        # remove weak check for firstNoteId as it might be present but empty/invalid in some states
        return False
    except Exception:
        return False


def _try_close_login_modal(page: ChromiumPage) -> bool:
    """尝试关闭登录弹窗（返回是否有尝试点击）。"""
    # 常见关闭按钮：右上角 “×” 或带 aria-label 的 close
    candidates = [
        'xpath://button[contains(normalize-space(.), "×")]',
        'xpath://div[contains(normalize-space(.), "×")]',
        'xpath://span[contains(normalize-space(.), "×")]',
        'xpath://button[contains(@aria-label, "关闭") or contains(@aria-label, "close") or contains(@aria-label, "Close")]',
        'xpath://div[contains(@aria-label, "关闭") or contains(@aria-label, "close") or contains(@aria-label, "Close")]',
        'xpath://button[contains(@class, "close") or contains(@class, "Close")]',
        'xpath://div[contains(@class, "close") or contains(@class, "Close")]',
    ]

    for sel in candidates:
        try:
            ele = page.ele(sel, timeout=0)
            if ele:
                ele.click()
                return True
        except Exception:
            continue
    return False


def parse_xhs(url: str):
    co = _build_chromium_options()
    page = ChromiumPage(co)
    
    try:
        # cookies 优先级：环境变量 > 持久化文件
        cookies = _load_xhs_cookies_from_env() or _load_xhs_cookies_from_file()
        if cookies is not None:
            # 先访问主域名，再设置 cookies（否则部分浏览器不允许设置域不匹配的 cookie）
            page.get("https://www.xiaohongshu.com/")
            page.set.cookies(cookies)
            print("0. 已从环境变量注入小红书 cookies")

        print(f"1. 正在尝试访问: {url}")
        page.get(url)
        
        # 打印一下当前的标题，看看是不是遇到了验证码
        title = page.title
        print(f"2. 当前页面标题: {title}")
        
        if "验证" in title or "安全" in title:
             raise Exception(f"触发了小红书风控，页面标题为: {title}")

        # 稍微多等一会儿，确保 JS 加载
        page.wait(3)

        # 优先从运行时提取（适配“壳页面 + 外链脚本”场景）
        runtime_data = _try_get_initial_data_from_runtime(page, timeout_sec=8)
        if runtime_data is not None:
            # 关键：检查运行时数据是否完整。如果缺笔记详情，说明可能遇到风控或加载不全，
            # 此时不要直接采信，而是尝试去 HTML 里或者 script 标签里找（那里往往有完整数据）。
            if _has_note_detail(runtime_data):
                data = runtime_data
                print("3. 使用运行时数据 (Runtime Data)")
            else:
                print("3. 运行时数据缺失笔记详情，尝试降级到静态解析...")
                data = None
        else:
            # 优先尝试 Next.js 常见的 __NEXT_DATA__
            next_data_ele = page.ele('xpath://script[@id="__NEXT_DATA__"]')
            if next_data_ele and next_data_ele.text:
                data = json.loads(next_data_ele.text)
            else:
                # 使用更通用的方式查找数据脚本：遍历所有 script，尽量兼容不同写法
                scripts = page.eles('xpath://script') or []
                data: Optional[dict[str, Any]] = None
                last_debug_sample: str = ""

                for s in scripts:
                    text = s.text or ""
                    if "__INITIAL_STATE__" not in text:
                        continue
                    last_debug_sample = text[:240]
                    expr = _extract_initial_state_expr(text)
                    if not expr:
                        continue
                    try:
                        data = _parse_initial_state_expr(expr)
                        break
                    except Exception:
                        # 继续尝试下一个 script
                        continue
                
                # 3) 兜底方案：直接在 HTML 全文中搜索（解决 script.text 为空或截断的问题）
                if data is None:
                    html_full = page.html or ""
                    # 尝试匹配 window.__INITIAL_STATE__=
                    extracted_str = _extract_object_by_balance(html_full, "window.__INITIAL_STATE__=")
                    if extracted_str:
                        try:
                            data = _parse_initial_state_expr(extracted_str)
                        except Exception:
                            pass

                if data is None:
                    html_sample = (page.html or "")[:500]
                    # reds-lock-scroll 常见于弹层/锁定滚动（可能是登录/风控/验证码）
                    if _page_has_lock_class(page, page.html or ""):
                        try:
                            cur_url = getattr(page, "url", "") or ""
                        except Exception:
                            cur_url = ""
                        try:
                            cookie_count = len(page.cookies(all_domains=True) or [])
                        except Exception:
                            cookie_count = 0
                        raise Exception(
                            "疑似触发登录/风控弹层：页面是“壳页面”，未能拿到笔记初始数据。"
                            f"页面标题: {title}。"
                            f"当前URL: {cur_url}。"
                            f"cookies数量: {cookie_count}。"
                            f"HTML预览: {html_sample}"
                        )
                    raise Exception(
                        "无法从页面脚本中提取初始数据（__INITIAL_STATE__/__NEXT_DATA__）。"
                        f"页面标题: {title}。"
                        f"script预览: {last_debug_sample!r}。"
                        f"HTML预览: {html_sample}"
                    )

        # 提取逻辑
        note_data = data.get('note', {}).get('noteDetailMap', {})
        if not note_data:
             note_data = data.get('note', {}).get('firstNoteId', {})
        
        if not note_data:
            # 这种情况下通常是因为没有拿到 note 对象
            raise Exception("数据结构中未找到笔记详情，可能是风控导致数据不全")

        first_key = list(note_data.keys())[0]
        note_detail = note_data[first_key].get('note', {})

        result = {
            "title": note_detail.get('title', '无标题'),
            "desc": note_detail.get('desc', ''),
            "type": note_detail.get('type'), 
            "image_list": [img.get('urlDefault') for img in note_detail.get('imageList', [])],
            "user": note_detail.get('user', {}).get('nickname'),
            "raw_url": url
        }
        print("3. 数据提取成功！")
        return result

    except Exception as e:
        try:
            with open("debug_failed.html", "w", encoding="utf-8") as f:
                f.write(page.html or "")
        except Exception:
            pass
        print(f"Error 发生: {str(e)}")
        raise e
    finally:
        page.quit() 

@app.post("/scrape")
async def scrape_note(item: URLItem):
    try:
        data = parse_xhs(item.url)
        return {"status": "success", "data": data}
    except Exception as e:
        # 这里返回 400 错误，方便看日志
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/login/qr")
async def get_login_qr(url: str = "https://www.xiaohongshu.com/explore"):
    """获取登录二维码截图（PNG）。

    重要：该接口会在服务端保留一个浏览器会话，以便你扫码后网页能继续完成登录流程。
    登录完成后请调用 /login/wait 或 /login/close 释放资源。
    """
    global _LOGIN_PAGE, _LOGIN_CREATED_AT, _LOGIN_LAST_URL

    with _LOGIN_LOCK:
        # 如果已有会话，直接复用（避免频繁生成二维码导致失效）
        if _LOGIN_PAGE is None:
            try:
                co = _build_chromium_options()
                _LOGIN_PAGE = ChromiumPage(co)
                _LOGIN_CREATED_AT = time.time()
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"创建浏览器失败：{e}")

        _LOGIN_LAST_URL = url

        try:
            _LOGIN_PAGE.get(url)
            _LOGIN_PAGE.wait(2)

            # 确保登录弹窗尽量被触发（否则截图可能没有二维码）
            _try_open_login_modal(_LOGIN_PAGE, timeout_sec=8)

            # 截整页（减少对 DOM selector 的依赖，二维码通常在弹层居中）
            shot = _LOGIN_PAGE.get_screenshot(as_bytes=True, full_page=False)
            if not shot:
                raise Exception("截图失败：未获取到截图结果")

            # DrissionPage 不同版本可能返回 bytes 或图片路径(str)。这里做兼容处理。
            if isinstance(shot, str):
                try:
                    with open(shot, "rb") as f:
                        img_bytes = f.read()
                except Exception as e:
                    raise Exception(f"截图失败：返回了路径但读取失败：{e}，path={shot!r}")
            elif isinstance(shot, (bytes, bytearray)):
                img_bytes = bytes(shot)
            else:
                raise Exception(f"截图失败：未知返回类型 {type(shot)}")

            # 识别图片类型（避免返回的不是 PNG 导致系统无法打开）
            media_type = "application/octet-stream"
            if img_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                media_type = "image/png"
            elif img_bytes.startswith(b"\xff\xd8\xff"):
                media_type = "image/jpeg"
            elif img_bytes.startswith(b"RIFF") and b"WEBP" in img_bytes[:16]:
                media_type = "image/webp"
            else:
                # 防止把 JSON/HTML 错误内容当作 png 保存
                preview = img_bytes[:80]
                raise Exception(f"截图失败：返回内容不是有效图片。内容前80字节: {preview!r}")

            return Response(content=img_bytes, media_type=media_type)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"获取二维码截图失败：{e}")


@app.get("/login/wait")
async def wait_login(timeout: int = 120):
    """等待扫码登录完成。

    - timeout：最大等待秒数（默认 120 秒）
    成功后会关闭登录会话（释放资源），并返回当前 cookies 数量用于粗略确认。
    """
    global _LOGIN_PAGE, _LOGIN_CREATED_AT, _LOGIN_LAST_URL

    deadline = time.time() + max(1, timeout)
    last_debug: dict[str, Any] = {}
    last_recheck_at = 0.0

    while time.time() < deadline:
        with _LOGIN_LOCK:
            page = _LOGIN_PAGE
            last_url = _LOGIN_LAST_URL

        if page is None:
            raise HTTPException(status_code=400, detail="当前没有登录会话，请先访问 /login/qr")

        try:
            html = page.html or ""

            # 采集调试信息（避免返回敏感 cookie 值，只返回名称与数量）
            try:
                cur_url = getattr(page, "url", "") or ""
            except Exception:
                cur_url = ""
            try:
                title = page.title
            except Exception:
                title = ""
            try:
                cookies = page.cookies(all_domains=True) or []
            except Exception:
                cookies = []
            cookie_names: list[str] = []
            for c in cookies:
                if isinstance(c, dict) and "name" in c:
                    cookie_names.append(str(c["name"]))
            runtime_state_preview = ""
            try:
                state_json = page.run_js(
                    'return (window.__INITIAL_STATE__ ? JSON.stringify(window.__INITIAL_STATE__) : "");'
                )
                if isinstance(state_json, str) and state_json.strip():
                    runtime_state_preview = state_json[:200]
            except Exception:
                pass

            last_debug = {
                "title": title,
                "url": cur_url,
                "has_lock_class": _page_has_lock_class(page, html),
                "cookies_count": len(cookies),
                "cookie_names_preview": cookie_names[:10],
                "runtime_state_preview": runtime_state_preview,
            }

            # 1) 弹层消失通常意味着登录完成或至少通过验证
            if not _page_has_lock_class(page, html):
                cookies = page.cookies(all_domains=True, all_info=True) or []
                _save_xhs_cookies_to_file(cookies)
                
                with _LOGIN_LOCK:
                    try:
                        _LOGIN_PAGE.quit()
                    except Exception:
                        pass
                    _LOGIN_PAGE = None
                    _LOGIN_CREATED_AT = 0.0
                    _LOGIN_LAST_URL = ""

                return {"status": "success", "data": {"cookies_count": len(cookies)}}

            # 2) 仍有弹层：如果 cookies 已经写入，尝试关闭弹窗/刷新/回到目标页再判断
            if len(cookies) >= 8:
                _try_close_login_modal(page)

                # 每隔几秒做一次“重新访问目标页”的确认，避免一直停留在首页弹层状态
                now = time.time()
                if now - last_recheck_at >= 5:
                    last_recheck_at = now
                    try:
                        # 刷新一次，有些站点登录后需要刷新才能解除锁滚动
                        page.refresh()
                        page.wait(2)
                    except Exception:
                        pass
                    try:
                        # 再访问你传入的 url（通常是笔记链接），用于确认是否仍会触发登录弹层
                        if last_url:
                            page.get(last_url)
                            page.wait(2)
                    except Exception:
                        pass

                    # 关键：直接以“能否取到笔记详情”作为登录成功判据（比 class 消失更可靠）
                    try:
                        verify_data = _try_get_initial_data_from_runtime(page, timeout_sec=3) or {}
                        html2 = page.html or ""
                        title2 = page.title
                        # 只有“笔记页可访问”（不锁滚动 + 不显示不见了）且能提取到详情，才算真正登录成功
                        if (
                            isinstance(verify_data, dict)
                            and _has_note_detail(verify_data)
                            and (not _page_has_lock_class(page, html2))
                            and ("不见了" not in (title2 or ""))
                        ):
                            # 登录验证通过后，把 cookies 持久化到挂载目录，供 /scrape 复用
                            cookies2 = page.cookies(all_domains=True, all_info=True) or []
                            _save_xhs_cookies_to_file(cookies2)
                            with _LOGIN_LOCK:
                                try:
                                    _LOGIN_PAGE.quit()
                                except Exception:
                                    pass
                                _LOGIN_PAGE = None
                                _LOGIN_CREATED_AT = 0.0
                                _LOGIN_LAST_URL = ""
                            return {"status": "success", "data": {"cookies_count": len(cookies2)}}
                    except Exception:
                        pass

            page.wait(1)
        except Exception:
            # 网络/页面异常时稍等重试
            time.sleep(1)

    return {"status": "waiting", "data": {"timeout": timeout, "last_url": last_url, "debug": last_debug}}


@app.post("/login/close")
async def close_login_session():
    """手动关闭登录会话（释放资源）。"""
    global _LOGIN_PAGE, _LOGIN_CREATED_AT, _LOGIN_LAST_URL
    with _LOGIN_LOCK:
        if _LOGIN_PAGE is not None:
            try:
                _LOGIN_PAGE.quit()
            except Exception:
                pass
        _LOGIN_PAGE = None
        _LOGIN_CREATED_AT = 0.0
        _LOGIN_LAST_URL = ""
    return {"status": "success"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
