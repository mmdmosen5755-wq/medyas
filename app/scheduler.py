"""Background loop: sends due posts to Eitaa and notifies the owner."""
import asyncio
import logging
from html import escape

from sqlalchemy import select, update

from . import eitaa, rubika, scraper
from .crypto import dec
from .db import Channel, Post, Session, User, get_settings, utcnow

log = logging.getLogger("scheduler")
POLL_SECONDS = 10
RETRIES = 3


async def recover() -> None:
    """Posts stuck in 'sending' after a crash/redeploy go back to the queue."""
    async with Session() as s:
        await s.execute(update(Post).where(Post.status == "sending").values(status="pending"))
        await s.commit()


async def _file_bytes(post: Post, bot, err):
    """File content of a queued post: a Telegram file_id (user upload) or a remote image URL (AI agent post)."""
    if post.file_id:
        if bot is None:
            raise err("ربات تلگرام فعال نیست؛ دانلود فایل ممکن نشد")
        buf = await bot.download(post.file_id)
        if buf is None:
            raise err("دانلود فایل از تلگرام ناموفق بود")
        return buf.read()
    if post.file_url:
        data = await scraper.download(post.file_url)
        if data is None:
            raise err("دانلود تصویر منبع ناموفق بود")
        return data
    raise err("فایل پست پیدا نشد")


async def _deliver(post: Post, token: str, chat_id: str, bot) -> dict:
    kw = dict(title=post.title or None, silent=post.silent, pin=post.pin,
              view_delete=post.view_delete or None)
    if post.kind == "text":
        return await eitaa.send_message(token, chat_id, post.text, **kw)
    data = await _file_bytes(post, bot, eitaa.EitaaError)
    return await eitaa.send_file(token, chat_id, post.file_name or "file", data,
                                 caption=post.text or None, **kw)


async def _deliver_rubika(post: Post, token: str, chat_id: str, bot) -> dict:
    if post.kind == "text":
        return await rubika.send_message(token, chat_id, post.text, silent=post.silent)
    data = await _file_bytes(post, bot, rubika.RubikaError)
    return await rubika.send_file(token, chat_id, post.file_name or "file", data,
                                  caption=post.text or None)


async def process_post(post_id: int, manager) -> None:
    async with Session() as s:
        post = await s.get(Post, post_id)
        user = await s.get(User, post.user_id)
        chan = await s.get(Channel, post.channel_id)
    if not (user and chan):
        await _finish(post_id, "failed", "کاربر یا کانال حذف شده است")
        return
    is_rb = chan.platform == "rubika"
    if is_rb:
        token = (await get_settings())["rubika_bot_token"].strip()
        missing = "ربات روبیکا هنوز توسط مدیر فعال نشده است"
    else:
        token = dec(user.eitaa_token_enc)
        missing = "توکن ایتایار تنظیم نشده است"
    if not token:
        await _finish(post_id, "failed", missing)
        await _notify(manager, post, chan, ok=False, err=missing)
        return
    deliver = _deliver_rubika if is_rb else _deliver
    net_err = rubika.RubikaNetworkError if is_rb else eitaa.EitaaNetworkError

    error = ""
    result: dict = {}
    for attempt in range(1, RETRIES + 1):
        try:
            result = await deliver(post, token, chan.chat_id, manager.bot)
            error = ""
            break
        except net_err as e:
            error = f"خطای شبکه: {e}"
            await asyncio.sleep(5 * attempt)
        except Exception as e:  # EitaaError or anything else: no retry
            error = str(e) or type(e).__name__
            break

    if error:
        await _finish(post_id, "failed", error[:900])
        await _notify(manager, post, chan, ok=False, err=error)
    else:
        mid = ""
        if isinstance(result, dict):
            mid = str(result.get("message_id", ""))
        await _finish(post_id, "sent", "", mid)
        await _notify(manager, post, chan, ok=True)


async def _finish(post_id: int, status: str, error: str, mid: str = "") -> None:
    async with Session() as s:
        p = await s.get(Post, post_id)
        p.status, p.error, p.eitaa_message_id = status, error, mid
        p.sent_at = utcnow() if status == "sent" else None
        await s.commit()


async def _notify(manager, post: Post, chan: Channel, ok: bool, err: str = "") -> None:
    if manager.bot is None:
        return
    label = escape(post.title or (post.text[:40] if post.text else post.file_name) or f"#{post.id}")
    if ok:
        msg = f"✅ پست «{label}» در کانال <b>{escape(chan.title)}</b> ارسال شد."
    else:
        msg = (f"❌ ارسال پست «{label}» به کانال <b>{escape(chan.title)}</b> ناموفق بود.\n"
               f"<code>{escape(err[:500])}</code>")
        if chan.platform == "rubika":
            msg += ("\n\nمطمئن شوید ربات روبیکا هنوز در کانال <b>ادمین</b> است و اجازه‌ی ارسال پیام دارد؛ "
                    "در غیر این صورت کانال را حذف و دوباره متصل کنید.")
        elif "chat not found" in err.lower():
            msg += ("\n\n💡 یعنی ایتایار این کانال را در پنل شما پیدا نکرده است. راه حل:\n"
                    "۱) در پنل eitaayar.ir وارد بخش «کانال‌ها» شوید و کانال خودتان را اضافه کنید "
                    "(باید مدیر آن کانال باشید).\n"
                    "۲) <b>شناسه عددی</b> همان کانال را از پنل کپی کنید (مثل 1404).\n"
                    "۳) در ربات کانال قبلی را حذف و با همان شناسه عددی دوباره اضافه کنید.")
    try:
        await manager.bot.send_message(post.user_id, msg)
    except Exception:
        pass


async def scheduler_loop(manager) -> None:
    await recover()
    log.info("scheduler started")
    while True:
        try:
            async with Session() as s:
                due = (await s.execute(
                    select(Post).where(Post.status == "pending", Post.run_at <= utcnow())
                    .order_by(Post.run_at).limit(20))).scalars().all()
                ids = [p.id for p in due]
                for p in due:
                    p.status = "sending"
                await s.commit()
            if ids:
                await asyncio.gather(*(process_post(i, manager) for i in ids), return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(POLL_SECONDS)
