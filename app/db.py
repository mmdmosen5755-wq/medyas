from datetime import datetime, timezone

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
                        inspect, select, text)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from . import config
from .crypto import dec, enc


def utcnow() -> datetime:
    """Naive UTC datetime (all DB datetimes are naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(128), default="")
    eitaa_token_enc: Mapped[str] = mapped_column(Text, default="")
    eitaa_account: Mapped[str] = mapped_column(String(128), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    chat_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(128))
    platform: Mapped[str] = mapped_column(String(10), default="eitaa", server_default="eitaa")  # eitaa | rubika


class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    channel_id: Mapped[int] = mapped_column(Integer, index=True)  # no FK: channels can be deleted
    kind: Mapped[str] = mapped_column(String(8), default="text")  # text | file
    text: Mapped[str] = mapped_column(Text, default="")  # message text or file caption
    file_id: Mapped[str] = mapped_column(String(256), default="")  # telegram file_id
    file_name: Mapped[str] = mapped_column(String(256), default="")
    title: Mapped[str] = mapped_column(String(256), default="")
    silent: Mapped[bool] = mapped_column(Boolean, default=False)
    pin: Mapped[bool] = mapped_column(Boolean, default=False)
    view_delete: Mapped[int] = mapped_column(Integer, default=0)
    run_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    # pending | sending | sent | failed | cancelled
    error: Mapped[str] = mapped_column(Text, default="")
    file_url: Mapped[str] = mapped_column(String(512), default="", server_default="")  # remote image (AI agent posts)
    eitaa_message_id: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    days: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending|approved|rejected
    receipt_file_id: Mapped[str] = mapped_column(String(256), default="")
    receipt_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ------------------------------------------------------------------ AI content agent
class SourceChannel(Base):
    """A public Eitaa channel the user wants the agent to read content from."""
    __tablename__ = "source_channels"
    __table_args__ = (UniqueConstraint("user_id", "handle", name="uq_source_user_handle"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    handle: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(128), default="")
    subscribers: Mapped[str] = mapped_column(String(32), default="")
    description: Mapped[str] = mapped_column(String(300), default="")
    last_msg_id: Mapped[int] = mapped_column(Integer, default=0)  # highest post id already ingested
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    fails: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(200), default="")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Candidate(Base):
    """One post found in a source channel, with the agent's verdict."""
    __tablename__ = "candidates"
    __table_args__ = (UniqueConstraint("user_id", "source_id", "msg_id", name="uq_cand"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    source_id: Mapped[int] = mapped_column(Integer, index=True)
    msg_id: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(String(512), default="")
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)  # video/file we cannot republish
    views: Mapped[int] = mapped_column(Integer, default=0)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(40), default="", index=True)
    # new | accepted | proposed | published | rejected | duplicate | blocked
    status: Mapped[str] = mapped_column(String(12), default="new", index=True)
    ad_score: Mapped[int] = mapped_column(Integer, default=0)
    relevance: Mapped[int] = mapped_column(Integer, default=0)
    quality: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str] = mapped_column(String(48), default="")
    verdict: Mapped[str] = mapped_column(String(300), default="")
    rewritten: Mapped[str] = mapped_column(Text, default="")
    post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_by: Mapped[str] = mapped_column(String(8), default="")  # rule | ai | user
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentProfile(Base):
    __tablename__ = "agent_profiles"
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True, autoincrement=False)
    target_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Channel.id the agent posts to
    niche: Mapped[str] = mapped_column(Text, default="")          # what my channel is about
    tone: Mapped[str] = mapped_column(Text, default="")           # writing voice
    instructions: Mapped[str] = mapped_column(Text, default="")   # the user's own standing orders
    mode: Mapped[str] = mapped_column(String(8), default="off")   # off | review | auto
    interval_min: Mapped[int] = mapped_column(Integer, default=30)
    max_per_day: Mapped[int] = mapped_column(Integer, default=5)
    min_relevance: Mapped[int] = mapped_column(Integer, default=65)
    rewrite: Mapped[bool] = mapped_column(Boolean, default=True)
    attribution: Mapped[bool] = mapped_column(Boolean, default=True)
    footer: Mapped[str] = mapped_column(String(300), default="")
    suspended_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentMsg(Base):
    """Chat memory of the agent (only user text + final answers are kept)."""
    __tablename__ = "agent_msgs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PolicyEvent(Base):
    """Every time the guard blocked something. Shown in the web panel and DM'd to admins."""
    __tablename__ = "policy_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(12))  # request | action | settings | content
    request: Mapped[str] = mapped_column(Text, default="")
    rule: Mapped[str] = mapped_column(String(300), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(String(48), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AgentUsage(Base):
    __tablename__ = "agent_usage"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_usage_day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD (UTC)
    msgs: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    auto_posts: Mapped[int] = mapped_column(Integer, default=0)


engine = create_async_engine(config.DATABASE_URL, pool_pre_ping=True, connect_args=config.DB_CONNECT_ARGS)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # tiny migration: databases created before Rubika support lack channels.platform
        cols = await conn.run_sync(lambda c: [x["name"] for x in inspect(c).get_columns("channels")])
        if "platform" not in cols:
            await conn.execute(text("ALTER TABLE channels ADD COLUMN platform VARCHAR(10) DEFAULT 'eitaa'"))
        # ... and databases created before the AI agent lack posts.file_url
        pcols = await conn.run_sync(lambda c: [x["name"] for x in inspect(c).get_columns("posts")])
        if "file_url" not in pcols:
            await conn.execute(text("ALTER TABLE posts ADD COLUMN file_url VARCHAR(512) DEFAULT ''"))


# ---------------------------------------------------------------- settings
DEFAULTS = {
    "bot_token": "",
    "rubika_bot_token": "",
    "admin_ids": "",
    "brand_name": "ربات انتشار",
    "welcome_text": "",
    "support_username": "",
    "card_number": "",
    "card_owner": "",
    "trial_days": "3",
    "plans": "30:150000\n90:400000\n365:1400000",
    # --- AI agent (all editable from the web panel)
    "ai_provider": "anthropic",       # anthropic | openai (any OpenAI-compatible endpoint)
    "ai_api_key": "",
    "ai_base_url": "",                # blank = provider default
    "ai_model_chat": "",              # blank = default (claude-sonnet-5-5)
    "ai_model_fast": "",              # blank = default (claude-haiku-5-5)
    "agent_enabled": "1",             # global kill switch
    "agent_rules": "",                # admin rules, plain Persian text; they outrank anything a user says
    "agent_blocked_words": "",        # one per line / comma separated; hard block, no AI involved
    "agent_max_msgs_day": "40",       # chat messages per user per day
    "agent_max_auto_day": "10",       # auto-published posts per user per day (hard cap)
    "agent_allow_auto": "1",          # may users switch the agent to fully automatic mode?
    "agent_max_sources": "15",
    "agent_force_attr": "0",          # 1 = always append "منبع: @channel"
    "agent_violation_limit": "5",     # violations within 24h before the agent is suspended for that user (0 = never)
}
SECRET_KEYS = {"bot_token", "rubika_bot_token", "ai_api_key"}


async def get_settings(s: AsyncSession | None = None) -> dict[str, str]:
    async def _load(sess: AsyncSession) -> dict[str, str]:
        rows = (await sess.execute(select(Setting))).scalars().all()
        out = dict(DEFAULTS)
        for r in rows:
            out[r.key] = dec(r.value) if r.key in SECRET_KEYS else r.value
        if not out["bot_token"] and config.ENV_BOT_TOKEN:
            out["bot_token"] = config.ENV_BOT_TOKEN
        if not out["rubika_bot_token"] and config.ENV_RUBIKA_TOKEN:
            out["rubika_bot_token"] = config.ENV_RUBIKA_TOKEN
        if not out["ai_api_key"] and config.ENV_AI_KEY:
            out["ai_api_key"] = config.ENV_AI_KEY
        if not out["admin_ids"] and config.ENV_ADMIN_IDS:
            out["admin_ids"] = config.ENV_ADMIN_IDS
        return out

    if s is not None:
        return await _load(s)
    async with Session() as sess:
        return await _load(sess)


async def save_settings(values: dict[str, str]) -> None:
    async with Session() as s:
        for k, v in values.items():
            if k not in DEFAULTS:
                continue
            v = enc(v) if k in SECRET_KEYS else v
            row = await s.get(Setting, k)
            if row:
                row.value = v
            else:
                s.add(Setting(key=k, value=v))
        await s.commit()


def parse_ids(raw: str) -> set[int]:
    out = set()
    for part in raw.replace("،", ",").replace("\n", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def parse_plans(raw: str) -> list[tuple[int, int]]:
    """'30:150000' per line -> [(days, price_toman)]"""
    plans = []
    for line in raw.splitlines():
        if ":" not in line:
            continue
        d, p = line.split(":", 1)
        d, p = d.strip(), p.strip().replace(",", "")
        if d.isdigit() and p.isdigit() and int(d) > 0:
            plans.append((int(d), int(p)))
    return plans
