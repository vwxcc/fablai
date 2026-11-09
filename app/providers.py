from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from .crypto import decrypt_secret
from .db import SessionLocal
from .models import ServiceCall, ServiceConnection, utcnow


@dataclass(frozen=True)
class Provider:
    id: int
    task_type: str
    label: str
    base_url: str
    model: str
    api_key: str
    max_parallel: int
    request_limit_12h: int
    timeout_seconds: int
    options: dict[str, Any]


_semaphores: dict[int, asyncio.Semaphore] = {}
_semaphore_locks: dict[int, asyncio.Lock] = {}


async def get_provider_pool(task_type: str) -> list[Provider]:
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(ServiceConnection)
            .where(ServiceConnection.task_type == task_type, ServiceConnection.enabled.is_(True))
            .order_by(ServiceConnection.priority.asc(), ServiceConnection.id.asc())
        )).scalars().all()
        result: list[Provider] = []
        for row in rows:
            if row.request_limit_12h > 0:
                since = utcnow() - timedelta(hours=12)
                count = await db.scalar(
                    select(func.count(ServiceCall.id)).where(
                        ServiceCall.connection_id == row.id,
                        ServiceCall.created_at >= since,
                        ServiceCall.success.is_(True),
                    )
                )
                if int(count or 0) >= row.request_limit_12h:
                    continue
            import json
            try:
                options = json.loads(row.options_json or "{}")
            except (TypeError, ValueError):
                options = {}
            result.append(Provider(
                id=row.id,
                task_type=row.task_type,
                label=row.label,
                base_url=row.base_url.rstrip("/"),
                model=row.model,
                api_key=decrypt_secret(row.encrypted_api_key),
                max_parallel=max(1, min(50, row.max_parallel)),
                request_limit_12h=row.request_limit_12h,
                timeout_seconds=max(5, min(1800, row.timeout_seconds)),
                options=options if isinstance(options, dict) else {},
            ))
        return result


async def provider_semaphore(provider: Provider) -> asyncio.Semaphore:
    lock = _semaphore_locks.setdefault(provider.id, asyncio.Lock())
    async with lock:
        sem = _semaphores.get(provider.id)
        if sem is None or getattr(sem, "_value", None) is None:
            sem = asyncio.Semaphore(provider.max_parallel)
            _semaphores[provider.id] = sem
        return sem


async def record_service_call(provider_id: int, success: bool) -> None:
    async with SessionLocal() as db:
        db.add(ServiceCall(connection_id=provider_id, success=success))
        await db.commit()
