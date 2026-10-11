"""Connects Rubika channels to users without typing internal IDs.

Rubika can only send to a channel's internal id (c0...). We learn it from getUpdates:
  A) (works always) the user sends the one-time code to the bot in a private chat, then forwards
     any post of their channel to the bot -> update.new_message.forwarded_from.from_chat_id;
  B) (needs "receive all messages" in BotFather) the user posts the code inside the channel.
Polling only runs while at least one code is waiting.
"""
import asyncio
import logging
import re
import secrets
import time

from sqlalchemy import select

from . import rubika
from .db import Channel, Session, get_settings

log = logging.getLogger("rubika_hub")

CODE_TTL = 15 * 60
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_RE = re.compile(r"RBK-([A-Z0-9]{5})", re.I)
_pending: dict[str, tuple[int, float]] = {}  # code -> (telegram user id, expires_at)


def new_code(uid: int) -> str:
    for k in [k for k, (u, _) in _pending.items() if u == uid]:
        del _pending[k]
    code = "RBK-" + "".join(secrets.choice(_ALPHABET) for _ in range(5))
    _pending[code] = (uid, time.time() + CODE_TTL)
    return code


def cancel(uid: int) -> None:
    for k in [k for k, (u, _) in _pending.items() if u == uid]:
        del _pending[k]


_links: dict[str, tuple[int, str, float]] = {}  # rubika private chat -> (telegram uid, code, expires)
_fwds: dict[str, tuple[str, float]] = {}  # rubika private chat -> (forwarded channel guid, time)


async def _say(token: str, chat_id: str, text: str) -> None:
    try:
        await rubika.send_message(token, chat_id, text)
    except rubika.RubikaError:
        pass


async def _register(manager, token: str, guid: str, uid: int, code: str, *,
                    reply_chat: str = "", delete_msg: str = "") -> None:
    info = await rubika.lookup_chat(token, guid)
    title = info["title"] or "کانال روبیکا"
    async with Session() as s:
        dup = (await s.execute(select(Channel).where(
            Channel.user_id == uid, Channel.chat_id == guid, Channel.platform == "rubika"))).scalar_one_or_none()
        if not dup:
            s.add(Channel(user_id=uid, chat_id=guid, title=title[:100], platform="rubika"))
            await s.commit()
    _pending.pop(code, None)
    for k in [k for k, v in _links.items() if v[0] == uid]:
        _links.pop(k, None)
        _fwds.pop(k, None)
    if delete_msg:
        try:
            await rubika.delete_message(token, guid, delete_msg)
        except rubika.RubikaError:
            pass
    if reply_chat:
        await _say(token, reply_chat, f"کانال «{title}» متصل شد.")
    if manager.bot:
        note = "کانال از قبل متصل بود." if dup else "کانال روبیکا متصل شد."
        if info["exists"] is False:
            note += "\nروبیکا اطلاعات کانال را نداد؛ مطمئن شوید ربات در کانال ادمین است."
        try:
            await manager.bot.send_message(uid, f"{note}\n<b>{_esc(title)}</b>")
        except Exception:
            pass


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _handle(manager, token: str, upd: dict) -> None:
    msg = upd.get("new_message") or upd.get("updated_message") or {}
    if not isinstance(msg, dict):
        return
    chat_id = str(upd.get("chat_id") or msg.get("chat_id") or "")
    private = chat_id[:1] not in ("c", "g")  # c0... channel, g0... group; anything else = chat with the bot
    now = time.time()

    # 1) a post forwarded from the user's channel to the bot (works without any special bot setting)
    fwd = msg.get("forwarded_from")
    guid = str(fwd.get("from_chat_id") or "") if isinstance(fwd, dict) else ""
    if private and guid[:1] in ("c", "g"):
        link = _links.get(chat_id)
        if link and link[2] > now:
            await _register(manager, token, guid, link[0], link[1], reply_chat=chat_id)
        else:
            _fwds[chat_id] = (guid, now)
            await _say(token, chat_id, "پست دریافت شد. حالا کد اتصال (از ربات تلگرام) را برای من بفرستید.")
        return

    # 2) the one-time code
    m = _CODE_RE.search(msg.get("text") or "")
    if not m:
        return
    code = "RBK-" + m.group(1).upper()
    owner = _pending.get(code)
    if not owner or owner[1] < now:
        if private:
            await _say(token, chat_id, "کد نامعتبر است یا منقضی شده. در ربات تلگرام کد جدید بگیرید.")
        return
    if not private:  # code posted inside the channel/group itself (needs "receive all messages")
        await _register(manager, token, chat_id, owner[0], code, delete_msg=str(msg.get("message_id") or ""))
        return
    _links[chat_id] = (owner[0], code, now + CODE_TTL)
    fw = _fwds.get(chat_id)
    if fw and fw[1] > now - CODE_TTL:
        await _register(manager, token, fw[0], owner[0], code, reply_chat=chat_id)
    else:
        await _say(token, chat_id, "کد تأیید شد. حالا یک پست از کانال را برای من فوروارد کنید.")


async def hub_loop(manager) -> None:
    offset: str | None = None
    log.info("rubika hub started")
    while True:
        try:
            now = time.time()
            for k in [k for k, (_, exp) in _pending.items() if exp < now]:
                del _pending[k]
            for k in [k for k, v in _links.items() if v[2] < now]:
                del _links[k]
            for k in [k for k, v in _fwds.items() if v[1] < now - CODE_TTL]:
                del _fwds[k]
            token = (await get_settings())["rubika_bot_token"].strip()
            if not token or not _pending:
                await asyncio.sleep(3)
                continue
            data = await rubika.get_updates(token, offset)
            for upd in data.get("updates") or []:
                if isinstance(upd, dict):
                    await _handle(manager, token, upd)
            if data.get("next_offset_id"):
                offset = str(data["next_offset_id"])
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except rubika.RubikaError as e:
            log.warning("rubika poll: %s", e)
            await asyncio.sleep(8)
        except Exception:
            log.exception("rubika hub error")
            await asyncio.sleep(8)
