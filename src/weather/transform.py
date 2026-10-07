"""Преобразования данных между слоями: raw -> staging -> core -> mart.

Шаги — SQL-файлы в sql/transform/. Они выполняются по порядку имён в одной
транзакции: если какой-то шаг упал, база остаётся как до запуска. Первая строка
файла «-- target: схема.таблица» говорит, какую таблицу шаг заполняет, — после
шага в журнал пишется, сколько в ней строк.

Параметры из настроек (станция, пороги, периоды) передаются в SQL через
set_config, в SQL они читаются как current_setting('weather.<имя>').
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weather.config import PROJECT_ROOT, Settings

log = logging.getLogger(__name__)

TRANSFORM_DIR = PROJECT_ROOT / "sql" / "transform"
TARGET_RE = re.compile(r"^--\s*target:\s*([a-z_]+\.[a-z_]+)\s*$", re.MULTILINE)
TRANSFORM_LOCK_KEY = 27612_0003


class TransformError(RuntimeError):
    """Шаг преобразования завершился ошибкой; изменения откатены."""


@dataclass(frozen=True)
class Step:
    name: str
    target: str
    sql: str


@dataclass(frozen=True)
class StepResult:
    step: str
    target: str
    status: str
    rows_after: int | None
    duration_ms: int


@dataclass(frozen=True)
class TransformResult:
    transform_run_id: int
    status: str
    steps: list[StepResult]


def discover_steps(directory: Path = TRANSFORM_DIR) -> list[Step]:
    steps = []
    for path in sorted(directory.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        match = TARGET_RE.search(sql)
        if not match:
            raise TransformError(f"{path.name}: нет строки «-- target: схема.таблица»")
        steps.append(Step(path.stem, match.group(1), sql))
    if not steps:
        raise TransformError(f"нет SQL-файлов преобразований в {directory}")
    return steps


def session_params(settings: Settings, transform_run_id: int) -> dict[str, str]:
    p = settings.processing
    return {
        "weather.transform_run_id": str(transform_run_id),
        "weather.station_id": settings.station.wmo_id,
        "weather.timezone": settings.timezone,
        "weather.history_start": settings.history_start.isoformat(),
        "weather.heating_threshold_c": str(settings.heating.threshold_c),
        "weather.heating_days": str(settings.heating.consecutive_days),
        "weather.ice_threshold_c": str(settings.ice_threshold_c),
        "weather.norm_from_year": str(p.climate_norm_from_year),
        "weather.norm_to_year": str(p.climate_norm_to_year),
        "weather.bias_from": p.bias_period_from.isoformat(),
        "weather.bias_to": p.bias_period_to.isoformat(),
    }


def run_transform(settings: Settings, conn: Any, directory: Path = TRANSFORM_DIR) -> TransformResult:
    """Выполняет все шаги. Подключение должно быть в режиме autocommit."""
    steps = discover_steps(directory)
    run_id = conn.execute(
        "INSERT INTO meta.transform_run DEFAULT VALUES RETURNING transform_run_id"
    ).fetchone()[0]
    log.info("Преобразование №%d: %d шагов", run_id, len(steps))

    results: list[StepResult] = []
    current: Step | None = None
    started = time.perf_counter()
    try:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (TRANSFORM_LOCK_KEY,))
            for name, value in session_params(settings, run_id).items():
                conn.execute("SELECT set_config(%s, %s, true)", (name, value))
            _upsert_station(conn, settings)
            for step in steps:
                current, started = step, time.perf_counter()
                conn.execute(step.sql)
                rows = conn.execute(f"SELECT count(*) FROM {step.target}").fetchone()[0]
                results.append(StepResult(step.name, step.target, "success", rows, _ms(started)))
                log.info("%s → %s: %d строк", step.name, step.target, rows)
    except Exception as exc:
        if current is not None:
            results.append(StepResult(current.name, current.target, "failed", None, _ms(started)))
        _finish(conn, run_id, "failed", results, f"{current.name if current else 'подготовка'}: {exc}")
        raise TransformError(f"шаг {current.name if current else 'подготовка'}: {exc}") from exc

    _finish(conn, run_id, "success", results, None)
    log.info("Преобразование №%d завершено", run_id)
    return TransformResult(run_id, "success", results)


def _upsert_station(conn: Any, settings: Settings) -> None:
    s = settings.station
    conn.execute(
        "INSERT INTO core.dim_station (station_id, wmo_id, name, latitude, longitude, elevation_m) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (station_id) DO UPDATE SET wmo_id = EXCLUDED.wmo_id, name = EXCLUDED.name, "
        "latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude, "
        "elevation_m = EXCLUDED.elevation_m, updated_at = now()",
        (s.wmo_id, s.wmo_id, s.name, s.latitude, s.longitude, s.elevation_m),
    )


def _finish(conn: Any, run_id: int, status: str, results: list[StepResult], error: str | None) -> None:
    for r in results:
        conn.execute(
            "INSERT INTO meta.transform_step "
            "(transform_run_id, step, target_table, status, rows_after, duration_ms) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (run_id, r.step, r.target, r.status, r.rows_after, r.duration_ms),
        )
    conn.execute(
        "UPDATE meta.transform_run SET finished_at = now(), status = %s, error_message = %s "
        "WHERE transform_run_id = %s",
        (status, error, run_id),
    )


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
