"""Pure helpers for the bot: date parsing/formatting, message content extraction."""
import re
from datetime import datetime, timedelta, timezone

import jdatetime

from .config import TZ

_DIG = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
MAX_FILE = 20 * 1024 * 1024  # Telegram bot download limit


def norm_digits(s: str) -> str:
    return s.translate(_DIG)


def to_utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def fmt_dt(dt_utc: datetime | None) -> str:
    if not dt_utc:
        return "-"
    local = dt_utc.replace(tzinfo=timezone.utc).astimezone(TZ)
    j = jdatetime.datetime.fromgregorian(datetime=local.replace(tzinfo=None))
    return j.strftime("%Y/%m/%d - %H:%M")


def parse_when(text: str, now: datetime | None = None) -> datetime | None:
    """Returns an aware Tehran datetime, or None if invalid / in the past.
    Accepted: `+30` (min), `+2 ساعت`, `18:30`, `فردا 09:00`, `پس فردا 21:00`,
    `1405/07/20 18:30` (Jalali) or `2026/10/12 18:30` (Gregorian)."""
    now = now or datetime.now(TZ)
    t = norm_digits(text).replace("،", " ").replace("‌", " ")
    t = re.sub(r"\s+", " ", t).strip()
    dt: datetime | None = None

    m = re.fullmatch(r"\+ ?(\d{1,5}) ?(دقیقه|ساعت|m|h)?", t)
    if m:
        n = int(m[1])
        dt = now + (timedelta(hours=n) if m[2] in ("ساعت", "h") else timedelta(minutes=n))
    else:
        m = re.fullmatch(r"(امروز|فردا|پس ?فردا)? ?(\d{1,2}):(\d{2})", t)
        if m:
            h, mi = int(m[2]), int(m[3])
            if h > 23 or mi > 59:
                return None
            dt = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            word = m[1]
            if word == "فردا":
                dt += timedelta(days=1)
            elif word and word.startswith("پس"):
                dt += timedelta(days=2)
            elif word is None and dt <= now:
                dt += timedelta(days=1)
        else:
            m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2}) (\d{1,2}):(\d{2})", t)
            if not m:
                return None
            y, mo, d, h, mi = (int(x) for x in m.groups())
            try:
                if 1300 <= y < 1700:
                    naive = jdatetime.datetime(y, mo, d, h, mi).togregorian()
                else:
                    naive = datetime(y, mo, d, h, mi)
            except ValueError:
                return None
            dt = naive.replace(tzinfo=TZ)

    if dt is None or dt < now + timedelta(seconds=30):
        return None
    return dt


def normalize_chat_id(raw: str) -> str:
    t = norm_digits(raw).strip()
    if "joinchat" in t:  # group invite links are passed as-is
        return t
    m = re.search(r"eitaa\.com/([^/?\s]+)", t)
    if m:
        t = m.group(1)
    return t.lstrip("@").strip()


def _swap_ext(name: str, ext: str) -> str:
    base = name.rsplit(".", 1)[0] if "." in name else name
    return f"{base}.{ext}"


def extract_content(m) -> dict | None:
    """Telegram message -> {kind,text,file_id,file_name} | {'error': msg} | None (unsupported)."""
    if m.text:
        return {"kind": "text", "text": m.text, "file_id": "", "file_name": ""}
    cap = m.caption or ""

    def f(file_id, name, size):
        if size and size > MAX_FILE:
            return {"error": "حجم فایل بیشتر از ۲۰ مگابایت است (محدودیت تلگرام برای ربات‌ها)."}
        return {"kind": "file", "text": cap, "file_id": file_id, "file_name": name}

    if m.photo:
        p = m.photo[-1]
        return f(p.file_id, f"photo_{p.file_unique_id[:8]}.jpg", p.file_size)
    if m.animation:  # Eitaa wants .gif for GIFs
        a = m.animation
        return f(a.file_id, _swap_ext(a.file_name or f"anim_{a.file_unique_id[:8]}", "gif"), a.file_size)
    if m.video:
        v = m.video
        return f(v.file_id, v.file_name or f"video_{v.file_unique_id[:8]}.mp4", v.file_size)
    if m.document:
        d = m.document
        return f(d.file_id, d.file_name or f"file_{d.file_unique_id[:8]}", d.file_size)
    if m.audio:
        a = m.audio
        return f(a.file_id, a.file_name or f"audio_{a.file_unique_id[:8]}.mp3", a.file_size)
    if m.voice:
        v = m.voice
        return f(v.file_id, f"voice_{v.file_unique_id[:8]}.ogg", v.file_size)
    if m.sticker:
        s = m.sticker
        if s.is_animated or s.is_video:
            return {"error": "فقط استیکر ثابت پشتیبانی می‌شود."}
        return f(s.file_id, f"sticker_{s.file_unique_id[:8]}.webp", s.file_size)
    return None
