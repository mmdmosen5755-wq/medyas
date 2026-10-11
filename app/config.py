"""Environment configuration. Everything else (bot token, admin IDs, prices...) lives in the DB and is edited from the web panel."""
import os
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


SECRET_KEY = _env("SECRET_KEY")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD")

def _prepare_db(url: str) -> tuple[str, dict]:
    """Normalise DATABASE_URL for SQLAlchemy+asyncpg. Hosted Postgres (Neon, Supabase...) hands out
    `postgresql://...?sslmode=require[&channel_binding=require]`; asyncpg rejects those query params,
    so they are translated into connect_args (ssl, and no prepared-statement cache behind a pgbouncer pooler)."""
    from sqlalchemy.engine import make_url
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    u = make_url(url)
    args: dict = {}
    if u.drivername == "postgresql+asyncpg":
        q = dict(u.query)
        sslmode, ssl_q = q.pop("sslmode", None), q.pop("ssl", None)
        q.pop("channel_binding", None)
        if (sslmode and sslmode != "disable") or (ssl_q and str(ssl_q).lower() not in ("0", "false", "disable")):
            args["ssl"] = True
        if u.host and "-pooler" in u.host:  # pgbouncer transaction pooling cannot keep prepared statements
            args["statement_cache_size"] = 0
        u = u.set(query=q)
    return u.render_as_string(hide_password=False), args


DATABASE_URL, DB_CONNECT_ARGS = _prepare_db(_env("DATABASE_URL", "sqlite+aiosqlite:///./data.db"))

EITAA_API_BASE = _env("EITAA_API_BASE", "https://eitaayar.ir/api").rstrip("/")
EITAA_PROXY = _env("EITAA_PROXY") or None

RUBIKA_API_BASE = _env("RUBIKA_API_BASE", "https://botapi.rubika.ir/v3").rstrip("/")
RUBIKA_PROXY = _env("RUBIKA_PROXY") or None

# public Eitaa web (read-only scraping of source channels). Point at your own relay if eitaa.com blocks foreign IPs.
EITAA_WEB_BASE = _env("EITAA_WEB_BASE", "https://eitaa.com").rstrip("/")

# AI provider (the panel values win over these)
ENV_AI_KEY = _env("ANTHROPIC_API_KEY") or _env("AI_API_KEY")
AI_PROXY = _env("AI_PROXY") or None

# optional fallbacks, the panel values win
ENV_BOT_TOKEN = _env("BOT_TOKEN")
ENV_RUBIKA_TOKEN = _env("RUBIKA_BOT_TOKEN")
ENV_ADMIN_IDS = _env("ADMIN_IDS")

TZ = ZoneInfo("Asia/Tehran")

MISSING: list[str] = []
if len(SECRET_KEY) < 16:
    MISSING.append("SECRET_KEY (حداقل ۱۶ کاراکتر)")
if len(ADMIN_PASSWORD) < 6:
    MISSING.append("ADMIN_PASSWORD (حداقل ۶ کاراکتر)")
