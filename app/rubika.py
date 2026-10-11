"""Client for the Rubika Bot API v3: https://botapi.rubika.ir/v3/TOKEN/METHOD

One platform-wide bot (token set in the web panel) posts to the channels that users
connect to it. Responses look like {"status": "OK", "data": {...}}.
"""
import re

import httpx

from . import config


class RubikaError(Exception):
    """API answered with status != OK (or garbage)."""


class RubikaNetworkError(RubikaError):
    """Could not reach the API (retry-able)."""


GUID_RE = re.compile(r"^[a-z]0[A-Za-z0-9]{8,}$")  # c0... channel, g0... group, u0... user, b0... bot


def looks_like_guid(s: str) -> bool:
    return bool(GUID_RE.match(s.strip()))


def _clean(msg: str, token: str) -> str:
    return msg.replace(token, "***") if token else msg


def _client(timeout: float = 90) -> httpx.AsyncClient:
    kw = {"proxy": config.RUBIKA_PROXY} if config.RUBIKA_PROXY else {}
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=20), **kw)


def _unwrap(r: httpx.Response, token: str):
    try:
        j = r.json()
    except ValueError:
        raise RubikaError(f"پاسخ نامعتبر از روبیکا (HTTP {r.status_code})") from None
    if not isinstance(j, dict) or str(j.get("status", "")).upper() != "OK":
        desc = ""
        if isinstance(j, dict):
            desc = " ".join(str(j[k]) for k in ("status", "status_det", "dev_message", "message") if j.get(k))
        raise RubikaError(_clean(desc or f"HTTP {r.status_code}", token))
    data = j.get("data")
    return data if isinstance(data, dict) else {}


async def _call(token: str, method: str, payload: dict | None = None) -> dict:
    url = f"{config.RUBIKA_API_BASE}/{token}/{method}"
    try:
        async with _client() as c:
            r = await c.post(url, json=payload or {})
    except httpx.HTTPError as e:
        raise RubikaNetworkError(_clean(f"{type(e).__name__}: {e}", token)) from None
    return _unwrap(r, token)


async def get_me(token: str) -> dict:
    d = await _call(token, "getMe")
    return d.get("bot") if isinstance(d.get("bot"), dict) else d


async def get_chat(token: str, chat_id: str) -> dict:
    d = await _call(token, "getChat", {"chat_id": chat_id})
    return d.get("chat") if isinstance(d.get("chat"), dict) else d


async def send_message(token: str, chat_id: str, text: str, *, silent: bool = False) -> dict:
    p: dict = {"chat_id": chat_id, "text": text}
    if silent:
        p["disable_notification"] = True
    return await _call(token, "sendMessage", p)


_EXT_TYPES = {
    "Image": {"jpg", "jpeg", "png", "webp", "bmp"},
    "Gif": {"gif"},
    "Video": {"mp4", "mov", "mkv", "avi", "webm", "3gp"},
    "Voice": {"ogg", "oga", "opus"},
    "Music": {"mp3", "m4a", "wav", "flac", "aac"},
}


def file_type_for(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for t, exts in _EXT_TYPES.items():
        if ext in exts:
            return t
    return "File"


async def send_file(token: str, chat_id: str, filename: str, content: bytes, *,
                    caption: str | None = None) -> dict:
    """requestSendFile -> upload to the returned URL -> sendFile with the file_id."""
    d = await _call(token, "requestSendFile", {"type": file_type_for(filename)})
    upload_url = d.get("upload_url")
    if not upload_url:
        raise RubikaError("آدرس آپلود از روبیکا دریافت نشد")
    try:
        async with _client() as c:
            r = await c.post(upload_url, files={"file": (filename, content)})
    except httpx.HTTPError as e:
        raise RubikaNetworkError(_clean(f"{type(e).__name__}: {e}", token)) from None
    up = _unwrap(r, token)
    file_id = up.get("file_id")
    if not file_id:
        raise RubikaError("آپلود فایل در روبیکا ناموفق بود")
    p = {"chat_id": chat_id, "file_id": file_id}
    if caption:
        p["text"] = caption
    return await _call(token, "sendFile", p)


async def delete_message(token: str, chat_id: str, message_id: str) -> None:
    await _call(token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})


async def get_updates(token: str, offset_id: str | None = None, limit: int = 100) -> dict:
    p: dict = {"limit": limit}
    if offset_id:
        p["offset_id"] = offset_id
    return await _call(token, "getUpdates", p)


async def lookup_chat(token: str, chat_id: str) -> dict:
    """{exists: True|False|None, kind: channel|group|user|unknown, title}. None = could not check."""
    try:
        c = await get_chat(token, chat_id)
    except RubikaNetworkError:
        return {"exists": None, "kind": "unknown", "title": "", "chat_id": chat_id}
    except RubikaError:
        return {"exists": False, "kind": "unknown", "title": "", "chat_id": chat_id}
    t = str(c.get("chat_type") or "").lower()
    kind = {"channel": "channel", "group": "group", "user": "user", "bot": "user"}.get(t)
    if kind is None:
        kind = {"c": "channel", "g": "group"}.get(chat_id[:1], "unknown")
    title = c.get("title") or " ".join(
        x for x in (c.get("first_name"), c.get("last_name")) if x) or c.get("username") or ""
    return {"exists": True, "kind": kind, "title": str(title)[:100]}
