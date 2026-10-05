"""Подключение к PostgreSQL."""

from __future__ import annotations

import logging
import time

import psycopg

from weather.config import DatabaseSettings

log = logging.getLogger(__name__)

# Слои хранения: имя схемы -> назначение. Схемы создаются миграцией V001.
LAYERS: dict[str, str] = {
    "raw": "ответы источников без изменений",
    "staging": "очищенные и типизированные данные",
    "core": "единая модель данных: справочники, факты, история",
    "mart": "витрины для дашбордов и моделей",
    "meta": "служебные метаданные",
}


def connect(db: DatabaseSettings, *, autocommit: bool = False, timeout_s: int = 10) -> psycopg.Connection:
    return psycopg.connect(
        **db.connect_kwargs(),
        autocommit=autocommit,
        connect_timeout=timeout_s,
        application_name="moscow-weather",
    )


def wait_for_db(db: DatabaseSettings, *, timeout_s: float = 60, interval_s: float = 2) -> None:
    """Ждёт, пока база начнёт принимать подключения.

    Нужна при запуске в Docker: контейнер базы может стартовать дольше приложения.
    """
    deadline = time.monotonic() + timeout_s
    attempt = 0
    while True:
        attempt += 1
        try:
            with connect(db, autocommit=True, timeout_s=5) as conn:
                conn.execute("SELECT 1")
            if attempt > 1:
                log.info("База доступна (попытка %d)", attempt)
            return
        except psycopg.OperationalError as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"База {db.describe()} недоступна дольше {timeout_s:.0f} с: {exc}"
                ) from exc
            log.info("База %s пока недоступна, повтор через %.0f с", db.describe(), interval_s)
            time.sleep(interval_s)
