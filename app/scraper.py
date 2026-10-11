"""Reads PUBLIC Eitaa channels (the same page eitaa.com shows in a browser) so the AI agent can see new posts.

Eitaayar's API can only send, never read, so this goes through the public web page:
    https://eitaa.com/<channel>            -> channel header + the latest posts
    https://eitaa.com/<channel>?before=ID  -> older posts
The markup is a clone of Telegram's t.me/s/ page with the prefix `etme_` instead of `tgme_`.
Selectors are deliberately tolerant (several fallbacks each): a missing field never breaks the whole parse.
Best effort by nature: if Eitaa changes its HTML, only this file needs to be fixed.
"""
import hashlib
import html as _html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from . import config

_DIG = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_GENERIC_TITLES = {"", "eitaa", "ایتا", "پیام رسان ایتا", "پیام‌رسان ایتا", "eitaa messenger"}
HANDLE_RE = re.compile(r"[A-Za-z0-9_.]{3,64}")


@dataclass
class SrcPost:
    msg_id: int
    text: str = ""
    image_url: str = ""
    views: int = 0
    posted_at: datetime | None = None  # naive UTC
    has_media: bool = False  # video / file / voice: cannot be republished by the agent
    forwarded: bool = False


@dataclass
class SrcInfo:
    handle: str
    exists: bool | None = None  # None = could not check
    kind: str = "unknown"  # channel | user | unknown
    title: str = ""
    subscribers: str = ""
    description: str = ""
    posts: list[SrcPost] = field(default_factory=list)
    error: str = ""


# ------------------------------------------------------------------ user input
def parse_handle_lines(raw: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Free text (one id/link per line, but spaces/commas also work) -> (valid [(raw, handle)], bad [(raw, why)])."""
    ok: list[tuple[str, str]] = []
    bad: list[tuple[str, str]] = []
    seen: set[str] = set()
    tokens = [t for t in re.split(r"[\n\r,،;\s]+", (raw or "").translate(_DIG)) if t.strip()]
    for tok in tokens:
        t = tok.strip().strip("<>()[]\"'«»")
        if not t:
            continue
        if "joinchat" in t or "/+" in t or t.startswith("+"):
            bad.append((tok, "لینک دعوت خصوصی است؛ فقط کانال‌های عمومی (با آیدی) قابل خواندن‌اند"))
            continue
        m = re.search(r"eitaa\.com/(?:s/)?([^/?#\s]+)", t, re.I)
        h = m.group(1) if m else t
        h = h.lstrip("@").strip().rstrip("/")
        if not HANDLE_RE.fullmatch(h) or h.isdigit():
            bad.append((tok, "آیدی یا لینک معتبر ایتا نیست"))
            continue
        if h.lower() in seen:
            continue
        seen.add(h.lower())
        ok.append((tok, h))
    return ok, bad


# ------------------------------------------------------------------ parsing
def _txt(tag) -> str:
    return " ".join(tag.get_text(" ", strip=True).split()) if tag else ""


def _meta(soup: BeautifulSoup, prop: str) -> str:
    t = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return _html.unescape(t.get("content", "")).strip() if t and t.get("content") else ""


def _msg_text(tag) -> str:
    """Message body -> plain text keeping line breaks and real (non-hashtag/mention) links."""
    if tag is None:
        return ""
    for br in tag.find_all("br"):
        br.replace_with("\n")
    for a in tag.find_all("a"):
        href = (a.get("href") or "").strip()
        label = a.get_text(" ", strip=True)
        if href.startswith("http") and "eitaa.com/?q=" not in href and href.rstrip("/") != label.rstrip("/") \
                and not label.startswith(("#", "@")):
            a.replace_with(f"{label} ({href})" if label else href)
        else:
            a.replace_with(label)
    out = tag.get_text("", strip=False)
    out = re.sub(r"[ \t\u00a0]+", " ", out)
    out = re.sub(r" *\n *", "\n", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def parse_count(s: str) -> int:
    """'1.2K' / '3 هزار' / '2.1M' / '۱۲۳' -> int"""
    t = (s or "").translate(_DIG).replace(",", "").replace("٫", ".").strip()
    m = re.match(r"([\d.]+)\s*(K|M|هزار|میلیون)?", t, re.I)
    if not m or not m.group(1).replace(".", "").isdigit():
        return 0
    n = float(m.group(1))
    u = (m.group(2) or "").lower()
    n *= 1000 if u in ("k", "هزار") else 1_000_000 if u in ("m", "میلیون") else 1
    return int(n)


def _iso_to_naive_utc(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _msg_id(node) -> int:
    cand = []
    for n in [node, *node.find_all(True, limit=40)]:
        dp = n.get("data-post") if hasattr(n, "get") else None
        if dp:
            cand.append(dp.rsplit("/", 1)[-1])
    meta = node.find(class_=re.compile(r"etme_widget_message_(meta|date)"))
    if meta:
        a = meta if meta.name == "a" else meta.find("a", href=True)
        if a and a.get("href"):
            cand.append(a["href"].rstrip("/").rsplit("/", 1)[-1].split("?")[0])
    if node.get("id"):
        cand.append(node["id"])
    for c in cand:
        if str(c).isdigit():
            return int(c)
    return 0


def _bg_url(tag, base: str) -> str:
    m = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", (tag.get("style") or "") if tag else "")
    return urljoin(base + "/", m.group(1)) if m else ""


def parse_page(page: str, handle: str) -> SrcInfo:
    soup = BeautifulSoup(page, "html.parser")
    base = config.EITAA_WEB_BASE
    info = SrcInfo(handle=handle)
    head = soup.find(class_="etme_channel_info_header_title")
    title = _txt(head)
    if not title:
        title = re.split(r"\s[-–|]\s*(?:Eitaa|ایتا)\s*$", _meta(soup, "og:title"), flags=re.I)[0].strip()
        if not title and soup.title:
            title = re.split(r"\s[-–|]\s*(?:Eitaa|ایتا)\s*$", soup.title.get_text(strip=True), flags=re.I)[0].strip()
    if title.lower() in _GENERIC_TITLES:
        info.exists = False
        return info
    info.exists = True
    info.title = title[:100]
    info.description = (_txt(soup.find(class_="etme_channel_info_description")) or _meta(soup, "og:description"))[:300]
    cnt = soup.find(class_="counter_value")
    info.subscribers = _txt(cnt)

    if soup.find(class_="etme_channel_info") or soup.find(class_="etme_widget_message"):
        info.kind = "channel"
    elif soup.find(class_="etme_body_wrap") and soup.find(class_="etme_page_title"):
        info.kind = "user"
    else:
        info.kind = "channel" if info.subscribers else "unknown"

    nodes = [n for n in soup.find_all("div") if "etme_widget_message" in (n.get("class") or [])]
    if not nodes:  # older markup: only bubbles
        nodes = soup.find_all("div", class_="etme_widget_message_bubble")
    seen: set[int] = set()
    for n in nodes:
        mid = _msg_id(n)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        body = n.find(class_="etme_widget_message_text")
        t = n.find("time")
        views = n.find(class_="etme_widget_message_views")
        photo = n.find(class_="etme_widget_message_photo_wrap")
        media = bool(n.find(class_=re.compile(r"etme_widget_message_(video|document|voice|roundvideo|audio)")))
        info.posts.append(SrcPost(
            msg_id=mid, text=_msg_text(body), image_url=_bg_url(photo, base),
            views=parse_count(_txt(views)) if views else 0,
            posted_at=_iso_to_naive_utc(t["datetime"]) if t and t.get("datetime") else None,
            has_media=media, forwarded=bool(n.find(class_=re.compile(r"etme_widget_message_forwarded")))))
    info.posts.sort(key=lambda p: p.msg_id)
    return info


async def fetch_source(handle: str, before: int | None = None) -> SrcInfo:
    """Download + parse. Never raises: problems go to info.error (exists=None means 'could not check')."""
    url = f"{config.EITAA_WEB_BASE}/{handle}" + (f"?before={before}" if before else "")
    kw = {"proxy": config.EITAA_PROXY} if config.EITAA_PROXY else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; ContentReader/1.0)",
                                              "Accept-Language": "fa,en;q=0.8"}, **kw) as c:
            r = await c.get(url)
    except httpx.HTTPError as e:
        return SrcInfo(handle=handle, exists=None, error=f"خطای شبکه: {type(e).__name__}")
    if r.status_code == 404:
        return SrcInfo(handle=handle, exists=False)
    if r.status_code != 200:
        return SrcInfo(handle=handle, exists=None, error=f"HTTP {r.status_code}")
    try:
        return parse_page(r.text, handle)
    except Exception as e:  # parser must never take the loop down
        return SrcInfo(handle=handle, exists=None, error=f"خطای تحلیل صفحه: {type(e).__name__}")


async def download(url: str, max_bytes: int = 8 * 1024 * 1024) -> bytes | None:
    kw = {"proxy": config.EITAA_PROXY} if config.EITAA_PROXY else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=True, **kw) as c:
            r = await c.get(url)
        if r.status_code == 200 and 0 < len(r.content) <= max_bytes:
            return r.content
    except httpx.HTTPError:
        pass
    return None


# ------------------------------------------------------------------ cheap text heuristics (no AI, no cost)
_STRONG_AD = [r"سفارش\s*تبلیغ", r"رزرو\s*تبلیغ", r"تبلیغات\s*(?:در|با)", r"#\s*تبلیغ", r"#\s*ads?\b", r"تبادل\s*(?:می|می‌)?کنیم",
              r"حمایت\s*مالی\s*از", r"اسپانسر", r"جهت\s*(?:سفارش|خرید)", r"ثبت\s*سفارش"]
_SOFT_AD = [r"تخفیف", r"حراج", r"فروش\s*ویژه", r"کد\s*تخفیف", r"همین\s*(?:حالا|امروز)", r"فرصت\s*(?:را|رو)?\s*از\s*دست",
            r"تماس\s*بگیرید", r"واتس\s*اپ", r"عضو\s*(?:شوید|شو)\b", r"لینک\s*عضویت", r"پیج\s*(?:ما|اینستاگرام)",
            r"ارزان\s*ترین", r"گارانتی", r"ارسال\s*رایگان", r"بخرید", r"مشاوره\s*رایگان", r"ثبت\s*نام\s*کنید"]
_PHONE = re.compile(r"(?:\+98|0098|0)9\d{9}")
_INVITE = re.compile(r"(?:eitaa\.com/joinchat|t\.me/|rubika\.ir/|instagram\.com/|wa\.me/)", re.I)
_URL = re.compile(r"https?://\S+")


def ad_signals(text: str) -> tuple[int, list[str]]:
    """0-100 'how advertisement-like' + the reasons. >= 85 only for explicit ad wording."""
    t = (text or "").translate(_DIG)
    score, flags = 0, []
    for p in _STRONG_AD:
        if re.search(p, t):
            score += 65
            flags.append("عبارت صریح تبلیغاتی")
            break
    soft = [p for p in _SOFT_AD if re.search(p, t)]
    if soft:
        score += min(40, 14 * len(soft))
        flags.append(f"{len(soft)} عبارت فروش/تخفیف")
    if _PHONE.search(t):
        score += 20
        flags.append("شماره تماس")
    if _INVITE.search(t):
        score += 20
        flags.append("لینک کانال/پیج دیگر")
    if len(_URL.findall(t)) >= 3:
        score += 10
        flags.append("لینک‌های زیاد")
    return min(score, 100), flags


# ------------------------------------------------------------------ dedupe
def _norm(text: str) -> str:
    t = (text or "").translate(_DIG).lower()
    t = _URL.sub(" ", t)
    t = re.sub(r"[@#]\w+", " ", t)
    t = t.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
    t = re.sub(r"[^\w\s]|_", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def content_hash(text: str) -> str:
    return hashlib.sha1(_norm(text)[:400].encode()).hexdigest()


def _shingles(text: str, n: int = 3) -> set[str]:
    w = _norm(text).split()
    return {" ".join(w[i:i + n]) for i in range(max(1, len(w) - n + 1))} if w else set()


def similar(a: str, b: str, threshold: float = 0.6) -> bool:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return False
    return len(sa & sb) / len(sa | sb) >= threshold
