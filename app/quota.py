from __future__ import annotations

import asyncio
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import SessionLocal
from .models import AppSetting, QuotaWindow, utcnow


class QuotaExceeded(RuntimeError):
    def __init__(self, used: int, limit: int, ends_at) -> None:
        self.used = used
        self.limit = limit
        self.ends_at = ends_at
        super().__init__("The 12-hour token allowance has been exhausted")


_user_locks: dict[int, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _user_lock(user_id: int) -> asyncio.Lock:
    async with _locks_guard:
        return _user_locks.setdefault(user_id, asyncio.Lock())


async def reserve_tokens(user_id: int, amount: int) -> int:
    amount = max(1, int(amount))
    async with await _user_lock(user_id):
        async with SessionLocal() as db:
            async with db.begin():
                now = utcnow()
                limit_row = await db.get(AppSetting, "user_token_limit")
                window_row = await db.get(AppSetting, "user_limit_window_seconds")
                try:
                    dynamic_limit = int(limit_row.value) if limit_row else settings.user_token_limit
                except (TypeError, ValueError):
                    dynamic_limit = settings.user_token_limit
                try:
                    dynamic_window = int(window_row.value) if window_row else settings.user_limit_window_seconds
                except (TypeError, ValueError):
                    dynamic_window = settings.user_limit_window_seconds
                dynamic_limit = max(1_000, min(100_000_000, dynamic_limit))
                dynamic_window = max(60, min(604_800, dynamic_window))
                row = (await db.execute(
                    select(QuotaWindow).where(QuotaWindow.user_id == user_id).with_for_update()
                )).scalars().one_or_none()
                if row is None:
                    row = QuotaWindow(
                        user_id=user_id,
                        window_started_at=now,
                        window_ends_at=now + timedelta(seconds=dynamic_window),
                        used_tokens=0,
                        limit_tokens=dynamic_limit,
                    )
                    db.add(row)
                    try:
                        await db.flush()
                    except IntegrityError:
                        raise QuotaExceeded(0, dynamic_limit, now)
                elif row.window_ends_at <= now:
                    row.window_started_at = now
                    row.window_ends_at = now + timedelta(seconds=dynamic_window)
                    row.used_tokens = 0
                    row.limit_tokens = dynamic_limit
                row.limit_tokens = dynamic_limit
                if row.used_tokens + amount > row.limit_tokens:
                    raise QuotaExceeded(row.used_tokens, row.limit_tokens, row.window_ends_at)
                row.used_tokens += amount
                return amount


async def settle_tokens(user_id: int, reserved: int, actual: int | None) -> None:
    async with await _user_lock(user_id):
        async with SessionLocal() as db:
            async with db.begin():
                row = (await db.execute(
                    select(QuotaWindow).where(QuotaWindow.user_id == user_id).with_for_update()
                )).scalars().one_or_none()
                if row is None or row.window_ends_at <= utcnow():
                    return
                charge = max(0, min(int(actual or 0), int(reserved)))
                row.used_tokens = max(0, row.used_tokens - int(reserved) + charge)


async def release_tokens(user_id: int, reserved: int) -> None:
    await settle_tokens(user_id, reserved, 0)


def estimate_tokens(value: object) -> int:
    if isinstance(value, str):
        raw = value
    else:
        import json
        raw = json.dumps(value, ensure_ascii=False, default=str)
    return max(1, (len(raw.encode("utf-8")) + 3) // 4)


async def quota_status(user_id: int) -> dict:
    async with SessionLocal() as db:
        row = (await db.execute(
            select(QuotaWindow).where(QuotaWindow.user_id == user_id)
        )).scalars().one_or_none()
        limit_row = await db.get(AppSetting, "user_token_limit")
        try:
            dynamic_limit = int(limit_row.value) if limit_row else settings.user_token_limit
        except (TypeError, ValueError):
            dynamic_limit = settings.user_token_limit
        if row is None or row.window_ends_at <= utcnow():
            return {
                "used_tokens": 0,
                "limit_tokens": dynamic_limit,
                "remaining_tokens": dynamic_limit,
                "window_ends_at": None,
            }
        return {
            "used_tokens": row.used_tokens,
            "limit_tokens": row.limit_tokens,
            "remaining_tokens": max(0, row.limit_tokens - row.used_tokens),
            "window_ends_at": row.window_ends_at.isoformat(),
        }
