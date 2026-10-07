"""Запуск загрузки по всем источникам.

Каждый источник загружается независимо: сбой одного (недоступен, таймаут, сломанный
формат) записывается в журнал и не мешает остальным. Итоговый статус запуска:
success — все источники отработали, failed — ни один, partial — что-то между.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from weather.config import Settings, today_in
from weather.http_client import HttpClient, SourceError
from weather.ingest.base import Loader
from weather.ingest.journal import Journal, LoadStats
from weather.ingest.meteostat import MeteostatLoader
from weather.ingest.openmeteo import OpenMeteoArchiveLoader, OpenMeteoForecastLoader

log = logging.getLogger(__name__)

LOADERS: tuple[type[Loader], ...] = (OpenMeteoArchiveLoader, OpenMeteoForecastLoader, MeteostatLoader)
SOURCE_CODES = tuple(loader.source_code for loader in LOADERS)
# Не даёт двум загрузкам идти одновременно (например, ручной и по расписанию)
INGEST_LOCK_KEY = 27612_0002


class IngestBusy(RuntimeError):
    """Другая загрузка уже выполняется."""


@dataclass(frozen=True)
class SourceOutcome:
    source_code: str
    load_id: int
    status: str
    stats: LoadStats
    period: tuple[date, date] | None
    watermark_before: date | None
    watermark_after: date | None
    error: str | None


@dataclass(frozen=True)
class IngestResult:
    run_id: int
    status: str
    outcomes: list[SourceOutcome]


def run_ingest(
    settings: Settings,
    conn: Any,
    *,
    sources: Iterable[str] | None = None,
    trigger: str = "manual",
    http: HttpClient | None = None,
    today: date | None = None,
) -> IngestResult:
    """Загружает данные выбранных (по умолчанию всех включённых) источников."""
    selected = set(sources) if sources else None
    unknown = (selected or set()) - set(SOURCE_CODES)
    if unknown:
        raise ValueError(
            f"неизвестные источники: {', '.join(sorted(unknown))}; есть: {', '.join(SOURCE_CODES)}"
        )

    today = today or today_in(settings.timezone)
    http = http or HttpClient(settings.http)
    journal = Journal(conn)

    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (INGEST_LOCK_KEY,)).fetchone()[0]:
        raise IngestBusy("загрузка уже выполняется другим процессом")
    try:
        loaders = [
            cls(settings, http, conn, today)
            for cls in LOADERS
            if selected is None or cls.source_code in selected
        ]
        run_id = journal.start_run(
            trigger, {"sources": [loader.source_code for loader in loaders], "today": today}
        )
        log.info("Запуск загрузки №%d (%s), дата %s", run_id, trigger, today)
        outcomes = [_load_source(loader, journal, http, run_id) for loader in loaders]
        status = _overall_status([outcome.status for outcome in outcomes])
        failed = [o.source_code for o in outcomes if o.status in ("failed", "partial")]
        journal.finish_run(run_id, status, f"с ошибками: {', '.join(failed)}" if failed else None)
        log.info("Загрузка №%d завершена: %s", run_id, status)
        return IngestResult(run_id, status, outcomes)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (INGEST_LOCK_KEY,))


def _load_source(loader: Loader, journal: Journal, http: HttpClient, run_id: int) -> SourceOutcome:
    code = loader.source_code
    stats = LoadStats()
    watermark_before = journal.watermark(code)

    if not loader.enabled:
        plan = None
        stats.notes.append("источник отключён в настройках")
    else:
        plan = loader.plan(watermark_before)
        if plan is None:
            stats.notes.append(loader.nothing_to_do)

    load_id = journal.start_load(
        run_id, code, plan.period_from if plan else None, plan.period_to if plan else None, watermark_before
    )
    status, error = "skipped", None
    if plan is not None:
        http.reset_stats()
        try:
            loader.load(plan, load_id, stats, lambda day: journal.advance_watermark(code, day, load_id))
            status = "partial" if stats.payloads_invalid else "success"
        except SourceError as exc:
            # Часть порций могла успеть сохраниться — тогда загрузка частичная,
            # следующий запуск продолжит с отметки
            saved = stats.payloads_new + stats.payloads_unchanged
            status, error = ("partial" if saved else "failed"), str(exc)
            log.error("%s: %s", code, exc)
        except Exception as exc:  # непредвиденная ошибка тоже должна попасть в журнал
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            log.exception("%s: непредвиденная ошибка", code)
        stats.requests, stats.retries = http.requests, http.retries

    watermark_after = journal.watermark(code)
    journal.finish_load(load_id, status, stats, watermark_after, error)
    period = (plan.period_from, plan.period_to) if plan else None
    log.info("%s: %s, отметка %s → %s", code, status, watermark_before, watermark_after)
    return SourceOutcome(code, load_id, status, stats, period, watermark_before, watermark_after, error)


def _overall_status(statuses: list[str]) -> str:
    if all(s in ("success", "skipped") for s in statuses):
        return "success"
    if all(s == "failed" for s in statuses):
        return "failed"
    return "partial"
