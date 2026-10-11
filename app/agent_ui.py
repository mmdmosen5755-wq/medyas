"""Telegram handlers for the content-source feature and the AI agent. Registered from bot.make_router()."""
import asyncio
import contextlib
import logging
from datetime import timedelta
from html import escape
from types import SimpleNamespace

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from . import ai, agent, curator, policy, scraper
from .botutil import fmt_dt, parse_when, to_utc_naive
from .db import AgentProfile, Candidate, Channel, Session, SourceChannel, User, utcnow

log = logging.getLogger("agent_ui")

B_SRC = "افزودن کانال محتوا"
B_AI = "دستیار هوشمند"
SRC_PROMPT = "لطفا در هر خط ایدی یا لینک کانال مورد نظر در برنامه ایتا را وارد کنید"

_chat_locks: dict[int, asyncio.Lock] = {}


class SrcAdd(StatesGroup):
    lines = State()


class AgentChat(StatesGroup):
    talk = State()


class AgentSched(StatesGroup):
    when = State()


def ikb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return b.as_markup()


def _mgr(bot) -> SimpleNamespace:
    return SimpleNamespace(bot=bot)


async def _safe_edit(msg: Message, text: str, kb=None) -> None:
    with contextlib.suppress(TelegramBadRequest):
        await msg.edit_text(text, reply_markup=kb)


async def _gate_user(uid: int, is_admin: bool) -> str | None:
    """None = may use the feature, else a message for the user."""
    rules = await policy.load_rules()
    async with Session() as s:
        u = await s.get(User, uid)
    if not (is_admin or (u and u.expires_at and u.expires_at > utcnow())):
        return "اشتراک شما فعال نیست. از «اشتراک» تمدید کنید."
    return None


async def _send_long(m: Message, html: str) -> None:
    for i in range(0, len(html), 3900):
        chunk = html[i:i + 3900]
        try:
            await m.answer(chunk)
        except TelegramBadRequest:  # model produced something Telegram HTML dislikes
            await m.answer(chunk.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
                           .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"), parse_mode=None)


# ------------------------------------------------------------------ add source channels
async def btn_src(m: Message, state: FSMContext, is_admin: bool):
    await state.clear()
    block = await _gate_user(m.from_user.id, is_admin)
    if block:
        await m.answer(block)
        return
    async with Session() as s:
        n = (await s.execute(select(func.count()).select_from(SourceChannel).where(SourceChannel.user_id == m.from_user.id))).scalar_one()
    await state.set_state(SrcAdd.lines)
    await m.answer(f"{SRC_PROMPT}\n\nمثال:\n<code>@channel_one\nhttps://eitaa.com/channel_two</code>\n\n"
                   + (f"کانال‌های محتوای فعلی شما: {n}\n" if n else "") + "لغو: /cancel",
                   reply_markup=ikb([[("📋 فهرست کانال‌های محتوا", "src:list")]]) if n else None)


async def st_src_lines(m: Message, state: FSMContext, is_admin: bool):
    uid = m.from_user.id
    block = await _gate_user(uid, is_admin)
    if block:
        await state.clear()
        await m.answer(block)
        return
    ok, bad = scraper.parse_handle_lines(m.text or "")
    if not ok and not bad:
        await m.answer("چیزی پیدا نشد؛ آیدی یا لینک کانال را بفرستید (هر کانال در یک خط).")
        return
    wait = await m.answer(f"⏳ در حال بررسی {len(ok)} کانال…")
    rules = await policy.load_rules()
    res = await curator.add_sources(uid, m.text or "", rules)
    await state.clear()
    lines = []
    if res["added"]:
        lines.append(f"✅ <b>{len(res['added'])} کانال اضافه شد:</b>")
        for a in res["added"]:
            subs = f" · {escape(a['subscribers'])} عضو" if a["subscribers"] else ""
            lines.append(f"• <b>{escape(a['title'] or a['handle'])}</b> (@{escape(a['handle'])}){subs}")
    if res["existing"]:
        lines.append("\nℹ️ از قبل در فهرست بود: " + "، ".join(escape(e["title"] or e["handle"]) for e in res["existing"]))
    for f in res["failed"]:
        lines.append(f"❌ @{escape(f['handle'])} — {escape(f['why'])}")
    for raw, why in res["bad"]:
        lines.append(f"❌ <code>{escape(raw[:40])}</code> — {escape(why)}")
    if res["over_limit"]:
        lines.append(f"⚠️ سقف تعداد کانال محتوا ({rules.max_sources}) پر شد؛ این‌ها اضافه نشدند: "
                     + "، ".join("@" + escape(h) for h in res["over_limit"]))
    prof = await curator.get_profile(uid)
    if res["added"]:
        lines.append("\nپست‌های قدیمی این کانال‌ها خوانده نمی‌شود؛ دستیار فقط پست‌های «از این به بعد» را بررسی می‌کند."
                     + ("" if prof.niche else "\nقدم بعد: «دستیار هوشمند» را باز کنید و موضوع کانال خودتان را بگویید."))
    with contextlib.suppress(TelegramBadRequest):
        await wait.delete()
    await m.answer("\n".join(lines) or "موردی اضافه نشد.",
                   reply_markup=ikb([[("🤖 دستیار هوشمند", "ag:open"), ("➕ افزودن بیشتر", "src:more")],
                                     [("📋 فهرست کانال‌های محتوا", "src:list")]]))


async def cb_src(c: CallbackQuery, state: FSMContext, is_admin: bool):
    act = c.data.split(":")[1]
    uid = c.from_user.id
    if act == "more":
        await state.set_state(SrcAdd.lines)
        await c.message.answer(SRC_PROMPT)
    elif act == "list":
        async with Session() as s:
            rows = (await s.execute(select(SourceChannel).where(SourceChannel.user_id == uid).order_by(SourceChannel.id))).scalars().all()
        if not rows:
            await c.message.answer("هنوز کانال محتوایی اضافه نکرده‌اید.")
        else:
            txt = "<b>کانال‌های محتوای شما</b>\n\n" + "\n".join(
                f"{'🟢' if r.active else '⚪️'} <b>{escape(r.title or r.handle)}</b> (@{escape(r.handle)})"
                + (f" · {escape(r.subscribers)}" if r.subscribers else "")
                + (f"\n   ⚠️ {escape(r.error)}" if r.error else "") for r in rows)
            await c.message.answer(txt, reply_markup=ikb([[(f"حذف · {r.title or r.handle}"[:40], f"src:del:{r.id}")] for r in rows]))
    elif act == "del":
        sid = int(c.data.split(":")[2])
        async with Session() as s:
            r = await s.get(SourceChannel, sid)
            if r and r.user_id == uid:
                await s.delete(r)
                await s.commit()
        await c.answer("حذف شد.")
        await _safe_edit(c.message, "🗑 کانال محتوا حذف شد.")
        return
    await c.answer()


# ------------------------------------------------------------------ agent entry + chat
async def _intro(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    p = await curator.get_profile(uid)
    async with Session() as s:
        n = (await s.execute(select(func.count()).select_from(SourceChannel).where(SourceChannel.user_id == uid))).scalar_one()
        tgt = await s.get(Channel, p.target_channel_id) if p.target_channel_id else None
    mode = {"off": "خاموش", "review": "پیشنهاد با تأیید شما", "auto": "انتشار خودکار"}[p.mode]
    txt = ("🤖 <b>دستیار هوشمند محتوا</b>\n\n"
           "کانال‌های محتوای شما را زیر نظر دارم، تبلیغ و تکراری را کنار می‌گذارم و پست‌های مناسبِ کانال شما را پیشنهاد می‌دهم "
           "(یا خودم منتشر می‌کنم). هر چیزی هم خواستید همین‌جا بگویید.\n\n"
           f"• کانال‌های محتوا: {n}\n• کانال مقصد: {escape(tgt.title) if tgt else 'تعیین نشده'}\n• حالت: {mode}\n"
           f"• موضوع کانال شما: {escape(p.niche[:80]) if p.niche else 'هنوز نگفته‌اید'}\n\n"
           "<b>نمونه دستورها:</b>\n"
           "«کانال من درباره‌ی سلامت و تغذیه است»\n«آخرین پست‌های کانال X را ببین»\n«الان بررسی کن»\n«حالت خودکار را روشن کن»\n"
           "«بعد از هر پست بنویس: کانال ما را به دوستان معرفی کنید»\n\nخروج: دکمه‌های منوی پایین یا /cancel")
    return txt, ikb([[("🔍 بررسی الان", "ag:run"), ("📋 پیشنهادها", "ag:props")], [("⚙️ تنظیمات", "ag:panel")]])


async def _open_agent(target: Message, uid: int, state: FSMContext, is_admin: bool) -> None:
    await state.clear()
    block = await _gate_user(uid, is_admin)
    if block:
        await target.answer(block)
        return
    rules = await policy.load_rules()
    if not rules.enabled:
        await target.answer("دستیار هوشمند موقتاً توسط مدیر غیرفعال شده است.")
        return
    if not await ai.available():
        await target.answer("دستیار هوشمند هنوز توسط مدیر راه‌اندازی نشده است.")
        return
    await state.set_state(AgentChat.talk)
    txt, kb = await _intro(uid)
    await target.answer(txt, reply_markup=kb)


async def btn_ai(m: Message, state: FSMContext, is_admin: bool):
    await _open_agent(m, m.from_user.id, state, is_admin)


async def cb_open(c: CallbackQuery, state: FSMContext, is_admin: bool):
    await c.answer()
    await _open_agent(c.message, c.from_user.id, state, is_admin)


async def st_ai_text(m: Message, state: FSMContext, is_admin: bool):
    uid = m.from_user.id
    lock = _chat_locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await m.answer("هنوز در حال پاسخ به پیام قبلی‌تان هستم…")
        return
    async with lock:
        with contextlib.suppress(Exception):
            await m.bot.send_chat_action(m.chat.id, "typing")
        block = await _gate_user(uid, is_admin)
        if block:
            await state.clear()
            await m.answer(block)
            return
        typing = asyncio.create_task(_keep_typing(m))
        try:
            reply = await agent.chat(_mgr(m.bot), uid, (m.text or "")[:3000])
        finally:
            typing.cancel()
    await _send_long(m, reply)


async def _keep_typing(m: Message) -> None:
    while True:
        await asyncio.sleep(4)
        with contextlib.suppress(Exception):
            await m.bot.send_chat_action(m.chat.id, "typing")


async def st_ai_other(m: Message):
    await m.answer("در گفتگو با دستیار فقط پیام متنی پشتیبانی می‌شود. اگر می‌خواهید پستی را بازنویسی کنم، متنش را بفرستید.")


# ------------------------------------------------------------------ run / proposals / panel
async def cb_run(c: CallbackQuery, is_admin: bool):
    uid = c.from_user.id
    block = await _gate_user(uid, is_admin)
    if block:
        await c.answer(block, show_alert=True)
        return
    rules = await policy.load_rules()
    gate = await policy.check_access(rules, uid)
    if not gate.allowed:
        await c.answer(gate.reason, show_alert=True)
        return
    await c.answer("در حال بررسی…")
    wait = await c.message.answer("⏳ در حال بررسی کانال‌های محتوا (ممکن است ۱ تا ۲ دقیقه طول بکشد)…")
    st = await curator.run_for_user(_mgr(c.bot), uid, force=True)
    with contextlib.suppress(TelegramBadRequest):
        await wait.delete()
    await c.message.answer("🔍 <b>نتیجه‌ی بررسی</b>\n\n" + escape(curator.summary(st))
                           + ("\n\nپیشنهادها در پیام‌های جدا با دکمه ارسال شد." if st.get("proposed") else ""))


async def cb_props(c: CallbackQuery):
    uid = c.from_user.id
    async with Session() as s:
        rows = (await s.execute(select(Candidate).where(Candidate.user_id == uid, Candidate.status == "proposed")
                                .order_by(Candidate.id.desc()).limit(5))).scalars().all()
        src = {x.id: x for x in (await s.execute(select(SourceChannel).where(SourceChannel.user_id == uid))).scalars()}
        prof = await s.get(AgentProfile, uid)
    await c.answer()
    if not rows:
        await c.message.answer("پیشنهاد بازی ندارید. «بررسی الان» را بزنید.")
        return
    rules = await policy.load_rules()
    for cand in reversed(rows):
        s_ = src.get(cand.source_id)
        await curator.send_card(_mgr(c.bot), uid, cand, s_.title if s_ else "", curator.compose(cand, prof or AgentProfile(user_id=uid), s_.handle if s_ else "", rules))


async def _panel(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    p = await curator.get_profile(uid)
    async with Session() as s:
        tgt = await s.get(Channel, p.target_channel_id) if p.target_channel_id else None
    rules = await policy.load_rules()
    mk = lambda key, label: (("✅ " if p.mode == key else "") + label, f"ag:mode:{key}")  # noqa: E731
    rows = [[mk("review", "پیشنهاد"), mk("auto", "خودکار"), mk("off", "خاموش")],
            [(f"کانال مقصد: {tgt.title if tgt else 'انتخاب کنید'}"[:50], "ag:tgt")],
            [(f"بازنویسی: {'روشن' if p.rewrite else 'خاموش'}", "ag:tog:rewrite"),
             (f"ذکر منبع: {'روشن' if (p.attribution or rules.force_attr) else 'خاموش'}", "ag:tog:attr")]]
    txt = (f"⚙️ <b>تنظیمات دستیار</b>\n\nهر {p.interval_min} دقیقه بررسی · حداکثر {p.max_per_day} پست در روز · "
           f"حداقل ارتباط {p.min_relevance}٪\n"
           + (f"موضوع: {escape(p.niche[:120])}\n" if p.niche else "") + (f"لحن: {escape(p.tone[:80])}\n" if p.tone else "")
           + (f"دستور دائمی: {escape(p.instructions[:150])}\n" if p.instructions else "")
           + "\nبرای تغییر موضوع، لحن، دستور دائمی، تعداد و فاصله‌ی زمانی از خود دستیار (در گفتگو) بخواهید.")
    return txt, ikb(rows)


async def cb_panel(c: CallbackQuery):
    txt, kb = await _panel(c.from_user.id)
    await c.answer()
    await c.message.answer(txt, reply_markup=kb)


async def cb_mode(c: CallbackQuery, is_admin: bool):
    mode = c.data.split(":")[2]
    uid = c.from_user.id
    rules = await policy.load_rules()
    if mode == "auto" and not rules.allow_auto and not policy.exempt(rules, uid):
        v = policy.Verdict(False, "حالت خودکار غیرفعال است", "حالت انتشار خودکار توسط مدیر غیرفعال شده است.", "quota")
        await policy.report(_mgr(c.bot), uid, "settings", "تلاش برای روشن‌کردن حالت خودکار", v, "mode=auto")
        await c.answer("حالت انتشار خودکار توسط مدیر غیرفعال است.", show_alert=True)
        return
    if await policy.suspended_until(uid) and not policy.exempt(rules, uid):
        await c.answer("دسترسی شما به دستیار موقتاً محدود شده است.", show_alert=True)
        return
    async with Session() as s:
        p = await s.get(AgentProfile, uid) or AgentProfile(user_id=uid)
        if mode != "off" and not p.target_channel_id:
            await c.answer("ابتدا «کانال مقصد» را انتخاب کنید.", show_alert=True)
            return
        s.add(p)
        p.mode = mode
        await s.commit()
    txt, kb = await _panel(uid)
    await _safe_edit(c.message, txt, kb)
    await c.answer("ذخیره شد.")


async def cb_tgt(c: CallbackQuery):
    async with Session() as s:
        chans = (await s.execute(select(Channel).where(Channel.user_id == c.from_user.id).order_by(Channel.id))).scalars().all()
    if not chans:
        await c.answer("ابتدا از «کانال‌ها» کانال خودتان را اضافه کنید.", show_alert=True)
        return
    await c.answer()
    await c.message.answer("کانال مقصد (جایی که دستیار منتشر می‌کند) کدام است؟",
                           reply_markup=ikb([[(f"{ch.title} · {'ایتا' if ch.platform == 'eitaa' else 'روبیکا'}", f"ag:set:{ch.id}")] for ch in chans]))


async def cb_set_tgt(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    async with Session() as s:
        ch = await s.get(Channel, cid)
        if not ch or ch.user_id != c.from_user.id:
            await c.answer("کانال پیدا نشد.", show_alert=True)
            return
        p = await s.get(AgentProfile, c.from_user.id) or AgentProfile(user_id=c.from_user.id)
        s.add(p)
        p.target_channel_id = ch.id
        await s.commit()
    await _safe_edit(c.message, f"✅ کانال مقصد: <b>{escape(ch.title)}</b>")
    await c.answer()


async def cb_toggle(c: CallbackQuery):
    what = c.data.split(":")[2]
    rules = await policy.load_rules()
    async with Session() as s:
        p = await s.get(AgentProfile, c.from_user.id) or AgentProfile(user_id=c.from_user.id)
        s.add(p)
        if what == "rewrite":
            p.rewrite = not p.rewrite
        elif rules.force_attr and p.attribution:
            await c.answer("ذکر منبع طبق قوانین سرویس اجباری است.", show_alert=True)
            return
        else:
            p.attribution = not p.attribution
        await s.commit()
    txt, kb = await _panel(c.from_user.id)
    await _safe_edit(c.message, txt, kb)
    await c.answer()


# ------------------------------------------------------------------ proposal cards
async def _cand_ctx(c: CallbackQuery, is_admin: bool):
    """Common checks for card buttons -> (candidate, error text)"""
    uid = c.from_user.id
    block = await _gate_user(uid, is_admin)
    if block:
        return None, block
    rules = await policy.load_rules()
    gate = await policy.check_access(rules, uid)
    if not gate.allowed and gate.layer != "quota":
        return None, gate.reason
    cid = int(c.data.split(":")[2])
    async with Session() as s:
        cand = await s.get(Candidate, cid)
    if not cand or cand.user_id != uid:
        return None, "این پیشنهاد پیدا نشد."
    if cand.status == "published":
        return None, "این پست قبلاً ارسال/زمان‌بندی شده است."
    return cand, None


async def _publish(c: CallbackQuery, cand: Candidate, run_at, is_admin: bool) -> tuple[bool, str]:
    uid = c.from_user.id
    ctx = agent.Ctx(uid=uid, user_text=f"(کاربر دکمه‌ی انتشار پست #{cand.id} را زد)", manager=_mgr(c.bot), rules=await policy.load_rules())
    v = await agent.gate(ctx, "publish_candidate", {"candidate_id": cand.id})
    if not v.allowed:
        await policy.report(ctx.manager, uid, "action", ctx.user_text, v, "publish_candidate")
        return False, "⛔️ این پست طبق قوانین سرویس قابل انتشار نیست و به مدیر گزارش شد."
    ok, msg, pid = await curator.enqueue(uid, cand.id, run_at, by="user")
    if not ok:
        return False, "❌ " + msg
    when = "در صف ارسال فوری" if run_at <= utcnow() + timedelta(seconds=60) else f"برای {fmt_dt(run_at)} زمان‌بندی شد"
    return True, f"✅ پست #{pid} {when}."


async def cb_card_ok(c: CallbackQuery, is_admin: bool):
    cand, err = await _cand_ctx(c, is_admin)
    if err:
        await c.answer(err, show_alert=True)
        return
    ok, msg = await _publish(c, cand, utcnow(), is_admin)
    await c.answer()
    if ok:
        await _safe_edit(c.message, msg)
    else:
        await c.message.answer(msg)


async def cb_card_sched(c: CallbackQuery, state: FSMContext, is_admin: bool):
    cand, err = await _cand_ctx(c, is_admin)
    if err:
        await c.answer(err, show_alert=True)
        return
    await state.set_state(AgentSched.when)
    await state.update_data(cand_id=cand.id)
    await c.answer()
    await c.message.answer("⏰ زمان انتشار را بفرستید (به وقت تهران):\n<code>18:30</code> · <code>فردا 09:00</code> · <code>+45</code> · "
                           "<code>1405/07/20 18:30</code>\nلغو: /cancel")


async def st_sched_when(m: Message, state: FSMContext, is_admin: bool):
    dt = parse_when(m.text or "")
    if dt is None:
        await m.answer("❌ زمان نامعتبر یا گذشته است. نمونه: <code>فردا 09:00</code>")
        return
    d = await state.get_data()
    await state.clear()
    cb = SimpleNamespace(from_user=m.from_user, bot=m.bot)
    async with Session() as s:
        cand = await s.get(Candidate, d.get("cand_id", 0))
    if not cand or cand.user_id != m.from_user.id or cand.status == "published":
        await m.answer("این پیشنهاد دیگر قابل زمان‌بندی نیست.")
        return
    ok, msg = await _publish(cb, cand, to_utc_naive(dt), is_admin)
    await m.answer(msg)


async def cb_card_rw(c: CallbackQuery, is_admin: bool):
    cand, err = await _cand_ctx(c, is_admin)
    if err:
        await c.answer(err, show_alert=True)
        return
    uid = c.from_user.id
    rules = await policy.load_rules()
    if not (await policy.check_access(rules, uid)).allowed:
        await c.answer("سقف استفاده‌ی امروز شما تمام شده است.", show_alert=True)
        return
    await c.answer("در حال بازنویسی…")
    prof = await curator.get_profile(uid)
    try:
        new = await curator.rewrite_text(prof, rules, curator.clean_tail(cand.text), "یک نسخه‌ی متفاوت با جمله‌بندی تازه بنویس")
    except ai.AIError:
        await c.message.answer("⚠️ بازنویسی الان ممکن نشد؛ کمی بعد دوباره تلاش کنید.")
        return
    async with Session() as s:
        row = await s.get(Candidate, cand.id)
        row.rewritten = new
        src = await s.get(SourceChannel, row.source_id)
        await s.commit()
    await policy.bump(uid, msgs=1)
    cand.rewritten = new
    final = curator.compose(cand, prof, src.handle if src else "", rules)
    await _safe_edit(c.message, curator.card_text(cand, src.title if src else "", final), curator._kb(cand.id))


async def cb_card_no(c: CallbackQuery, is_admin: bool):
    cand, err = await _cand_ctx(c, is_admin)
    if err:
        await c.answer(err, show_alert=True)
        return
    async with Session() as s:
        row = await s.get(Candidate, cand.id)
        row.status, row.decided_by, row.decided_at, row.verdict = "rejected", "user", utcnow(), "رد شد توسط کاربر"
        await s.commit()
    await c.answer("رد شد؛ از این سلیقه یاد می‌گیرم.")
    await _safe_edit(c.message, f"❌ پیشنهاد #{cand.id} رد شد.")


# ------------------------------------------------------------------ admin
async def cb_pv(c: CallbackQuery, is_admin: bool):
    if not is_admin:
        await c.answer("فقط مدیر.", show_alert=True)
        return
    _, act, uid = c.data.split(":")
    uid = int(uid)
    async with Session() as s:
        p = await s.get(AgentProfile, uid) or AgentProfile(user_id=uid)
        s.add(p)
        if act == "sus":
            p.suspended_until, p.mode = utcnow() + timedelta(hours=24), "off"
        else:
            p.suspended_until = None
        await s.commit()
    await c.answer("تعلیق ۲۴ ساعته اعمال شد." if act == "sus" else "تعلیق برداشته شد.", show_alert=True)


async def cmd_srctest(m: Message, is_admin: bool):
    """/srctest <id> - shows what the scraper sees (use it first on Railway to verify eitaa.com is reachable)."""
    if not is_admin:
        return
    parts = (m.text or "").split()
    if len(parts) < 2:
        await m.answer("استفاده: <code>/srctest آیدی_کانال</code>")
        return
    ok, _ = scraper.parse_handle_lines(parts[1])
    if not ok:
        await m.answer("آیدی نامعتبر.")
        return
    info = await scraper.fetch_source(ok[0][1])
    if info.exists is None:
        await m.answer(f"❌ اتصال به ایتا ممکن نشد: {escape(info.error)}\nاگر سرور خارج از ایران است، EITAA_PROXY یا EITAA_WEB_BASE را تنظیم کنید.")
        return
    if info.exists is False:
        await m.answer("❌ کانال پیدا نشد (یا صفحه‌ی عمومی ندارد).")
        return
    last = info.posts[-1] if info.posts else None
    await m.answer(f"✅ <b>{escape(info.title)}</b> · {escape(info.subscribers)} · نوع: {info.kind}\n"
                   f"پست‌های خوانده‌شده: {len(info.posts)}\n"
                   + (f"آخرین پست #{last.msg_id} ({fmt_dt(last.posted_at)}) · بازدید {last.views}\n<i>{escape(last.text[:300])}</i>\n"
                      f"امتیاز تبلیغاتی بودن: {scraper.ad_signals(last.text)[0]}" if last else
                      "⚠️ هیچ پستی خوانده نشد: احتمالاً ساختار صفحه تغییر کرده؛ scraper.py را بررسی کنید."))


async def cmd_guardtest(m: Message, is_admin: bool):
    """/guardtest - the admin is exempt from the guard; this toggles 'treat me like a normal user' for testing the rules."""
    if not is_admin:
        return
    uid = m.from_user.id
    if uid in policy.ADMIN_AS_USER:
        policy.ADMIN_AS_USER.discard(uid)
        await m.answer("حالت آزمایش قوانین خاموش شد؛ دوباره معاف از محدودیت هستید.")
    else:
        policy.ADMIN_AS_USER.add(uid)
        await m.answer("حالت آزمایش قوانین روشن شد: دستیار با شما مثل کاربر عادی رفتار می‌کند (محدودیت‌ها و گزارش به مدیر فعال).\n"
                       "تا ری‌استارت بعدی سرور یا اجرای دوباره‌ی /guardtest فعال می‌ماند.")


# ------------------------------------------------------------------ wiring
def register_buttons(M) -> None:
    M(cmd_srctest, Command("srctest"))
    M(cmd_guardtest, Command("guardtest"))
    M(btn_src, F.text == B_SRC)
    M(btn_ai, F.text == B_AI)


def register_states(M, C) -> None:
    M(st_src_lines, SrcAdd.lines, F.text)
    M(st_sched_when, AgentSched.when, F.text)
    M(st_ai_text, AgentChat.talk, F.text)
    M(st_ai_other, AgentChat.talk)
    C(cb_src, F.data.startswith("src:"))
    C(cb_open, F.data == "ag:open")
    C(cb_run, F.data == "ag:run")
    C(cb_props, F.data == "ag:props")
    C(cb_panel, F.data == "ag:panel")
    C(cb_mode, F.data.startswith("ag:mode:"))
    C(cb_tgt, F.data == "ag:tgt")
    C(cb_set_tgt, F.data.startswith("ag:set:"))
    C(cb_toggle, F.data.startswith("ag:tog:"))
    C(cb_card_ok, F.data.startswith("ag:ok:"))
    C(cb_card_sched, F.data.startswith("ag:sc:"))
    C(cb_card_rw, F.data.startswith("ag:rw:"))
    C(cb_card_no, F.data.startswith("ag:no:"))
    C(cb_pv, F.data.startswith("pv:"))
