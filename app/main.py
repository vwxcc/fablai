from __future__ import annotations

import asyncio
import hashlib
import html
import logging
from datetime import timezone
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select

from .admin import router as admin_router
from .config import settings
from .db import SessionLocal, init_db, dispose_db
from .models import AppSetting, Conversation, Message, ShareLink
from .telegram_bot import router as telegram_router


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fablai")

app = FastAPI(title="fablai", docs_url=None, redoc_url=None, openapi_url=None)
session_secret = settings.app_secret if len(settings.app_secret) >= 32 else "fablai-local-development-session-secret-change-me"
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    session_cookie="fablai_admin",
    same_site="strict",
    https_only=bool(settings.telegram_webhook_url.startswith("https://")),
    max_age=60 * 60 * 12,
)
app.include_router(admin_router)

bot: Bot | None = None
dispatcher = Dispatcher()
polling_task: asyncio.Task | None = None


@app.on_event("startup")
async def startup() -> None:
    global bot, polling_task
    await init_db()
    Path(settings.temp_upload_root).mkdir(parents=True, exist_ok=True)
    if len(settings.app_secret) < 32:
        log.error("APP_SECRET is missing or too short; admin provider-secret encryption will be unavailable")
    if not settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN is not configured; the admin site and health endpoint will still start")
        return

    bot = Bot(token=settings.telegram_bot_token)
    dispatcher.include_router(telegram_router)

    if settings.telegram_webhook_url.strip():
        if not settings.telegram_webhook_secret or len(settings.telegram_webhook_secret) < 16:
            raise RuntimeError("TELEGRAM_WEBHOOK_SECRET with at least 16 characters is required for webhook mode")
        target = settings.telegram_webhook_url.strip()
        await bot.set_webhook(
            url=target,
            secret_token=settings.telegram_webhook_secret,
            allowed_updates=dispatcher.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        log.info("Telegram webhook configured")
    elif settings.telegram_polling:
        await bot.delete_webhook(drop_pending_updates=False)
        polling_task = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                handle_signals=False,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
        )
        log.info("Telegram long polling started")


@app.on_event("shutdown")
async def shutdown() -> None:
    global bot, polling_task
    if polling_task is not None:
        try:
            await dispatcher.stop_polling()
            await asyncio.wait_for(polling_task, timeout=10)
        except Exception:
            polling_task.cancel()
        polling_task = None
    if bot is not None:
        await bot.session.close()
        bot = None
    await dispose_db()


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "fablai"}


@app.post(settings.webhook_path)
async def telegram_webhook(request: Request):
    if bot is None:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")
    if not settings.telegram_webhook_url:
        raise HTTPException(status_code=404, detail="Webhook mode is not enabled")
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not settings.telegram_webhook_secret or supplied != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": bot})
        await dispatcher.feed_update(bot, update)
    except Exception:
        log.exception("Could not process Telegram update")
        raise HTTPException(status_code=400, detail="Invalid update")
    return JSONResponse({"ok": True})


@app.get("/s/{token}", response_class=HTMLResponse)
async def public_share(token: str):
    if not token or len(token) > 128:
        raise HTTPException(status_code=404, detail="Share link not found")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with SessionLocal() as db:
        row = (await db.execute(
            select(ShareLink).where(ShareLink.token_hash == token_hash, ShareLink.revoked.is_(False))
        )).scalars().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Share link not found")
        expiry = row.expires_at
        if expiry is not None:
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= __import__("datetime").datetime.now(timezone.utc):
                raise HTTPException(status_code=404, detail="Share link expired")
        conversation = await db.get(Conversation, row.conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages = (await db.execute(
            select(Message).where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.asc()).limit(500)
        )).scalars().all()
        title = html.escape(conversation.title or "Shared conversation")
        content = "\n\n".join(
            ("USER" if item.role == "user" else "FABLAI") + "\n" + (item.content or "")
            for item in messages if item.role in {"user", "assistant"}
        )
    safe_content = html.escape(content)
    page = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>""" + title + """</title>
<style>body{margin:0;background:#f4f1eb;color:#28241f;font:15px/1.6 system-ui,sans-serif}.wrap{max-width:900px;margin:40px auto;padding:0 18px}article{background:#fff;border:1px solid #e1dbd1;border-radius:16px;padding:24px;white-space:pre-wrap;overflow-wrap:anywhere}h1{font-size:23px;font-weight:600}</style></head><body><div class="wrap"><h1>""" + title + """</h1><article>""" + safe_content + """</article></div></body></html>"""
    return HTMLResponse(page)


@app.get("/")
async def index():
    return {"service": "fablai", "health": "/healthz", "admin": "/admin"}
