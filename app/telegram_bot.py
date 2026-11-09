from __future__ import annotations

import asyncio
import hashlib
import io
import json
import secrets
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from sqlalchemy import select

from .agent import run_agent
from .config import settings
from .db import SessionLocal
from .llm import audio_request, ProviderUnavailable
from .models import AppSetting, Conversation, Message as StoredMessage, ShareLink, User, utcnow
from .quota import estimate_tokens, QuotaExceeded
from .rendering import split_telegram_html, to_telegram_html


router = Router()


async def _user_and_conversation(message: Message, *, create_new: bool = False) -> tuple[User, Conversation]:
    sender = message.from_user
    if sender is None:
        raise RuntimeError("Telegram sender is missing")
    async with SessionLocal() as db:
        user = await db.get(User, sender.id)
        if user is None:
            user = User(
                telegram_id=sender.id,
                username=sender.username,
                first_name=sender.first_name,
            )
            db.add(user)
            await db.flush()
        else:
            user.username = sender.username
            user.first_name = sender.first_name

        conversation = None
        if not create_new and user.active_conversation_id:
            conversation = await db.get(Conversation, user.active_conversation_id)
            if conversation is not None and conversation.user_id != sender.id:
                conversation = None
        if conversation is None:
            conversation = Conversation(
                id=str(uuid.uuid4()),
                user_id=sender.id,
                title="Новый диалог",
            )
            db.add(conversation)
            user.active_conversation_id = conversation.id
        await db.commit()
        await db.refresh(user)
        await db.refresh(conversation)
        return user, conversation


async def _save_user_message(conversation: Conversation, content: str) -> None:
    async with SessionLocal() as db:
        row = await db.get(Conversation, conversation.id)
        if row is None:
            raise RuntimeError("Conversation no longer exists")
        if row.title == "Новый диалог":
            row.title = (content.splitlines()[0].strip() or "Новый диалог")[:180]
        row.updated_at = utcnow()
        db.add(StoredMessage(
            id=str(uuid.uuid4()),
            conversation_id=conversation.id,
            role="user",
            content=content,
            estimated_tokens=estimate_tokens(content),
        ))
        await db.commit()


async def _setting_bool(key: str, default: bool = False) -> bool:
    async with SessionLocal() as db:
        row = await db.get(AppSetting, key)
        if row is None:
            return default
        return str(row.value).strip().lower() in {"1", "true", "yes", "on"}


async def _download_telegram_file(bot: Bot, file_id: str, max_bytes: int) -> tuple[bytes, str]:
    info = await bot.get_file(file_id)
    if info.file_size and info.file_size > max_bytes:
        raise ValueError("Файл превышает допустимый размер.")
    if not info.file_path:
        raise ValueError("Telegram не вернул путь к файлу.")
    buffer = io.BytesIO()
    await bot.download_file(info.file_path, destination=buffer)
    content = buffer.getvalue()
    if len(content) > max_bytes:
        raise ValueError("Файл превышает допустимый размер.")
    return content, info.file_path


def _extract_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".json", ".xml", ".html", ".py", ".js", ".css", ".sql", ".log"}:
        return path.read_text(encoding="utf-8", errors="replace")[:40000]
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        chunks = []
        for page in reader.pages[:100]:
            chunks.append(page.extract_text() or "")
            if sum(map(len, chunks)) >= 40000:
                break
        return "\n".join(chunks)[:40000]
    if suffix == ".docx":
        from docx import Document
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)[:40000]
    if suffix == ".xlsx":
        from openpyxl import load_workbook
        book = load_workbook(str(path), read_only=True, data_only=True)
        chunks = []
        for sheet in book.worksheets[:10]:
            chunks.append("Лист: " + sheet.title)
            for row in sheet.iter_rows(min_row=1, max_row=1000, values_only=True):
                chunks.append(" | ".join("" if item is None else str(item) for item in row[:30]))
                if sum(map(len, chunks)) >= 40000:
                    break
            if sum(map(len, chunks)) >= 40000:
                break
        book.close()
        return "\n".join(chunks)[:40000]
    if suffix == ".pptx":
        from pptx import Presentation
        presentation = Presentation(str(path))
        chunks = []
        for slide in presentation.slides[:100]:
            chunks.append("\n".join(shape.text for shape in slide.shapes if getattr(shape, "has_text_frame", False)))
        return "\n\n".join(chunks)[:40000]
    return "Формат файла не поддерживает извлечение текста. Название: " + path.name


async def _transcribe_voice(bot: Bot, message: Message) -> str:
    media = message.voice or message.audio
    if media is None:
        raise ValueError("Voice file is missing")
    content, path = await _download_telegram_file(bot, media.file_id, settings.max_upload_bytes)
    suffix = Path(path).suffix or ".ogg"
    result, content_type, provider = await audio_request(
        "stt",
        "/audio/transcriptions",
        data={"response_format": "json"},
        file_field=("voice" + suffix, content, "audio/ogg" if suffix == ".ogg" else "application/octet-stream"),
    )
    try:
        payload = json.loads(result.decode("utf-8"))
        transcript = str(payload.get("text") or "").strip()
    except (UnicodeError, ValueError, AttributeError):
        transcript = result.decode("utf-8", errors="replace").strip()
    if not transcript:
        raise RuntimeError("Распознавание речи вернуло пустой текст.")
    return transcript


async def _create_share(user_id: int, conversation_id: str) -> str:
    if not await _setting_bool("shares_enabled", True):
        raise RuntimeError("Создание ссылок отключено администратором.")
    base = (settings.public_base_url or settings.telegram_webhook_url.rsplit(settings.webhook_path, 1)[0]).rstrip("/")
    if not base:
        raise RuntimeError("PUBLIC_BASE_URL or TELEGRAM_WEBHOOK_URL must be configured to create share links")
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with SessionLocal() as db:
        db.add(ShareLink(
            token_hash=token_hash,
            conversation_id=conversation_id,
            owner_id=user_id,
            expires_at=utcnow() + timedelta(days=30),
        ))
        await db.commit()
    return base + "/s/" + token


async def _send_html(message: Message, text: str) -> None:
    rendered = to_telegram_html(text)
    for part in split_telegram_html(rendered):
        try:
            await message.answer(part, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            plain = re_strip_html(part)
            await message.answer(plain[:4000] or "Ответ не удалось отобразить.")


def re_strip_html(value: str) -> str:
    from html import unescape
    import re
    return unescape(re.sub(r"<[^>]*>", "", value or ""))


@router.message(Command("start"))
async def start_command(message: Message) -> None:
    if message.chat.type != ChatType.PRIVATE:
        return
    await _user_and_conversation(message)
    await message.answer("Отправьте сообщение.")


@router.message(Command("new"))
async def new_command(message: Message) -> None:
    if message.chat.type != ChatType.PRIVATE:
        return
    await _user_and_conversation(message, create_new=True)
    await message.answer("Новый диалог.")


@router.message(Command("share"))
async def share_command(message: Message) -> None:
    if message.chat.type != ChatType.PRIVATE or message.from_user is None:
        return
    try:
        _, conversation = await _user_and_conversation(message)
        link = await _create_share(message.from_user.id, conversation.id)
        await message.answer(link, disable_web_page_preview=True)
    except Exception as exc:
        await message.answer(str(exc)[:300])


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if message.chat.type != ChatType.PRIVATE or message.from_user is None:
        return
    if message.from_user.id not in settings.admin_telegram_ids:
        return
    base = (settings.public_base_url or settings.telegram_webhook_url.rsplit(settings.webhook_path, 1)[0]).rstrip("/")
    if base:
        await message.answer(base + "/admin", disable_web_page_preview=True)


@router.message(F.chat.type == ChatType.PRIVATE)
async def handle_private_message(message: Message, bot: Bot) -> None:
    if message.from_user is None or not any((message.text, message.caption, message.document, message.voice, message.audio, message.photo)):
        return
    if message.text and message.text.startswith("/"):
        return

    status = await message.answer("Запрос обрабатывается\nШаг агента: подготовка")
    last_edit = 0.0
    Path(settings.temp_upload_root).mkdir(parents=True, exist_ok=True)

    async def on_phase(phase: str) -> None:
        nonlocal last_edit
        now = time.monotonic()
        if now - last_edit < 0.9:
            return
        last_edit = now
        try:
            await status.edit_text("Запрос обрабатывается\nШаг агента: " + phase[:100])
        except Exception:
            pass

    try:
        user, conversation = await _user_and_conversation(message)
        user_content = message.text or message.caption or ""
        temp_context = ""
        with tempfile.TemporaryDirectory(prefix=str(message.from_user.id) + "-", dir=settings.temp_upload_root) as temp_dir:
            if message.voice or message.audio:
                await on_phase("распознавание голосового сообщения")
                transcript = await _transcribe_voice(bot, message)
                user_content = (user_content + "\n" + transcript).strip()
            elif message.document:
                await on_phase("чтение временного файла")
                content, telegram_path = await _download_telegram_file(bot, message.document.file_id, settings.max_upload_bytes)
                name = Path(message.document.file_name or telegram_path).name
                path = Path(temp_dir) / name
                path.write_bytes(content)
                temp_context = _extract_document(path)
                user_content = (user_content + "\n\n[Временный файл: " + name + "]").strip()
            elif message.photo:
                # Binary images are not retained. The request remains text-only unless image input is configured later.
                user_content = (user_content + "\n[Пользователь приложил изображение; в этой сборке визуальная обработка не подключена.]").strip()

            if not user_content.strip():
                user_content = "Пользователь отправил вложение без текстового описания."
            await _save_user_message(conversation, user_content)
            await on_phase("анализ запроса")
            answer = await run_agent(
                user_id=user.telegram_id,
                conversation_id=conversation.id,
                status_callback=on_phase,
                ephemeral_context=temp_context,
            )

        try:
            await status.delete()
        except Exception:
            pass
        await _send_html(message, answer)

        if await _setting_bool("tts_auto_reply", False):
            try:
                from .tools import synthesize_speech
                audio, mime = await synthesize_speech(answer)
                await message.answer_audio(BufferedInputFile(audio, filename="fablai-answer.mp3"))
            except Exception:
                pass

    except QuotaExceeded as exc:
        try:
            await status.delete()
        except Exception:
            pass
        await message.answer(
            "Лимит использования исчерпан. Доступ восстановится после "
            + (exc.ends_at.isoformat() if exc.ends_at else "окончания 12-часового периода")
            + "."
        )
    except Exception:
        try:
            await status.delete()
        except Exception:
            pass
        await message.answer("Не удалось выполнить запрос. Попробуйте ещё раз позже.")
