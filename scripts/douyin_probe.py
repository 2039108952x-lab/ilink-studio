"""抖音 DOM 探针：你扫一次码，它把真实页面结构 dump 成 JSON。

为什么需要这个脚本
    抖音前端是自绘 DOM，选择器只能靠实测。以前 `douyin.py` 里的选择器全是猜的，
    真机上一条消息都收不到。这个脚本在登录态下把私信页、视频页的真实结构
    （data-e2e 属性、消息气泡、输入框、评论区）落成 `data/douyin-dom.json`，
    之后改 `douyin.py` 的选择器就有事实依据，不用再猜。

真机上撞出来的三条事实（2026-10，本机）
    1. `goto(wait_until="domcontentloaded")` 在抖音上必然超时 —— 它抛 JS 挑战壳
       之后不再触发那个事件。必须 `wait_until="commit"` + 自己等。
    2. 裸 Chromium 拿到的「首页」是个只设了 title 的挑战壳（body 空、7 个 cookie）。
       带上真 UA、zh-CN、抹掉 `navigator.webdriver` 之后才会跳到 /jingxuan 真页面。
    3. 所以 playwright 默认参数是行不通的，必须 ignore `--enable-automation`。

用法
    python scripts/douyin_probe.py                     # 只探私信
    python scripts/douyin_probe.py --video <视频链接>   # 顺带探评论区
    python scripts/douyin_probe.py --wait-login 300    # 留更长时间扫码

它会做什么
    1. 用独立 profile（data/douyin-profile）起一个 Chromium，跟你的日常浏览器隔离
    2. 打开 douyin.com，等你扫码登录（只等，不代你登录，也不碰密码）
    3. 打开私信页，dump 会话列表 / 消息气泡 / 输入框
    4. 试着往输入框打一段字再清掉（**不会点发送**），验证 send() 的可行性
    5. 有 --video 就打开视频页，dump 评论区
    6. 全程只读页面，不发任何消息、不点关注/点赞

登录态保存在 data/douyin-profile/（已在 .gitignore 里），下次不用重扫。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Windows 控制台默认 GBK，中文和符号会让脚本在打印时直接崩，先钉死 utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOME_URL = "https://www.douyin.com/"
# 私信页入口改过几次，逐个试，哪个到得了就记哪个
DM_URLS = (
    "https://www.douyin.com/user/self?showTab=message",
    "https://www.douyin.com/im",
    "https://www.douyin.com/message",
)
LOGIN_COOKIES = ("sessionid", "sessionid_ss", "sid_tt")

# 真机实测：这一组参数决定了拿到挑战壳还是真页面
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-default-browser-check",
]
STEALTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
STEALTH_INIT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"

# 候选选择器：全部试一遍，命中数写进报告，谁是真的跑一次就知道
CANDIDATES: dict[str, list[str]] = {
    "conversation_item": [
        "[data-e2e='conversation-item']",
        "[data-e2e='session-item']",
        "[class*='conversation'] [class*='item']",
        "[class*='session'] [class*='item']",
        "ul[class*='list'] > li",
    ],
    "message_item": [
        "[data-e2e='message-item']",
        "[data-e2e='msg-item']",
        "[class*='message-item']",
        "[class*='msg-item']",
        "[class*='chat'] [class*='bubble']",
    ],
    "input_box": [
        "div[contenteditable='true']",
        "[data-e2e='chat-input']",
        "textarea",
    ],
    "send_button": [
        "[data-e2e='message-send']",
        "button[class*='send']",
        "[class*='send-btn']",
    ],
    "comment_item": [
        "[data-e2e='comment-item']",
        "[class*='comment-item']",
        "[class*='comment'] [class*='item']",
    ],
}

SAMPLE_LIMIT = 3          # 每类元素最多存几个样本
HTML_LIMIT = 3000         # 单个样本的 HTML 截断长度


# ---------------------------------------------------------------- 页面基础


async def _is_captcha(page: Any) -> bool:
    """是不是被风控丢到验证码/挑战页了。

    注意：抖音页面里**常驻一个隐藏的 captcha 容器**，所以不能只看元素存不存在 ——
    真机上就是因为这个，探针把已登录的个人主页误判成验证码页，卡死在等待里。
    必须判断可见性（有尺寸、非 hidden、非 display:none）。
    """
    try:
        title = await asyncio.wait_for(page.title(), timeout=8)
    except Exception:  # noqa: BLE001
        return False
    if "验证码" in title or "captcha" in title.lower():
        return True
    try:
        return bool(
            await asyncio.wait_for(
                page.evaluate(
                    """() => {
                        const sels = ['#captcha_container', '[class*="captcha"]', '[id*="captcha"]'];
                        for (const sel of sels) {
                            for (const el of document.querySelectorAll(sel)) {
                                const r = el.getBoundingClientRect();
                                const st = getComputedStyle(el);
                                if (r.width > 2 && r.height > 2 &&
                                    st.visibility !== 'hidden' &&
                                    st.display !== 'none' &&
                                    st.opacity !== '0') {
                                    return true;
                                }
                            }
                        }
                        return false;
                    }"""
                ),
                timeout=8,
            )
        )
    except Exception:  # noqa: BLE001
        return False


async def _goto(page: Any, url: str, *, settle_ms: int = 6000) -> dict[str, Any]:
    """打开页面。抖音只能等 commit，domcontentloaded 会超时。

    真机上还见过更糟的：`goto` 连 commit 都不返回（挑战页把连接吊着），
    整个探针就卡死了。所以外面再套一层硬超时，超时就当"页面已尽量加载"继续。

    返回实际落到的 url/title，以及是不是验证码页 —— 上层据此决定要不要重试。
    """
    info: dict[str, Any] = {"requested": url, "url": "", "title": "", "captcha": False, "error": ""}
    try:
        await asyncio.wait_for(page.goto(url, wait_until="commit", timeout=20000), timeout=25)
    except asyncio.TimeoutError:
        info["error"] = "goto 硬超时（挑战页把连接吊住了）"
    except Exception as exc:  # noqa: BLE001 - 超时也算落到了挑战页，继续看
        info["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    try:
        await asyncio.wait_for(page.wait_for_timeout(settle_ms), timeout=settle_ms / 1000 + 5)
    except Exception:  # noqa: BLE001 - 页面可能已关
        pass
    try:
        info["url"] = page.url
        info["title"] = await asyncio.wait_for(page.title(), timeout=10)
        info["captcha"] = await _is_captcha(page)
    except Exception as exc:  # noqa: BLE001
        info["error"] = info["error"] or f"{type(exc).__name__}: {str(exc)[:160]}"
    return info


async def _body_text(page: Any, limit: int = 200) -> str:
    try:
        return await page.evaluate(
            "() => ((document.body && document.body.innerText) || '').slice(0, %d)" % limit
        )
    except Exception:  # noqa: BLE001
        return ""


async def _login_state(ctx: Any) -> dict[str, Any]:
    """读 cookie 判断登录态。

    加超时保护：真机上见过 `ctx.cookies()` 迟迟不返回，把整个探针卡死 ——
    表现就是窗口开着、脚本像死了一样，连"还在等"都不再打印。
    """
    try:
        cookies = await asyncio.wait_for(ctx.cookies("https://www.douyin.com"), timeout=10)
    except asyncio.TimeoutError:
        return {"logged_in": False, "login_cookies": {}, "cookie_count": 0,
                "error": "读 cookie 超时（浏览器可能已被关闭）"}
    except Exception as exc:  # noqa: BLE001
        return {"logged_in": False, "login_cookies": {}, "cookie_count": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    names = {c["name"]: bool(c.get("value")) for c in cookies}
    ok = any(names.get(n) for n in LOGIN_COOKIES)
    return {
        "logged_in": ok,
        "login_cookies": {n: names.get(n, False) for n in LOGIN_COOKIES},
        "cookie_count": len(cookies),
    }


# ---------------------------------------------------------------- 结构采集


async def _e2e_histogram(page: Any) -> dict[str, int]:
    """页面里所有 data-e2e 属性值 → 出现次数。抖音前端最稳的定位依据。"""
    try:
        return await page.evaluate(
            """() => {
                const out = {};
                for (const el of document.querySelectorAll('[data-e2e]')) {
                    const v = el.getAttribute('data-e2e') || '';
                    out[v] = (out[v] || 0) + 1;
                }
                return out;
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        return {"__error__": str(exc)}


async def _count_matches(page: Any) -> dict[str, dict[str, int]]:
    """每个候选选择器的命中数。命中 0 的就是过时选择器。"""
    out: dict[str, dict[str, int]] = {}
    for group, selectors in CANDIDATES.items():
        counts: dict[str, int] = {}
        for sel in selectors:
            try:
                counts[sel] = len(await page.query_selector_all(sel))
            except Exception:  # noqa: BLE001 - 非法选择器不该中断探测
                counts[sel] = -1
        out[group] = counts
    return out


async def _samples(page: Any, selector: str, limit: int = SAMPLE_LIMIT) -> list[dict[str, Any]]:
    """取前几个命中元素的结构摘样：标签、关键属性、文本、HTML 截断。"""
    found: list[dict[str, Any]] = []
    try:
        nodes = await page.query_selector_all(selector)
    except Exception:  # noqa: BLE001
        return found
    for node in nodes[:limit]:
        try:
            info = await node.evaluate(
                """el => ({
                    tag: el.tagName.toLowerCase(),
                    attrs: Array.from(el.attributes)
                        .filter(a => a.name.startsWith('data-') || a.name === 'class' || a.name === 'role')
                        .slice(0, 12)
                        .map(a => a.name + '=' + String(a.value).slice(0, 120)),
                    text: (el.innerText || el.textContent || '').trim().slice(0, 300),
                    html: el.outerHTML.slice(0, %d),
                })""" % HTML_LIMIT
            )
            found.append(info)
        except Exception as exc:  # noqa: BLE001 - 元素可能已从 DOM 移除
            found.append({"error": str(exc)})
    return found


async def _screenshot(page: Any, name: str) -> str:
    """截图给人事后看。抖音页面上动画多，截图可能超时 —— 失败不当致命。"""
    try:
        path = ROOT / "data" / f"douyin-{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), timeout=8000, animations="disabled")
        return str(path)
    except Exception as exc:  # noqa: BLE001
        return f"(截图失败 {type(exc).__name__})"


# ---------------------------------------------------------------- 私信页


async def _open_dm(page: Any, report: dict[str, Any]) -> dict[str, Any]:
    """进私信页。先点页面上的私信入口，再挨个试 URL。

    真机实测：抖音首页（/jingxuan）的 data-e2e 里有 `im-entry` ——
    点它比猜 URL 稳，因为私信 URL 改过好几次（/im、/message、?showTab=message）。
    """
    attempts: list[dict[str, Any]] = []
    try:
        entry = await page.query_selector("[data-e2e='im-entry']")
    except Exception:  # noqa: BLE001
        entry = None
    if entry is not None:
        try:
            await entry.click(timeout=6000)
            await page.wait_for_timeout(4500)
            info = {
                "requested": "[data-e2e='im-entry']",
                "url": page.url,
                "title": await asyncio.wait_for(page.title(), timeout=10),
                "captcha": await _is_captcha(page),
            }
            info["body"] = await _body_text(page, 400)
            attempts.append(info)
            print(f"    点 im-entry → {info['url']}  验证码={info['captcha']}")
            # 真机实测：首页那个 im-entry 点下去落到的是 /user/self?...showTab=post
            # （个人主页），根本不是消息页 —— 所以要看落点，不能只看有没有验证码
            looked_right = any(k in info["url"] for k in ("message", "/im")) or "私信" in info["body"]
            if not info["captcha"] and looked_right:
                return {"attempts": attempts, "landed": info}
            if not looked_right:
                print("    ✗ im-entry 落到个人主页，不是消息页，改试 URL")
        except Exception as exc:  # noqa: BLE001
            attempts.append({"requested": "im-entry", "error": str(exc)[:200]})

    for url in DM_URLS:
        print(f"  → 试私信入口 {url}")
        info = await _goto(page, url)
        body = await _body_text(page, 400)
        info["body"] = body
        attempts.append(info)
        if info["captcha"]:
            print("    ⚠ 被丢到验证码页，停下来等你手动过验证")
            return {"attempts": attempts, "landed": None}
        # 落到消息页的特征：URL 里有 message/im，或者页面上有「私信/消息」字样
        looked_right = any(k in info["url"] for k in ("message", "/im")) or "私信" in body
        if looked_right:
            print(f"    ✓ 落到 {info['url']}")
            return {"attempts": attempts, "landed": info}
        print(f"    ✗ 落到 {info['url']}（不像消息页）")
    return {"attempts": attempts, "landed": None}


async def _probe_dm(page: Any, report: dict[str, Any], wait_ms: int = 3000) -> None:
    opened = await _open_dm(page, report)
    dm: dict[str, Any] = {"open": opened}
    if opened.get("landed") is None:
        dm["note"] = "没有到达私信页：被验证码拦了，或入口全部失效"
        dm["screenshot"] = await _screenshot(page, "dm-failed")
        dm["data_e2e"] = await _e2e_histogram(page)
        dm["matches"] = await _count_matches(page)
        report["direct_message"] = dm
        return

    dm["url"] = opened["landed"]["url"]
    dm["title"] = opened["landed"]["title"]
    dm["data_e2e"] = await _e2e_histogram(page)
    dm["matches"] = await _count_matches(page)

    # 未点开会话时的会话列表样本
    for sel in CANDIDATES["conversation_item"]:
        if dm["matches"]["conversation_item"].get(sel, 0) > 0:
            dm["conversation_selector"] = sel
            dm["conversation_samples"] = await _samples(page, sel)
            break

    # 点开第一个会话：消息区才会渲染出来
    clicked = ""
    for sel in CANDIDATES["conversation_item"]:
        if dm["matches"]["conversation_item"].get(sel, 0) <= 0:
            continue
        try:
            node = await page.query_selector(sel)
            if node is None:
                continue
            await node.click(timeout=5000)
            await page.wait_for_timeout(wait_ms)
            clicked = sel
            break
        except Exception as exc:  # noqa: BLE001
            dm.setdefault("click_errors", []).append({sel: str(exc)[:200]})
    dm["clicked_selector"] = clicked
    dm["after_click_matches"] = await _count_matches(page)
    dm["after_click_url"] = page.url

    for sel in CANDIDATES["message_item"]:
        if dm["after_click_matches"]["message_item"].get(sel, 0) > 0:
            dm["message_selector"] = sel
            dm["message_samples"] = await _samples(page, sel, limit=4)
            break

    for group in ("input_box", "send_button"):
        for sel in CANDIDATES[group]:
            if dm["after_click_matches"][group].get(sel, 0) > 0:
                dm[group + "_selector"] = sel
                dm[group + "_samples"] = await _samples(page, sel, limit=2)
                break

    # send() 能不能成立：打一段字再清掉，绝不点发送
    dm["input_test"] = await _test_input(page, dm.get("input_box_selector", ""))
    dm["screenshot"] = await _screenshot(page, "dm")
    report["direct_message"] = dm


async def _test_input(page: Any, selector: str) -> dict[str, Any]:
    """验证能不能往输入框打字。不发送，只读回自己打进去的字然后清掉。"""
    result: dict[str, Any] = {"selector": selector, "fill_ok": None, "typed_ok": None, "readback": ""}
    if not selector:
        result["error"] = "没有命中输入框选择器"
        return result
    probe_text = "probe-不要发送"
    try:
        await page.fill(selector, probe_text, timeout=3000)
        result["fill_ok"] = True
    except Exception as exc:  # noqa: BLE001 - fill 对 contenteditable 可能不支持，这是要测的
        result["fill_ok"] = False
        result["fill_error"] = str(exc)[:300]

    if not result["fill_ok"]:
        try:
            await page.click(selector, timeout=3000)
            await page.keyboard.type(probe_text)
            result["typed_ok"] = True
        except Exception as exc:  # noqa: BLE001
            result["typed_ok"] = False
            result["type_error"] = str(exc)[:300]

    try:
        result["readback"] = await page.evaluate(
            """sel => {
                const el = document.querySelector(sel);
                if (!el) return '';
                return ('value' in el && el.value) || el.innerText || el.textContent || '';
            }""",
            selector,
        )
    except Exception as exc:  # noqa: BLE001
        result["readback_error"] = str(exc)[:300]

    # 清空：全选删除，不发出去
    try:
        await page.click(selector, timeout=3000)
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Delete")
        result["cleared"] = True
    except Exception as exc:  # noqa: BLE001
        result["cleared"] = False
        result["clear_error"] = str(exc)[:200]
    return result


# ---------------------------------------------------------------- 视频页


async def _find_video_url(page: Any) -> dict[str, Any]:
    """自动找一个视频链接：先看 a[href*=video]，没有就点第一个卡片。

    精选页是 feed 流，卡片不一定是 <a>，所以留了点击兜底。
    """
    out: dict[str, Any] = {"url": "", "method": "", "hrefs": [], "clicked": ""}
    try:
        hrefs = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href*="/video/"]'))
                    .map(a => a.href)
                    .filter((v, i, arr) => arr.indexOf(v) === i)
                    .slice(0, 8)"""
        )
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
        hrefs = []
    out["hrefs"] = hrefs
    if hrefs:
        out["url"] = hrefs[0]
        out["method"] = "a[href*=video]"
        return out
    for sel in (
        "[data-e2e='feed-item']",
        "[data-e2e='video-card']",
        "[data-e2e='video-item']",
        "li[class*='card']",
    ):
        try:
            node = await page.query_selector(sel)
        except Exception:  # noqa: BLE001
            continue
        if node is None:
            continue
        try:
            await node.click(timeout=5000)
            await page.wait_for_timeout(4000)
            if "/video/" in page.url:
                out["url"] = page.url
                out["method"] = f"click {sel}"
                out["clicked"] = sel
                return out
        except Exception as exc:  # noqa: BLE001
            out.setdefault("errors", []).append({sel: str(exc)[:150]})
    return out


async def _probe_video(page: Any, url: str, report: dict[str, Any], wait_ms: int = 5000) -> None:
    """视频页：标题、元信息、评论区结构。"""
    print(f"  → 打开视频页 {url}")
    landed = await _goto(page, url, settle_ms=wait_ms)
    info: dict[str, Any] = {"landed": landed, "url": landed["url"], "title": landed["title"]}
    if landed["captcha"]:
        info["note"] = "被验证码拦了，评论区结构拿不到"
        report["video"] = info
        return
    info["data_e2e"] = await _e2e_histogram(page)
    info["matches"] = await _count_matches(page)
    try:
        info["meta"] = await page.evaluate(
            """() => {
                const pick = n => (document.querySelector('meta[property="' + n + '"]') || {}).content || '';
                return {
                    og_title: pick('og:title'),
                    og_desc: pick('og:description'),
                    og_image: pick('og:image'),
                    h1: (document.querySelector('h1') || {}).innerText || '',
                    canonical: (document.querySelector('link[rel="canonical"]') || {}).href || '',
                };
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        info["meta_error"] = str(exc)[:200]

    for sel in CANDIDATES["comment_item"]:
        if info["matches"]["comment_item"].get(sel, 0) > 0:
            info["comment_selector"] = sel
            info["comment_samples"] = await _samples(page, sel, limit=3)
            break
    info["screenshot"] = await _screenshot(page, "video")
    report["video"] = info


# ---------------------------------------------------------------- 主流程


async def _wait_for_login(page: Any, ctx: Any, seconds: float) -> dict[str, Any]:
    """等扫码。只轮询 cookie，不碰账号密码。"""
    state = await _login_state(ctx)
    if state["logged_in"]:
        print("  → 已经是登录态（profile 里有 sessionid）")
        return state
    print("\n  ⚠ 没检测到登录态。请在浏览器窗口里点右上角「登录」扫码。")
    print(f"    窗口留给你 {int(seconds)} 秒，登录成功后脚本自己继续，不用按键。\n")
    waited = 0.0
    while waited < seconds:
        await asyncio.sleep(3)
        waited += 3
        state = await _login_state(ctx)
        if state["logged_in"]:
            print(f"  → 已登录（等了 {int(waited)} 秒）")
            await page.wait_for_timeout(2000)
            return state
        if int(waited) % 30 == 0:
            print(f"    …还在等（{int(waited)}s）")
    print("  ⚠ 等超时了，继续按未登录状态探（结构会少很多）")
    return state


async def run(args: argparse.Namespace) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("没装 playwright：\n  pip install playwright\n  playwright install chromium")
        return 2

    profile = (ROOT / args.profile).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    out_path = (ROOT / args.out).resolve()
    report: dict[str, Any] = {"profile": str(profile), "note": "只读探测，未发送任何消息"}

    async with async_playwright() as pw:
        # Chromium 不吃 socket 补丁，只能把 DoH 查到的 IP 用 host-resolver-rules 喂它
        from gateway.adapters.douyin import resolver_rules

        launch_args = list(STEALTH_ARGS)
        rules = resolver_rules()
        if rules:
            launch_args.append(f"--host-resolver-rules={rules}")
        ctx = await pw.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            args=launch_args,
            ignore_default_args=["--enable-automation"],
            user_agent=STEALTH_UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
        )
        try:
            await ctx.add_init_script(STEALTH_INIT)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            print("→ 打开 douyin.com")
            landing = await _goto(page, HOME_URL)
            report["home"] = landing
            print(f"  落到 {landing['url']}  title={landing['title'][:40]!r}  "
                  f"验证码={landing['captcha']}")

            if landing["captcha"]:
                print("\n  ⚠ 首页直接是验证码页。请在窗口里手动把验证过掉（拖滑块），"
                      "过完脚本会自己发现。")
                for _ in range(int(args.wait_login) // 3):
                    await asyncio.sleep(3)
                    if not await _is_captcha(page):
                        print("  → 验证码过了")
                        break
                report["home_after_captcha"] = await _goto(page, HOME_URL)

            report["login"] = await _wait_for_login(page, ctx, args.wait_login)
            if not report["login"]["logged_in"]:
                report["hint"] = "没登录：私信页会跳到登录墙，结构不完整。重跑并扫码。"

            await _probe_dm(page, report)
            if args.video:
                video_url = args.video
                if video_url.strip().lower() == "auto":
                    print("  → 自动找视频：先回首页（抖音会跳到 /jingxuan）")
                    await _goto(page, HOME_URL)
                    found = await _find_video_url(page)
                    report["video_auto"] = found
                    video_url = found.get("url") or ""
                if video_url:
                    await _probe_video(page, video_url, report)
                else:
                    report["video"] = {
                        "note": "没找到视频链接；把首页 data-e2e 留下来供以后调",
                        "data_e2e": await _e2e_histogram(page),
                    }

            report["ok"] = True
        finally:
            await ctx.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ 报告已写入 {out_path}")
    _print_summary(report)
    return 0


def _print_summary(report: dict[str, Any]) -> None:
    """终端里先给人看关键结论，JSON 留给后面改代码用。"""
    login = report.get("login") or {}
    print(f"\n登录：{'✅ 已登录' if login.get('logged_in') else '❌ 未登录'}")
    dm = report.get("direct_message") or {}
    print(f"私信页：{dm.get('title', '')}  {dm.get('url', '')}")
    for group in ("conversation_item", "message_item", "input_box", "send_button"):
        best = ""
        for sel, n in (dm.get("after_click_matches", {}).get(group) or {}).items():
            if n > 0:
                best = f"{sel}  ({n} 个)"
                break
        print(f"  {group:<18}{best or '❌ 全部候选都没命中'}")
    test = dm.get("input_test") or {}
    print(f"  打字测试：fill={'ok' if test.get('fill_ok') else 'fail'} "
          f"type={'ok' if test.get('typed_ok') else '-'} "
          f"读回={str(test.get('readback', ''))[:40]!r}")
    video = report.get("video")
    if video:
        print(f"视频页：{str(video.get('title', ''))[:60]}")
        hit = [f"{sel} ({n} 个)" for sel, n in (video.get("matches", {}).get("comment_item") or {}).items() if n > 0]
        print(f"  评论项 {hit[0] if hit else '❌ 全部候选都没命中'}")


def main(argv: list[str] | None = None) -> int:
    # RPA 通道已退役（见 docs/ACCEPTANCE.md 的「内容雷达」一节）：
    # DOM 探针依赖的 PlaywrightDriver 与 resolver_rules 都已不存在，
    # 这里直接说清楚，而不是崩一个 ImportError 让人自己猜。
    print("douyin_probe 已退役：抖音通道改走 MCP 后端，DOM 探针不再适用。\n"
          "  只读联调：python scripts/douyin_smoke.py\n"
          "  单工具调用：node vendor/mcp-call.mjs douyin_status '{}'\n"
          "  正常使用：界面第七区「刷抖音」")
    return 2


if __name__ == "__main__":
    sys.exit(main())
