"""应用内后台定时任务（asyncio）。"""
from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.database import SessionLocal

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_stop = asyncio.Event()


def _run_maintenance_once() -> None:
    from app.services.oa_scheduled import run_scheduled_oa_maintenance

    db = SessionLocal()
    try:
        run_scheduled_oa_maintenance(db)
    finally:
        db.close()


async def _oa_maintenance_loop() -> None:
    while not _stop.is_set():
        settings = get_settings()
        minutes = int(settings.oa_scheduled_sync_minutes or 0)
        if minutes <= 0:
            # 关闭时短睡，便于运行中改配置后生效
            try:
                await asyncio.wait_for(_stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                continue
            continue

        try:
            # 同步 DB 操作放线程池，避免阻塞事件循环
            await asyncio.to_thread(_run_maintenance_once)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OA 定时维护失败: %s", type(exc).__name__)

        try:
            await asyncio.wait_for(_stop.wait(), timeout=max(60, minutes * 60))
        except asyncio.TimeoutError:
            continue


def start_background_tasks() -> None:
    global _task
    settings = get_settings()
    if int(settings.oa_scheduled_sync_minutes or 0) <= 0:
        logger.info("OA 定时维护未启用（OA_SCHEDULED_SYNC_MINUTES=0）")
        # 仍启动循环，便于日后改为 >0 时生效（每分钟检查配置）
    _stop.clear()
    try:
        loop = asyncio.get_event_loop()
        if _task is None or _task.done():
            _task = loop.create_task(_oa_maintenance_loop())
            logger.info("已启动后台定时任务循环")
    except RuntimeError:
        logger.warning("无事件循环，跳过后台定时任务")


def stop_background_tasks() -> None:
    global _task
    _stop.set()
    if _task and not _task.done():
        _task.cancel()
    _task = None
