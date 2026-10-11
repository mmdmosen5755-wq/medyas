import asyncio
import contextlib
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .bot import manager
from .db import init_db
from .curator import curator_loop
from .scheduler import scheduler_loop
from .rubika_hub import hub_loop
from .web import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")
# httpx logs full URLs, which contain the Eitaayar token -> keep them out of the logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sched = hub = cur = None
    if not config.MISSING:
        await init_db()
        sched = asyncio.create_task(scheduler_loop(manager))
        hub = asyncio.create_task(hub_loop(manager))
        cur = asyncio.create_task(curator_loop(manager))
        ok = await manager.start()
        if not ok:
            log.warning("bot not started: %s", manager.error)
    else:
        log.error("missing env vars: %s", config.MISSING)
    try:
        yield
    finally:
        for t in (sched, hub, cur):
            if t:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        await manager.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY or secrets.token_hex(16),
    max_age=60 * 60 * 24 * 7,
    same_site="lax",
    https_only=bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("COOKIE_SECURE")),
)


@app.middleware("http")
async def guard_config(request: Request, call_next):
    if request.url.path == "/health":
        return PlainTextResponse("ok")
    if config.MISSING:
        items = "".join(f"<li><code>{m}</code></li>" for m in config.MISSING)
        return HTMLResponse(
            "<html dir='rtl'><body style='font-family:sans-serif;max-width:560px;margin:60px auto'>"
            "<h2>⚙️ تنظیمات ناقص است</h2><p>این متغیرها را در Railway → Variables اضافه کنید و Redeploy بزنید:</p>"
            f"<ul>{items}</ul></body></html>", status_code=503)
    return await call_next(request)


app.include_router(router)
