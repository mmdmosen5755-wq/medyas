"""Client for the eitaayar.ir API: https://eitaayar.ir/api/TOKEN/METHOD"""
import httpx

from . import config


class EitaaError(Exception):
    """API answered with ok=false (or garbage)."""


class EitaaNetworkError(EitaaError):
    """Could not reach the API (retry-able)."""


def _clean(msg: str, token: str) -> str:
    return msg.replace(token, "***") if token else msg


async def _call(token: str, method: str, data: dict | None = None, files: dict | None = None):
    url = f"{config.EITAA_API_BASE}/{token}/{method}"
    kw = {"proxy": config.EITAA_PROXY} if config.EITAA_PROXY else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=20), **kw) as c:
            r = await c.post(url, data=data or None, files=files or None)
    except httpx.HTTPError as e:
        raise EitaaNetworkError(_clean(f"{type(e).__name__}: {e}", token)) from None
    try:
        j = r.json()
    except ValueError:
        raise EitaaError(f"پاسخ نامعتبر از ایتایار (HTTP {r.status_code})") from None
    if not isinstance(j, dict) or not j.get("ok"):
        desc = ""
        if isinstance(j, dict):
            desc = str(j.get("description") or j.get("message") or j.get("error") or j)
        raise EitaaError(_clean(desc or f"HTTP {r.status_code}", token))
    return j.get("result", j)


def _opts(title, silent, reply_to, date, pin, view_delete) -> dict:
    d: dict = {}
    if title:
        d["title"] = title
    if silent:
        d["disable_notification"] = 1
    if reply_to:
        d["reply_to_message_id"] = reply_to
    if date:
        d["date"] = int(date)
    if pin:
        d["pin"] = 1
    if view_delete:
        d["viewCountForDelete"] = int(view_delete)
    return d


async def get_me(token: str) -> dict:
    return await _call(token, "getMe")


async def send_message(token: str, chat_id: str, text: str, *, title=None, silent=False,
                       reply_to=None, date=None, pin=False, view_delete=None) -> dict:
    data = {"chat_id": chat_id, "text": text, **_opts(title, silent, reply_to, date, pin, view_delete)}
    return await _call(token, "sendMessage", data=data)


async def send_file(token: str, chat_id: str, filename: str, content: bytes, *, caption=None,
                    title=None, silent=False, reply_to=None, date=None, pin=False,
                    view_delete=None) -> dict:
    data = {"chat_id": chat_id, **_opts(title, silent, reply_to, date, pin, view_delete)}
    if caption:
        data["caption"] = caption
    return await _call(token, "sendFile", data=data, files={"file": (filename, content)})


# ------------------------------------------------------------------ chat lookup
# Eitaayar has no "getChat" method, so we read the public web page of the chat
# (the same thing eitaa.com shows in a browser). Best effort: never blocks adding.
import html as _html
import re as _re

_GENERIC_TITLES = {"", "eitaa", "ایتا", "پیام رسان ایتا", "پیام‌رسان ایتا", "eitaa messenger"}


def _meta(page: str, prop: str) -> str:
    for pat in (rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]+content=["\']([^"\']*)["\']',
                rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{prop}["\']'):
        m = _re.search(pat, page, _re.I)
        if m:
            return _html.unescape(m.group(1)).strip()
    return ""


def parse_chat_page(page: str, is_invite: bool) -> dict:
    """HTML of eitaa.com page -> {exists, kind, title, desc}. kind: channel|group|private|unknown"""
    title = _meta(page, "og:title")
    if not title:
        m = _re.search(r"<title[^>]*>(.*?)</title>", page, _re.I | _re.S)
        title = _html.unescape(m.group(1)).strip() if m else ""
    title = _re.split(r"\s[-–|]\s*(?:Eitaa|ایتا)\s*$", title, flags=_re.I)[0].strip()
    desc = _meta(page, "og:description") or _meta(page, "description")
    if title.lower() in _GENERIC_TITLES:
        return {"exists": False, "kind": "unknown", "title": "", "desc": ""}
    text = _re.sub(r"<[^>]+>", " ", page)
    low = text.lower()
    if "مشترک" in text or "subscriber" in low or "followers" in low:
        kind = "channel"
    elif "عضو" in text or "members" in low:
        kind = "group"
    else:
        kind = "private" if is_invite else "unknown"
    return {"exists": True, "kind": kind, "title": title[:100], "desc": desc[:200]}


async def lookup_chat(chat_id: str) -> dict:
    """Returns {exists: True|False|None, kind, title, desc}. None = could not check."""
    cid = chat_id.strip()
    is_invite = "joinchat" in cid
    if is_invite:
        code = cid.rstrip("/").split("joinchat/")[-1].split("?")[0]
        url = f"https://eitaa.com/joinchat/{code}"
    elif _re.fullmatch(r"[A-Za-z0-9_.]{3,64}", cid) and not cid.isdigit():
        url = f"https://eitaa.com/{cid}"
    else:
        return {"exists": None, "kind": "unknown", "title": "", "desc": ""}
    kw = {"proxy": config.EITAA_PROXY} if config.EITAA_PROXY else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=10), follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}, **kw) as c:
            r = await c.get(url)
    except httpx.HTTPError:
        return {"exists": None, "kind": "unknown", "title": "", "desc": ""}
    if r.status_code == 404:
        return {"exists": False, "kind": "unknown", "title": "", "desc": ""}
    if r.status_code != 200:
        return {"exists": None, "kind": "unknown", "title": "", "desc": ""}
    return parse_chat_page(r.text, is_invite)
