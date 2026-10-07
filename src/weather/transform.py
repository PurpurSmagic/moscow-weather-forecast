"""Преобразования данных между слоями: raw -> staging -> core -> mart.

Шаги — SQL-файлы в sql/transform/. Они выполняются по порядку имён в одной
транзакции: если какой-то шаг упал, база остаётся как до запуска. Первая строка
файла «-- target: схема.таблица» говорит, какую таблицу шаг заполняет, — после
шага в журнал пишется, сколько в ней строк.

Параметры из настроек (станция, пороги, периоды) передаются в SQL через
set_config, в SQL они читаются как current_setting('weather.<имя>').

Внутри той же транзакции выполняются проверки качества данных (dq.py):
после шагов staging — с переносом плохих строк в карантин, в конце — итоговые.
Если не прошла критичная проверка, всё откатывается.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from weather import dq
from weather.config import PROJECT_ROOT, Settings, today_in

log = logging.getLogger(__name__)

TRANSFORM_DIR = PROJECT_ROOT / "sql" / "transform"
TARGET_RE = re.compile(r"^--\s*target:\s*([a-z_]+\.[a-z_]+)\s*$", re.MULTILINE)
TRANSFORM_LOCK_KEY = 27612_0003


class TransformError(RuntimeError):
    """Шаг преобразования или критичная проверка завершились ошибкой; изменения откатены."""

    def __init__(self, message: str, checks: list[dq.CheckResult] | None = None) -> None:
        super().__init__(message)
        self.checks = checks or []


class QualityError(RuntimeError):
    """Не прошли критичные проверки качества."""


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
    checks: list[dq.CheckResult] = field(default_factory=list)


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


def session_params(settings: Settings, transform_run_id: int | None, today: date) -> dict[str, str]:
    p = settings.processing
    return {
        "weather.transform_run_id": str(transform_run_id or 0),
        "weather.today": today.isoformat(),
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


def run_transform(
    settings: Settings,
    conn: Any,
    directory: Path = TRANSFORM_DIR,
    *,
    rules: list[dq.Rule] | None = None,
    today: date | None = None,
) -> TransformResult:
    """Выполняет все шаги и проверки качества. Подключение должно быть в режиме autocommit."""
    steps = discover_steps(directory)
    rules = dq.load_rules() if rules is None else rules
    today = today or today_in(settings.timezone)
    run_id = conn.execute(
        "INSERT INTO meta.transform_run DEFAULT VALUES RETURNING transform_run_id"
    ).fetchone()[0]
    log.info("Преобразование №%d: %d шагов, %d правил проверки", run_id, len(steps), len(rules))

    results: list[StepResult] = []
    checks: list[dq.CheckResult] = []
    where, current, started = "подготовка", None, time.perf_counter()
    try:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (TRANSFORM_LOCK_KEY,))
            set_params(conn, session_params(settings, run_id, today))
            station_key = upsert_station(conn, settings)
            conn.execute("SELECT set_config('weather.station_key', %s, true)", (str(station_key),))
            conn.execute("TRUNCATE staging.quarantine")

            staging_checked = False
            for step in steps:
                if not staging_checked and not step.target.startswith("staging."):
                    where, current = "проверки staging", None
                    _check(conn, rules, "staging", run_id, checks)
                    staging_checked = True
                where, current, started = step.name, step, time.perf_counter()
                conn.execute(step.sql)
                rows = conn.execute(f"SELECT count(*) FROM {step.target}").fetchone()[0]
                results.append(StepResult(step.name, step.target, "success", rows, _ms(started)))
                log.info("%s → %s: %d строк", step.name, step.target, rows)
            current = None
            if not staging_checked:
                where = "проверки staging"
                _check(conn, rules, "staging", run_id, checks)
            where = "итоговые проверки"
            _check(conn, rules, "final", run_id, checks)
    except Exception as exc:
        if current is not None:
            results.append(StepResult(current.name, current.target, "failed", None, _ms(started)))
        _finish(conn, run_id, "failed", results, checks, f"{where}: {exc}")
        raise TransformError(f"{where}: {exc}", checks) from exc

    _finish(conn, run_id, "success", results, checks, None)
    log.info("Преобразование №%d завершено; %s", run_id, dq.summary(checks))
    return TransformResult(run_id, "success", results, checks)


def set_params(conn: Any, params: dict[str, str]) -> None:
    """Параметры для SQL на время текущей транзакции."""
    for name, value in params.items():
        conn.execute("SELECT set_config(%s, %s, true)", (name, value))


def _check(conn: Any, rules: list[dq.Rule], phase: str, run_id: int, checks: list[dq.CheckResult]) -> None:
    """Проверки одного этапа. Результаты добавляются в checks (их сохраним, даже если всё откатится)."""
    phase_checks = dq.run_checks(conn, rules, phase, quarantine_run_id=run_id)
    checks.extend(phase_checks)
    for c in phase_checks:
        if c.status == "failed" and c.rule.severity != "critical":
            note = f" ({c.message})" if c.message else ""
            log.warning("Проверка %s: нарушений %d%s", c.rule.code, c.failed_rows, note)
    bad = dq.blocking(phase_checks)
    if bad:
        raise QualityError("не прошли критичные проверки: " + ", ".join(c.rule.code for c in bad))


def upsert_station(conn: Any, settings: Settings) -> int:
    """Справочник станций с историей (SCD2). Возвращает номер текущей версии станции.

    Если описание станции в настройках не менялось — ничего не делаем. Если поменялось —
    закрываем текущую версию и добавляем новую.
    """
    s = settings.station
    new = (s.wmo_id, s.name, round(s.latitude, 4), round(s.longitude, 4), round(s.elevation_m, 1))
    row = conn.execute(
        "SELECT station_key, wmo_id, name, latitude, longitude, elevation_m "
        "FROM core.dim_station WHERE station_id = %s AND is_current",
        (s.wmo_id,),
    ).fetchone()
    if row is not None:
        key, wmo_id, name, lat, lon, elev = row
        old = (str(wmo_id), name, round(float(lat), 4), round(float(lon), 4), round(float(elev or 0), 1))
        if old == new:
            return int(key)
        conn.execute(
            "UPDATE core.dim_station SET valid_to = now(), is_current = false WHERE station_key = %s", (key,)
        )
        log.info("Станция %s: описание изменилось, добавлена новая версия", s.wmo_id)
    row = conn.execute(
        "INSERT INTO core.dim_station (station_id, wmo_id, name, latitude, longitude, elevation_m) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING station_key",
        (s.wmo_id, *new),
    ).fetchone()
    return int(row[0])


def _finish(
    conn: Any,
    run_id: int,
    status: str,
    results: list[StepResult],
    checks: list[dq.CheckResult],
    error: str | None,
) -> None:
    dq.save_results(conn, checks, run_id)
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
