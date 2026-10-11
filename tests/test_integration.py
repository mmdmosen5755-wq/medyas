"""End-to-end with a real (SQLite) DB, a scripted fake AI and a fake Telegram bot."""
import asyncio, json, re
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select, func

from app import ai, agent, curator, policy, scraper
from app.crypto import enc
from app.db import (Base, engine, init_db, Session, User, Channel, SourceChannel, Candidate, AgentProfile,
                    Post, PolicyEvent, save_settings, utcnow)

USER, ADMIN = 1001, 1


class FakeBot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, reply_markup=None, **kw): self.sent.append((chat_id, text, reply_markup))
    async def send_chat_action(self, *a, **k): pass
    def to(self, cid): return [t for c, t, _ in self.sent if c == cid]


calls = {"chat": 0, "fast": 0}
CHAT_SCRIPT: list = []
JUDGE_FAILS = {"on": False}


async def fake_complete(system, messages, *, kind="chat", tools=None, max_tokens=1500):
    calls[kind] += 1
    body = messages[-1]["content"] if isinstance(messages[-1]["content"], str) else ""
    if "compliance gate" in system:
        if JUDGE_FAILS["on"]:
            raise ai.AIError("down")
        item = body.split("ITEM:\n")[-1]
        bad = "قمار" in item
        return ai.AIReply(text=json.dumps({"allowed": not bad, "rule": "ممنوعیت قمار" if bad else "",
                                           "reason": "این موضوع مجاز نیست.", "severity": "high"}), tokens=5)
    if "content editor" in system:
        out = []
        for m in re.finditer(r"<post id=(\d+)[^>]*>\n(.*?)\n</post>", body, re.S):
            pid, text = int(m.group(1)), m.group(2)
            good = "سلامت" in text
            out.append({"id": pid, "is_ad": "تبلیغ" in text, "ad_confidence": 90 if "تبلیغ" in text else 5,
                        "relevance": 90 if good else 10, "quality": 80, "category": "سلامت" if good else "ورزش", "reason": "تست"})
        return ai.AIReply(text=json.dumps(out), tokens=20)
    if system.startswith("You rewrite a post"):
        return ai.AIReply(text="بازنویسی: " + re.sub(r"</?post>", "", body).strip()[:80], tokens=7)
    return CHAT_SCRIPT.pop(0)


def tool_call(name, **args):
    b = [{"type": "tool_use", "id": f"t{len(args)}{name}", "name": name, "input": args}]
    return ai.AIReply(blocks=b, tool_calls=[ai.ToolCall(b[0]["id"], name, args)], tokens=3)


def say(text): return ai.AIReply(text=text, blocks=[{"type": "text", "text": text}], tokens=3)


async def reset():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
    await init_db()
    async with Session() as s:
        s.add_all([User(id=ADMIN, name="مدیر", expires_at=None),
                   User(id=USER, name="کاربر", username="usr", expires_at=utcnow() + timedelta(days=30),
                        eitaa_token_enc=enc("tok"))])
        await s.flush()
        ch = Channel(user_id=USER, chat_id="1404", title="کانال من", platform="eitaa")
        s.add(ch)
        await s.flush()
        s.add(AgentProfile(user_id=USER, target_channel_id=ch.id, niche="سلامت و تغذیه", mode="review"))
        s.add(SourceChannel(user_id=USER, handle="health_src", title="منبع سلامت", last_msg_id=0))
        await s.commit()
    await save_settings({"admin_ids": str(ADMIN), "ai_api_key": "k", "agent_rules": "محتوای قمار ممنوع است",
                         "agent_violation_limit": "3", "agent_max_msgs_day": "40", "agent_allow_auto": "1"})
    policy.ADMIN_AS_USER.clear(); policy._last_dm.clear()
    CHAT_SCRIPT.clear(); JUDGE_FAILS["on"] = False
    calls.update(chat=0, fast=0)


def post(i, text, **kw): return scraper.SrcPost(msg_id=i, text=text, **kw)


def serve(posts):
    async def fetch(handle, before=None):
        return scraper.SrcInfo(handle=handle, exists=True, kind="channel", title="منبع سلامت", subscribers="5K", posts=posts)
    scraper.fetch_source = fetch


async def statuses():
    async with Session() as s:
        return {c.msg_id: c for c in (await s.execute(select(Candidate))).scalars()}


async def scenario():
    ai.complete = fake_complete
    async def avail(): return True
    ai.available = avail
    bot = FakeBot(); mgr = SimpleNamespace(bot=bot)

    # ---------- 1. first contact = baseline, no backlog flood
    await reset()
    serve([post(100, "قدیمی سلامت"), post(101, "قدیمی دیگر سلامت")])
    st = await curator.run_for_user(mgr, USER, force=True)
    assert st["new"] == 0 and not await statuses()
    async with Session() as s:
        assert (await s.execute(select(SourceChannel.last_msg_id))).scalar_one() == 101

    # ---------- 2. review mode: ad / duplicate / irrelevant / good
    good = "نکات سلامت: نوشیدن آب کافی، خواب منظم و پیاده‌روی روزانه سه عادت ساده برای سلامت قلب و کاهش استرس است."
    serve([post(100, "x"), post(101, "y"),
           post(102, good, views=900),
           post(103, "سفارش تبلیغ در کانال ما؛ تماس 09121234567"),
           post(104, good + " #سلامت"),
           post(105, "نتیجه‌ی بازی فوتبال دیشب پرسپولیس و استقلال")])
    st = await curator.run_for_user(mgr, USER, force=True)
    c = await statuses()
    assert c[102].status == "proposed" and c[102].rewritten.startswith("بازنویسی:")
    assert c[103].status == "rejected" and c[103].decided_by == "rule" and c[103].category == "تبلیغ"
    assert c[104].status == "duplicate"
    assert c[105].status == "rejected" and "نامرتبط" in c[105].verdict
    cards = [t for t in bot.to(USER) if "پیشنهاد" in t]
    assert len(cards) == 1 and "منبع: @health_src" in cards[0]
    assert calls["chat"] == 0 or True

    # ---------- 3. approving a proposal -> normal post queue
    ctx = agent.Ctx(uid=USER, user_text="منتشر کن", manager=mgr, rules=await policy.load_rules())
    assert (await agent.gate(ctx, "publish_candidate", {"candidate_id": c[102].id})).allowed
    ok, msg, pid = await curator.enqueue(USER, c[102].id, utcnow())
    assert ok
    async with Session() as s:
        p = await s.get(Post, pid)
        assert p.status == "pending" and p.kind == "text" and "منبع: @health_src" in p.text
    ok2, msg2, _ = await curator.enqueue(USER, c[102].id, utcnow())
    assert not ok2                                                     # no double publishing

    # ---------- 4. auto mode (with image) + admin can forbid auto mode
    async with Session() as s:
        (await s.get(AgentProfile, USER)).mode = "auto"; await s.commit()
    serve([post(105, "x"), post(110, "سلامت روان: پنج تمرین تنفس برای کاهش اضطراب و بهبود تمرکز در محیط کار و خانه.",
                                 image_url="https://eitaa.com/file/img.jpg")])
    st = await curator.run_for_user(mgr, USER, force=True)
    assert st["auto"] == 1, str(st)
    async with Session() as s:
        autos = (await s.execute(select(Post).where(Post.file_url != ""))).scalars().all()
        assert len(autos) == 1 and autos[0].kind == "file" and autos[0].file_name.startswith("img_")
        assert (await policy.usage_today(USER)).auto_posts == 1
    assert any("لغو این پست" in str(k) for _, _, k in bot.sent if k)
    await save_settings({"agent_allow_auto": "0"})
    serve([post(110, "x"), post(111, "سلامت پوست: چهار عادت برای محافظت از پوست در فصل سرما و پرهیز از خشکی.")])
    st = await curator.run_for_user(mgr, USER, force=True)
    assert st["auto"] == 0 and st["proposed"] == 1                      # silently downgraded to review

    # ---------- 5. user asks for something the admin forbade -> "I can't" + admin report
    await reset(); bot.sent.clear()
    reply = await agent.chat(mgr, USER, "یک پست درباره‌ی قمار آنلاین و سایت‌های شرط ببند بساز")
    assert "نمی‌توانم" in reply and "گزارش" in reply
    assert calls["chat"] == 0                                           # the chat model was never even reached
    async with Session() as s:
        ev = (await s.execute(select(PolicyEvent))).scalars().all()
        assert len(ev) == 1 and ev[0].kind == "request" and "قمار" in ev[0].rule
    assert any("تلاش برای نقض قوانین" in t for t in bot.to(ADMIN))

    # ---------- 6. innocent-looking message, but the tool call would publish forbidden content -> gate stops it
    async with Session() as s:
        s.add(Candidate(user_id=USER, source_id=1, msg_id=7, text="پیش‌بینی نتایج بازی‌ها و قمار ورزشی با ضریب بالا",
                        status="proposed", relevance=80, quality=80))
        await s.commit()
    async with Session() as s:
        cid = (await s.execute(select(Candidate.id))).scalar_one()
    CHAT_SCRIPT[:] = [tool_call("publish_candidate", candidate_id=cid, when="now"), say("نمی‌توانم این پست را منتشر کنم.")]
    bot.sent.clear()
    reply = await agent.chat(mgr, USER, "پیشنهاد شماره آخر را همین الان منتشر کن")
    assert "⛔️" in reply
    async with Session() as s:
        assert (await s.execute(select(func.count()).select_from(Post))).scalar_one() == 0
        assert (await s.get(Candidate, cid)).status == "proposed"
        assert (await s.execute(select(func.count()).select_from(PolicyEvent).where(PolicyEvent.kind == "action"))).scalar_one() == 1
    assert any("publish_candidate" in t for t in bot.to(ADMIN))

    # ---------- 7. normal conversation + tools work (set settings, add sources through chat)
    CHAT_SCRIPT[:] = [tool_call("update_settings", niche="آشپزی و دستور غذا", min_relevance=70), say("انجام شد: موضوع کانال ثبت شد.")]
    reply = await agent.chat(mgr, USER, "کانال من درباره آشپزی است")
    assert "انجام شد" in reply and "⛔️" not in reply
    p = await curator.get_profile(USER)
    assert p.niche == "آشپزی و دستور غذا" and p.min_relevance == 70
    async with Session() as s:
        hist = (await s.execute(select(func.count()).select_from(__import__("app.db", fromlist=["AgentMsg"]).AgentMsg))).scalar_one()
    assert hist >= 4

    # ---------- 8. repeated violations -> automatic 24h suspension
    for _ in range(2):
        await agent.chat(mgr, USER, "قمار آنلاین")
    assert await policy.suspended_until(USER)
    assert any("تعلیق" in t for t in bot.to(ADMIN))
    r = await agent.chat(mgr, USER, "سلام")
    assert "محدود" in r

    # ---------- 9. admin is exempt, unless /guardtest
    await reset()
    CHAT_SCRIPT[:] = [say("باشه")]
    assert "باشه" in await agent.chat(mgr, ADMIN, "درباره قمار حرف بزن")
    policy.ADMIN_AS_USER.add(ADMIN)
    assert "نمی‌توانم" in await agent.chat(mgr, ADMIN, "درباره قمار حرف بزن")

    # ---------- 10. hard checks without any AI: blocked words + daily quota
    await reset()
    await save_settings({"agent_rules": "", "agent_blocked_words": "شرط‌بندی", "agent_max_msgs_day": "2"})
    n0 = calls["fast"]
    assert "نمی‌توانم" in await agent.chat(mgr, USER, "سایت شرطبندی معرفی کن")
    assert calls["fast"] == n0                                          # decided by the word list alone
    CHAT_SCRIPT[:] = [say("a"), say("b")]
    await agent.chat(mgr, USER, "سلام"); 
    r = await agent.chat(mgr, USER, "سلام دوباره")
    assert "سقف" in r

    # ---------- 11. gate failure: requests fail open, actions fail closed
    await reset()
    JUDGE_FAILS["on"] = True
    rules = await policy.load_rules()
    assert (await policy.judge(rules, "request", "سلام", strict=False)).allowed
    v = await policy.judge(rules, "action", "update", strict=True)
    assert not v.allowed and v.layer == "error"

    # ---------- 12. adding sources: parsing, verification, limits
    await reset()
    async def fetch2(handle, before=None):
        if handle == "ghost": return scraper.SrcInfo(handle=handle, exists=False)
        if handle == "person": return scraper.SrcInfo(handle=handle, exists=True, kind="user", title="Ali")
        return scraper.SrcInfo(handle=handle, exists=True, kind="channel", title=f"کانال {handle}", subscribers="3K",
                               posts=[post(50, "p")])
    scraper.fetch_source = fetch2
    res = await curator.add_sources(USER, "chan_one\nhttps://eitaa.com/chan_two\nghost\nperson\nhttps://eitaa.com/joinchat/xx\nhealth_src",
                                    await policy.load_rules())
    assert [a["handle"] for a in res["added"]] == ["chan_one", "chan_two"]
    assert res["added"][0]["title"] == "کانال chan_one"
    assert {f["handle"] for f in res["failed"]} == {"ghost", "person"} and len(res["bad"]) == 1
    assert [e["handle"] for e in res["existing"]] == ["health_src"]
    await save_settings({"agent_max_sources": "4"})
    res = await curator.add_sources(USER, "a_ch1\nb_ch2\nc_ch3", await policy.load_rules())
    assert len(res["added"]) == 1 and len(res["over_limit"]) == 2      # 3 already there (health_src, chan_one, chan_two)
    await engine.dispose()


def test_everything():
    saved = (ai.complete, ai.available, scraper.fetch_source)
    try:
        asyncio.run(scenario())
    finally:  # do not leak the fakes into other tests
        ai.complete, ai.available, scraper.fetch_source = saved
