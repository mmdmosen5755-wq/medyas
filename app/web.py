"""Admin web panel: set Telegram bot token + admin IDs, prices, card; manage users & payments."""
import hmac
import secrets
import time
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from . import ai, config, rubika
from .bot import manager
from .botutil import fmt_dt
from .db import (DEFAULTS, AgentProfile, AgentUsage, Candidate, Payment, PolicyEvent, Post, Session,
                 SourceChannel, User, get_settings, parse_ids, parse_plans, save_settings, utcnow)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["jdate"] = fmt_dt
templates.env.filters["money"] = lambda n: f"{int(n):,}"

_fails: dict[str, list[float]] = {}  # ip -> failed login timestamps


def render(request: Request, name: str, **ctx):
    flash = request.session.pop("flash", None)
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return templates.TemplateResponse(request, name, {
        "flash": flash, "csrf": request.session["csrf"], "bot": manager,
        "page": name.split(".")[0], **ctx})


def authed(request: Request) -> bool:
    return bool(request.session.get("auth"))


def check_csrf(request: Request, token: str) -> bool:
    return bool(token) and hmac.compare_digest(token, request.session.get("csrf", ""))


def back(request: Request, to: str, msg: str | None = None):
    if msg:
        request.session["flash"] = msg
    return RedirectResponse(to, status_code=303)


# ------------------------------------------------------------------ auth
@router.get("/login")
async def login_page(request: Request):
    if authed(request):
        return back(request, "/")
    return render(request, "login.html", error=None)


@router.post("/login")
async def login(request: Request, password: str = Form("")):
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 600]
    if len(recent) >= 5:
        return render(request, "login.html", error="تعداد تلاش بیش از حد؛ ۱۰ دقیقه صبر کنید.")
    if hmac.compare_digest(password.encode(), config.ADMIN_PASSWORD.encode()):
        _fails.pop(ip, None)
        request.session.clear()
        request.session["auth"] = True
        return back(request, "/")
    _fails[ip] = recent + [now]
    return render(request, "login.html", error="رمز عبور اشتباه است.")


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return back(request, "/login")


# ------------------------------------------------------------------ dashboard
@router.get("/")
async def dashboard(request: Request):
    if not authed(request):
        return back(request, "/login")
    async with Session() as s:
        cnt = lambda q: s.execute(q)  # noqa: E731
        users = (await cnt(select(func.count()).select_from(User))).scalar_one()
        active = (await cnt(select(func.count()).select_from(User).where(User.expires_at > utcnow()))).scalar_one()
        pending = (await cnt(select(func.count()).select_from(Post).where(Post.status == "pending"))).scalar_one()
        sent = (await cnt(select(func.count()).select_from(Post).where(Post.status == "sent"))).scalar_one()
        failed = (await cnt(select(func.count()).select_from(Post).where(Post.status == "failed"))).scalar_one()
        pays = (await cnt(select(func.count()).select_from(Payment).where(Payment.status == "pending"))).scalar_one()
        recent = (await cnt(select(Post).order_by(Post.id.desc()).limit(10))).scalars().all()
        srcs = (await cnt(select(func.count()).select_from(SourceChannel))).scalar_one()
        viol = (await cnt(select(func.count()).select_from(PolicyEvent).where(
            PolicyEvent.created_at > utcnow() - timedelta(hours=24), PolicyEvent.kind != "content"))).scalar_one()
    cfg = await get_settings()
    return render(request, "dashboard.html", stats=dict(users=users, active=active, pending=pending,
                  sent=sent, failed=failed, pays=pays, srcs=srcs, viol=viol), recent=recent,
                  token_set=bool(cfg["bot_token"]), rubika_set=bool(cfg["rubika_bot_token"]),
                  admins=len(parse_ids(cfg["admin_ids"])))


# ------------------------------------------------------------------ settings
@router.get("/settings")
async def settings_page(request: Request):
    if not authed(request):
        return back(request, "/login")
    cfg = await get_settings()
    token_hint = ("••••" + cfg["bot_token"][-4:]) if cfg["bot_token"] else ""
    rb_hint = ("••••" + cfg["rubika_bot_token"][-4:]) if cfg["rubika_bot_token"] else ""
    ai_hint = ("••••" + cfg["ai_api_key"][-4:]) if cfg["ai_api_key"] else ""
    return render(request, "settings.html", cfg=cfg, token_hint=token_hint, rb_hint=rb_hint, ai_hint=ai_hint,
                  ai_defaults=ai.DEFAULT_MODELS)


@router.post("/settings")
async def settings_save(request: Request, csrf: str = Form(""), bot_token: str = Form(""),
                        rubika_bot_token: str = Form(""),
                        admin_ids: str = Form(""), brand_name: str = Form(""),
                        welcome_text: str = Form(""), support_username: str = Form(""),
                        card_number: str = Form(""), card_owner: str = Form(""),
                        trial_days: str = Form("0"), plans: str = Form(""),
                        ai_provider: str = Form("anthropic"), ai_api_key: str = Form(""), ai_base_url: str = Form(""),
                        ai_model_chat: str = Form(""), ai_model_fast: str = Form(""),
                        agent_enabled: str = Form(""), agent_rules: str = Form(""), agent_blocked_words: str = Form(""),
                        agent_max_msgs_day: str = Form("40"), agent_max_auto_day: str = Form("10"),
                        agent_max_sources: str = Form("15"), agent_violation_limit: str = Form("5"),
                        agent_allow_auto: str = Form(""), agent_force_attr: str = Form("")):
    if not authed(request):
        return back(request, "/login")
    if not check_csrf(request, csrf):
        return back(request, "/settings", "نشست منقضی شد؛ دوباره تلاش کنید.")
    if not parse_ids(admin_ids):
        return back(request, "/settings", "حداقل یک آیدی عددی ادمین وارد کنید (از ربات با /id بگیرید).")
    if not trial_days.strip().isdigit():
        return back(request, "/settings", "روزهای تست باید عدد باشد.")
    if plans.strip() and not parse_plans(plans):
        return back(request, "/settings", "فرمت پلن‌ها نادرست است (هر خط: روز:قیمت).")
    nums = {"agent_max_msgs_day": agent_max_msgs_day, "agent_max_auto_day": agent_max_auto_day,
            "agent_max_sources": agent_max_sources, "agent_violation_limit": agent_violation_limit}
    if not all(v.strip().isdigit() for v in nums.values()):
        return back(request, "/settings", "سقف‌های دستیار باید عدد باشند.")
    if ai_provider not in ai.DEFAULT_MODELS:
        return back(request, "/settings", "ارائه‌دهنده‌ی هوش مصنوعی نامعتبر است.")
    values = {"ai_provider": ai_provider, "ai_base_url": ai_base_url.strip(), "ai_model_chat": ai_model_chat.strip(),
              "ai_model_fast": ai_model_fast.strip(), "agent_enabled": "1" if agent_enabled else "0",
              "agent_rules": agent_rules.strip(), "agent_blocked_words": agent_blocked_words.strip(),
              "agent_allow_auto": "1" if agent_allow_auto else "0", "agent_force_attr": "1" if agent_force_attr else "0",
              **{k: v.strip() for k, v in nums.items()},
              "admin_ids": admin_ids.strip(), "brand_name": brand_name.strip() or DEFAULTS["brand_name"],
              "welcome_text": welcome_text.strip(), "support_username": support_username.strip().lstrip("@"),
              "card_number": card_number.strip(), "card_owner": card_owner.strip(),
              "trial_days": trial_days.strip(), "plans": plans.strip()}
    if bot_token.strip():  # blank = keep the stored token
        values["bot_token"] = bot_token.strip()
    if ai_api_key.strip():
        values["ai_api_key"] = ai_api_key.strip()
    rb_note = ""
    if rubika_bot_token.strip():
        try:
            me = await rubika.get_me(rubika_bot_token.strip())
            values["rubika_bot_token"] = rubika_bot_token.strip()
            rb_note = f" · روبیکا: @{me.get('username') or me.get('bot_title') or 'متصل'}"
        except rubika.RubikaError as e:
            rb_note = f" · توکن روبیکا پذیرفته نشد ({str(e)[:80]})"
    await save_settings(values)
    ok = await manager.start()
    msg = ((f"ذخیره شد؛ ربات @{manager.username} روشن شد" if ok
            else f"ذخیره شد، اما ربات روشن نشد: {manager.error}") + rb_note)
    return back(request, "/settings", msg)


@router.post("/bot/restart")
async def bot_restart(request: Request, csrf: str = Form("")):
    if not authed(request):
        return back(request, "/login")
    if not check_csrf(request, csrf):
        return back(request, "/", "نشست منقضی شد.")
    ok = await manager.start()
    return back(request, "/", "ربات دوباره راه‌اندازی شد" if ok else f"خطا: {manager.error}")


# ------------------------------------------------------------------ users
@router.get("/users")
async def users_page(request: Request, q: str = ""):
    if not authed(request):
        return back(request, "/login")
    async with Session() as s:
        stmt = select(User).order_by(User.created_at.desc()).limit(200)
        if q.strip():
            like = f"%{q.strip().lstrip('@')}%"
            stmt = select(User).where((User.username.ilike(like)) | (User.name.ilike(like))
                                      | (User.id == int(q) if q.strip().isdigit() else False)
                                      ).order_by(User.created_at.desc()).limit(200)
        users = (await s.execute(stmt)).scalars().all()
    return render(request, "users.html", users=users, q=q, now=utcnow())


@router.post("/users/{uid}/days")
async def user_days(request: Request, uid: int, csrf: str = Form(""), days: int = Form(0)):
    if not authed(request):
        return back(request, "/login")
    if not check_csrf(request, csrf):
        return back(request, "/users", "نشست منقضی شد.")
    async with Session() as s:
        u = await s.get(User, uid)
        if u:
            base = u.expires_at if (u.expires_at and u.expires_at > utcnow()) else utcnow()
            u.expires_at = base + timedelta(days=days)
            await s.commit()
    if u and manager.bot and days > 0:
        try:
            await manager.bot.send_message(uid, f"🎁 اشتراک شما {days} روز تمدید شد.")
        except Exception:
            pass
    return back(request, "/users", "اشتراک به‌روزرسانی شد.")


@router.post("/users/{uid}/ban")
async def user_ban(request: Request, uid: int, csrf: str = Form("")):
    if not authed(request):
        return back(request, "/login")
    if not check_csrf(request, csrf):
        return back(request, "/users", "نشست منقضی شد.")
    async with Session() as s:
        u = await s.get(User, uid)
        if u:
            u.banned = not u.banned
            await s.commit()
    return back(request, "/users", "انجام شد.")


# ------------------------------------------------------------------ payments
@router.get("/payments")
async def payments_page(request: Request):
    if not authed(request):
        return back(request, "/login")
    async with Session() as s:
        rows = (await s.execute(select(Payment, User).join(User, User.id == Payment.user_id)
                                .order_by(Payment.id.desc()).limit(100))).all()
    return render(request, "payments.html", rows=rows)


@router.post("/payments/{pid}/{action}")
async def payment_action(request: Request, pid: int, action: str, csrf: str = Form("")):
    if not authed(request):
        return back(request, "/login")
    if not check_csrf(request, csrf) or action not in ("ok", "no"):
        return back(request, "/payments", "درخواست نامعتبر.")
    async with Session() as s:
        pay = await s.get(Payment, pid)
        if not pay or pay.status != "pending":
            return back(request, "/payments", "قبلاً بررسی شده.")
        u = await s.get(User, pay.user_id)
        if action == "ok":
            pay.status = "approved"
            base = u.expires_at if (u.expires_at and u.expires_at > utcnow()) else utcnow()
            u.expires_at = base + timedelta(days=pay.days)
            note = f"✅ پرداخت شما تأیید شد. اشتراک تا <b>{fmt_dt(u.expires_at)}</b> فعال است."
        else:
            pay.status = "rejected"
            note = "❌ رسید شما تأیید نشد. در صورت اشتباه با پشتیبانی تماس بگیرید."
        await s.commit()
        uid = u.id
    if manager.bot:
        try:
            await manager.bot.send_message(uid, note)
        except Exception:
            pass
    return back(request, "/payments", "انجام شد.")


# ------------------------------------------------------------------ AI agent
@router.get("/agent")
async def agent_page(request: Request):
    if not authed(request):
        return back(request, "/login")
    since = utcnow() - timedelta(days=7)
    async with Session() as s:
        by_status = dict((await s.execute(select(Candidate.status, func.count()).where(
            Candidate.created_at > since).group_by(Candidate.status))).all())
        events = (await s.execute(select(PolicyEvent, User).outerjoin(User, User.id == PolicyEvent.user_id)
                                  .order_by(PolicyEvent.id.desc()).limit(100))).all()
        modes = dict((await s.execute(select(AgentProfile.mode, func.count()).group_by(AgentProfile.mode))).all())
        suspended = (await s.execute(select(AgentProfile, User).join(User, User.id == AgentProfile.user_id).where(
            AgentProfile.suspended_until > utcnow()))).all()
        tokens = (await s.execute(select(func.coalesce(func.sum(AgentUsage.tokens), 0)).where(
            AgentUsage.day >= (utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")))).scalar_one()
        top = (await s.execute(select(PolicyEvent.user_id, func.count().label("n")).where(
            PolicyEvent.kind != "content", PolicyEvent.created_at > since).group_by(PolicyEvent.user_id)
            .order_by(func.count().desc()).limit(5))).all()
    c = await ai.conf()
    return render(request, "agent.html", by_status=by_status, events=events, modes=modes, suspended=suspended,
                  tokens=tokens, top=top, ai_ready=bool(c["key"]), ai_conf=c)


@router.post("/agent/test")
async def agent_test(request: Request, csrf: str = Form("")):
    if not authed(request):
        return back(request, "/login")
    if not check_csrf(request, csrf):
        return back(request, "/agent", "نشست منقضی شد.")
    c = await ai.conf()
    try:
        r = await ai.complete("Answer with the single word: OK", [{"role": "user", "content": "ping"}], kind="fast", max_tokens=10)
        return back(request, "/agent", f"✅ اتصال برقرار است · ارائه‌دهنده {c['provider']} · مدل سریع {c['fast']} · پاسخ: {r.text.strip()[:20]}")
    except ai.AIError as e:
        return back(request, "/agent", f"❌ خطا در اتصال به هوش مصنوعی: {str(e)[:200]}")


@router.post("/agent/suspend/{uid}")
async def agent_suspend(request: Request, uid: int, csrf: str = Form("")):
    if not authed(request):
        return back(request, "/login")
    if not check_csrf(request, csrf):
        return back(request, "/agent", "نشست منقضی شد.")
    async with Session() as s:
        p = await s.get(AgentProfile, uid)
        if p and p.suspended_until and p.suspended_until > utcnow():
            p.suspended_until = None
            msg = "تعلیق برداشته شد."
        else:
            if not p:
                p = AgentProfile(user_id=uid)
                s.add(p)
            p.suspended_until, p.mode = utcnow() + timedelta(days=1), "off"
            msg = "دستیار این کاربر ۲۴ ساعت تعلیق شد."
        await s.commit()
    return back(request, "/agent", msg)
