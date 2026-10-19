import asyncio
import logging
import re

import httpx

from .config import LLM_BASE_URL, LLM_MODEL
from .llm import _balanced_objects

logger = logging.getLogger("vod-delivery")

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
MAX_STEPS = 10
MAX_PAGE_CHARS = 4000

AGENT_SYSTEM = """You are a media-fetch agent for a home IPTV bot. The user gave a PAGE URL (not a direct
video file). Your goal: find the DIRECT video file link (one that returns video content like mp4/mkv/webm/ts)
related to that page, or the actual video behind it.

Available tools:
- {"tool": "web_fetch", "args": {"url": "..."}} -> cleaned page text + any media links found in the HTML
- {"tool": "yt_dlp_probe", "args": {"url": "..."}} -> video metadata (title, duration, direct formats) if the page is a supported video page
- {"tool": "check_link", "args": {"url": "..."}} -> probes a candidate link: returns content-type, size, filename. Use this to VERIFY a candidate is a real video file before finishing.

Each turn reply with ONE JSON object as the last thing in your output:
{"thought": "one short line", "action": {"tool": "web_fetch|yt_dlp_probe|check_link", "args": {"url": "..."}}}

After at most 6 tool rounds you MUST finish with:
{"final_url": "https://.../video.mp4"}  (the verified direct media URL)
or {"final_url": null, "reason": "why no direct video was found"}

Player/embedded hosts (govid, doodstream, vidmoly, streamtape...): a page usually lists SEVERAL
player servers as links — web_fetch EACH of them (one at a time) until an observation shows
"network requests to video files": that URL IS the stream, verify it with check_link and finish.
Do not retry the original page — work through the player links. yt_dlp_probe only helps for
well-known platforms (youtube, vimeo, ...).

Rules: never guess a link you have not verified with check_link (yt_dlp_probe output of a direct
format URL also counts as verified). Do not repeat the same tool call with identical args.
Every action must include "action.tool" and "action.args.url" as non-empty strings.
Keep "thought" under 12 words. Output ONLY the JSON object, no other prose.
"""

PLAYER_HOST_RE = re.compile(
    r"https?://[^\s\"\'<>]+(?:govid|vidshar|earnvids|fastved|vinovo|dood|upshare|seekplayer"
    r"|luluvdoo|vidaraa|rpmvip|streamtape|mp4upload|mixdrop|vidmoly|filemoon|filelions"
    r"|dropload|vidhide|supervideo|oneupload|megacloud|smoothpre|embedrise)[^\s\"'<>]*",
    re.IGNORECASE)

MEDIA_LINK_RE = re.compile(
    r"https?://[^\s\"'<>\\]+\.(?:mp4|mkv|webm|avi|ts|m4v|mov)(?:\?[^\s\"'<>\\]*)?",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_ALL_TAGS_RE = re.compile(r"<[^>]+>")


def _page_to_text(html: str) -> str:
    html = _TAG_RE.sub(" ", html)
    html = _COMMENT_RE.sub(" ", html)
    text = _ALL_TAGS_RE.sub(" ", html)
    return re.sub(r"\s+", " ", text).strip()


_CF_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform", "attention required")
_camoufox_cm = None
_camoufox_browser = None
_camoufox_lock = asyncio.Lock()


async def _camoufox_recycle() -> None:
    global _camoufox_cm, _camoufox_browser
    try:
        if _camoufox_cm is not None:
            await _camoufox_cm.__aexit__(None, None, None)
    except Exception:
        logger.warning("camoufox recycle failed", exc_info=True)
    _camoufox_cm = None
    _camoufox_browser = None


def _camoufox_payload_empty(payload: str | None) -> bool:
    if not payload:
        return True
    iframes = (payload.rsplit("@@IFRAMES@:", 1)[-1].split("@@PLAYERS@:", 1)[0].strip()
               if "@@IFRAMES@:" in payload else "")
    players = (payload.split("@@PLAYERS@:", 1)[-1].split("@@MEDIA_REQ@:", 1)[0].strip()
               if "@@PLAYERS@:" in payload else "")
    sniffed = (payload.rsplit("@@MEDIA_REQ@:", 1)[-1].strip() if "@@MEDIA_REQ@:" in payload else "")
    return not iframes and not players and not sniffed


_sniff_cache: dict[str, tuple[float, str]] = {}
SNIFF_TTL = 30 * 60


async def _camoufox_fetch(url: str, max_wait: float = 25.0) -> str | None:
    cached = _sniff_cache.get(url)
    if cached and cached[0] > asyncio.get_event_loop().time():
        return cached[1]
    async with _camoufox_lock:
        cached = _sniff_cache.get(url)
        if cached and cached[0] > asyncio.get_event_loop().time():
            return cached[1]
        for attempt in range(3):
            result = await _camoufox_fetch_once(url, max_wait)
            if not _camoufox_payload_empty(result):
                if "@@MEDIA_REQ@@:" in result and result.rsplit("@@MEDIA_REQ@:", 1)[-1].strip():
                    _sniff_cache[url] = (asyncio.get_event_loop().time() + SNIFF_TTL, result)
                return result
            logger.info("camoufox: attempt %d for %s rendered empty — recycling", attempt + 1, url[:70])
            await _camoufox_recycle()
        return result


async def _camoufox_fetch_once(url: str, max_wait: float) -> str | None:
    global _camoufox_cm, _camoufox_browser
    try:
        from camoufox.async_api import AsyncCamoufox
        from camoufox.addons import DefaultAddons
    except ImportError:
        return None
    try:
        if _camoufox_browser is None:
            _camoufox_cm = AsyncCamoufox(headless=True, humanize=True, exclude_addons=[DefaultAddons.UBO])
            _camoufox_browser = await _camoufox_cm.__aenter__()
        browser = _camoufox_browser
        page = await browser.new_page()
        sniffed: list[str] = []

        def _on_request(req) -> None:
            u = req.url
            if ".m3u8" in u or ".mp4" in u or ".mkv" in u:
                sniffed.append(u)

        try:
            page.context.on("request", _on_request)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            deadline = asyncio.get_event_loop().time() + max_wait
            while asyncio.get_event_loop().time() < deadline:
                title = (await page.title()).strip().lower()
                size = len(await page.content())
                if (title and "just a moment" not in title and "loading" not in title
                        and size > 15000):
                    break
                await page.wait_for_timeout(2500)
            await page.wait_for_timeout(3000)
            try:
                await page.mouse.wheel(0, 2500)
                await page.wait_for_timeout(2000)
            except Exception:
                pass
            html = await page.content()
            iframes = await page.eval_on_selector_all("iframe", "els => els.map(e => e.src)")
            try:
                anchors = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            except Exception:
                anchors = []
            players = set(PLAYER_HOST_RE.findall(html) + [i for i in iframes if PLAYER_HOST_RE.search(i)]
                          + [a for a in anchors if PLAYER_HOST_RE.search(a)])
            if not sniffed:
                for sel in ["[class*=watch]", "[class*=play]", "[id*=watch]", "[id*=play]"]:
                    try:
                        el = await page.wait_for_selector(sel, timeout=12000)
                    except Exception:
                        continue
                    if not el:
                        continue
                    try:
                        await el.click(timeout=15000)
                    except Exception:
                        try:
                            await el.click(timeout=5000, force=True)
                        except Exception:
                            logger.info("camoufox: click on %s failed entirely", sel)
                            continue
                    await page.wait_for_timeout(7000)
                    logger.info("camoufox: clicked %s -> sniffed=%d", sel, len(sniffed))
                    if sniffed:
                        break
                html = await page.content()
                iframes = await page.eval_on_selector_all("iframe", "els => els.map(e => e.src)")
                try:
                    anchors = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                except Exception:
                    anchors = []
                players |= set(PLAYER_HOST_RE.findall(html) + [i for i in iframes if PLAYER_HOST_RE.search(i)]
                               + [a for a in anchors if PLAYER_HOST_RE.search(a)])
                logger.info("camoufox: player links %d, iframes %d, sniffed %d", len(players), len(iframes), len(sniffed))
            players = sorted(players)
            if "just a moment" in html.lower()[:4000]:
                return None
            return (html + "\n@@IFRAMES@@:" + " | ".join(i for i in iframes if i)
                    + "\n@@PLAYERS@@:" + " | ".join(players[:10])
                    + "\n@@MEDIA_REQ@@:" + " | ".join(dict.fromkeys(sniffed)))
        finally:
            await page.close()
    except Exception:
        logger.warning("camoufox fetch failed for %s", url[:120], exc_info=True)
        return None


async def tool_web_fetch(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": BROWSER_UA}) as client:
            r = await client.get(url)
        if r.status_code >= 403 or r.status_code == 429:
            cf = await _camoufox_fetch(url)
            if cf:
                media = sorted(set(MEDIA_LINK_RE.findall(cf)))[:12]
                players = (cf.split("@@PLAYERS@:", 1)[-1].split("@@MEDIA_REQ@:", 1)[0].strip()
                           if "@@PLAYERS@:" in cf else "")
                iframes = (cf.rsplit("@@IFRAMES@:", 1)[-1].split("@@PLAYERS@:", 1)[0].strip()
                           if "@@IFRAMES@:" in cf else "")
                sniffed = (cf.rsplit("@@MEDIA_REQ@:", 1)[-1].strip() if "@@MEDIA_REQ@:" in cf else "")
                text = _page_to_text(cf.split("@@IFRAMES@:", 1)[0])[:150]
                if sniffed or iframes or players:
                    obs = f"page title: {text[:120] or '(empty)'}"
                else:
                    obs = f"page text (stealth browser): {text or '(empty)'}"
                if sniffed:
                    obs += "\n*** network requests to video files (VERY LIKELY THE STREAM — verify with check_link): " + sniffed[:600]
                if players:
                    obs += "\n*** player host links (" + str(len(players.split(" | "))) + " servers — web_fetch EACH, one will issue the stream): " + players[:700]
                if iframes:
                    obs += "\n*** embedded iframes (player hosts — web_fetch them): " + iframes[:400]
                if media:
                    obs += "\nmedia links in page HTML: " + " | ".join(media[:8])
                return obs
            return f"HTTP {r.status_code}"
        ctype = (r.headers.get("content-type") or "").lower()
        body = r.text
        media = sorted(set(MEDIA_LINK_RE.findall(body)))[:12]
        if "html" in ctype:
            text = _page_to_text(body)[:MAX_PAGE_CHARS]
        else:
            text = body[:MAX_PAGE_CHARS]
        obs = f"page text: {text or '(empty)'}"
        if media:
            obs += "\nmedia links found in page: " + " | ".join(media[:8])
        return obs
    except Exception as exc:
        return f"web_fetch failed: {type(exc).__name__}: {str(exc)[:120]}"


async def tool_yt_dlp_probe(url: str) -> str:
    def _probe():
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 25,
            "skip_download": True,
            "nocheckcertificate": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return "no info"
        out = f"yt-dlp: title={info.get('title')!r} duration={info.get('duration')}s extractor={info.get('extractor_key')}"
        fmts = info.get("formats") or []
        direct = [f for f in fmts if f.get("url") and (f.get("vcodec") not in (None, "none"))]
        if direct:
            best = direct[-1]
            out += f"\ndirect format URL (verified playable): {best['url'][:300]}"
        return out

    import asyncio

    try:
        return await asyncio.wait_for(asyncio.to_thread(_probe), timeout=60)
    except asyncio.TimeoutError:
        return "yt_dlp_probe timed out"
    except Exception as exc:
        return f"yt_dlp_probe: {type(exc).__name__}: {str(exc)[:160]}"


async def tool_check_link(url: str) -> str:
    from .library import probe_media_url

    probe = await probe_media_url(url)
    if not probe["ok"]:
        return f"check_link: NOT a direct media file ({probe['reason']})"
    return f"check_link: OK — content_type={probe['content_type']} size={probe['size']} filename={probe['title_source']}"


async def _execute(action: dict) -> str:
    tool = (action or {}).get("tool")
    args = (action or {}).get("args") or {}
    url = str(args.get("url") or "").strip()
    if not url:
        return "error: missing url arg"
    if tool == "web_fetch":
        return await tool_web_fetch(url)
    if tool == "yt_dlp_probe":
        return await tool_yt_dlp_probe(url)
    if tool == "check_link":
        return await tool_check_link(url)
    return f"unknown tool {tool!r}"


async def _chat(messages: list[dict]) -> dict | None:
    import json as _json

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 2200,
    }
    import httpx

    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(f"{LLM_BASE_URL}/chat/completions", json=payload)
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        content = message.get("content") or ""
        if not content.strip():
            content = message.get("reasoning_content") or ""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    for source in (content, message.get("reasoning_content") or ""):
        for obj in reversed(_balanced_objects(source)):
            if isinstance(obj, dict) and ("action" in obj or "final_url" in obj):
                return obj
        for m in reversed(re.findall(r"\{[^{}]*\}", source, re.DOTALL)):
            try:
                parsed = _json.loads(m)
            except _json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and ("action" in parsed or "final_url" in parsed):
                return parsed
    return None


async def run_agent(page_url: str, max_steps: int = MAX_STEPS) -> dict:
    messages = [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": f"Page URL: {page_url}\nFind the direct video file link."},
    ]
    seen_calls: set[tuple] = set()
    last_verified = None
    executed = 0
    noops = 0
    step = 0
    while executed < max_steps and noops < 4:
        step += 1
        reply = await _chat(messages)
        if reply is None:
            noops += 1
            logger.warning("fetch agent step %d: unparseable reply", step)
            messages.append({"role": "user", "content": "Reply with ONE valid JSON object ({\"thought\", \"action\"} or {\"final_url\"})."})
            continue
        if "final_url" in reply:
            url = reply.get("final_url")
            if not url:
                return {"ok": False, "reason": str(reply.get("reason") or "no direct video found")}
            probe = await tool_check_link(str(url))
            if "check_link: OK" in probe or last_verified == str(url):
                return {"ok": True, "url": str(url)}
            if last_verified:
                return {"ok": True, "url": last_verified}
            return {"ok": False, "reason": f"final URL failed verification: {probe}"}
        action = reply.get("action") or {}
        tool_name = action.get("tool")
        url_arg = str((action.get("args") or {}).get("url") or "")
        if not tool_name or not url_arg:
            noops += 1
            logger.warning("fetch agent step %d: malformed action (%s) — nudging", step, str(reply)[:100])
            messages.append({"role": "user", "content": 'Malformed action: "action.tool" and "action.args.url" must be non-empty strings. Example: {"thought":"...","action":{"tool":"web_fetch","args":{"url":"https://..."}}}'})
            continue
        key = (tool_name, url_arg)
        if key in seen_calls:
            noops += 1
            logger.info("fetch agent step %d: duplicate call %s — nudging", step, key[0] + " " + key[1][:60])
            messages.append({"role": "user", "content": "observation: you already ran that exact call. Try a different tool/url or finish with {\"final_url\"}."})
            continue
        seen_calls.add(key)
        executed += 1
        observation = await _execute(action)
        sniffed_urls = re.findall(r"https?://\S+?\.(?:m3u8|mp4)\S*", observation)
        for candidate in sniffed_urls[:2]:
            clean = candidate.rstrip(").,'\"")
            probe = await tool_check_link(clean)
            if probe.startswith("check_link: OK"):
                logger.info("fetch agent: auto-verified sniffed stream %s", clean[:90])
                return {"ok": True, "url": clean}
        if observation.startswith("check_link: OK"):
            last_verified = str((action.get("args") or {}).get("url"))
        logger.info("fetch agent step %d: %s %s -> %s", step, key[0], key[1][:80], observation[:100])
        messages.append({"role": "assistant", "content": str(reply)})
        messages.append({"role": "user", "content": f"observation: {observation}\n\nContinue (next tool JSON) or finish with the final JSON."})
    if last_verified:
        return {"ok": True, "url": last_verified}
    return {"ok": False, "reason": f"agent hit its step limit without a verified link ({executed} tool steps, {noops} no-op rounds)"}
