"""Обработка данных между слоями: raw -> staging -> core -> mart.

Сами преобразования — проект dbt в каталоге dbt/:
* models/ — SQL-модели слоёв staging, core и mart;
* snapshots/ — история изменений (SCD2) справочника станций и погоды по дням;
* seeds/ — справочник кодов погоды (CSV);
* tests/ и описания моделей (*.yml) — проверки качества данных.

Этот модуль запускает `dbt build` из Python и ведёт журнал в базе:
* meta.transform_run — запуск обработки;
* meta.transform_step — каждый шаг (модель, справочник, снапшот) и сколько строк в таблице после него;
* meta.dq_result — результат каждой проверки (dq.py).

Параметры из config/settings.yaml (станция, пороги, периоды) передаются в dbt как vars.
dbt выполняет шаги в порядке зависимостей. Если шаг упал или не прошла критичная
проверка (severity: error), зависящие от них шаги не выполняются — витрины остаются
такими, какими были до запуска.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from weather import dq
from weather.config import PROJECT_ROOT, DatabaseSettings, Settings

log = logging.getLogger(__name__)

DBT_DIR = PROJECT_ROOT / "dbt"
TRANSFORM_LOCK_KEY = 27612_0003
STEP_TYPES = ("seed", "model", "snapshot")
STEP_STATUSES = {"success": "success", "error": "failed", "skipped": "skipped"}


class TransformError(RuntimeError):
    """Обработка завершилась с ошибкой: упал шаг или не прошла критичная проверка."""

    def __init__(
        self,
        message: str,
        checks: list[dq.CheckResult] | None = None,
        steps: list[StepResult] | None = None,
    ) -> None:
        super().__init__(message)
        self.checks = checks or []
        self.steps = steps or []


@dataclass(frozen=True)
class StepResult:
    step: str
    node_type: str  # seed | model | snapshot
    target: str
    status: str  # success | failed | skipped
    rows_after: int | None
    duration_ms: int


@dataclass(frozen=True)
class TransformResult:
    transform_run_id: int
    status: str
    steps: list[StepResult]
    checks: list[dq.CheckResult] = field(default_factory=list)


@dataclass
class DbtRun:
    """Итог одной команды dbt."""

    success: bool
    results: list[Any]  # результаты узлов (модели, снапшоты, тесты)
    manifest: Any
    error: str | None = None


# --- запуск dbt -------------------------------------------------------------------


def dbt_vars(settings: Settings, transform_run_id: int | None = None) -> dict[str, Any]:
    """Параметры из настроек для моделей dbt (в SQL — {{ var('имя') }})."""
    s, p = settings.station, settings.processing
    return {
        "station_id": s.wmo_id,
        "station_name": s.name,
        "station_latitude": s.latitude,
        "station_longitude": s.longitude,
        "station_elevation_m": s.elevation_m,
        "timezone": settings.timezone,
        "history_start": settings.history_start.isoformat(),
        "heating_threshold_c": settings.heating.threshold_c,
        "heating_days": settings.heating.consecutive_days,
        "ice_threshold_c": settings.ice_threshold_c,
        "norm_from_year": p.climate_norm_from_year,
        "norm_to_year": p.climate_norm_to_year,
        "bias_from": p.bias_period_from.isoformat(),
        "bias_to": p.bias_period_to.isoformat(),
        "transform_run_id": transform_run_id or 0,
    }


def run_dbt(
    settings: Settings,
    command: list[str],
    *,
    transform_run_id: int | None = None,
    project_dir: Path = DBT_DIR,
) -> DbtRun:
    """Выполняет команду dbt (build, test, docs ...) в этом же процессе.

    База — та же, что у приложения: параметры подключения передаются в dbt/profiles.yml
    через переменные окружения POSTGRES_*."""
    from dbt.cli.main import dbtRunner

    common = [
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(project_dir),
        "--vars",
        json.dumps(dbt_vars(settings, transform_run_id), ensure_ascii=False),
    ]
    with _db_env(settings.db):
        parsed = dbtRunner().invoke(["parse", "--quiet", *common])
        if not parsed.success or parsed.result is None:
            return DbtRun(False, [], None, f"ошибка в проекте dbt: {parsed.exception or 'см. вывод dbt'}")
        manifest = parsed.result
        res = dbtRunner(manifest=manifest).invoke([*command, *common])
    results = list(getattr(res.result, "results", None) or [])
    error = None
    if not res.success:
        error = "; ".join(_node_errors(results)) or str(res.exception or "dbt завершился с ошибкой")
    return DbtRun(res.success, results, manifest, error)


def load_manifest(settings: Settings, project_dir: Path = DBT_DIR) -> Any:
    """Разбирает проект dbt без подключения к базе (для проверки описаний и тестов)."""
    from dbt.cli.main import dbtRunner

    with _db_env(settings.db):
        parsed = dbtRunner().invoke(
            ["parse", "--quiet", "--project-dir", str(project_dir), "--profiles-dir", str(project_dir),
             "--vars", json.dumps(dbt_vars(settings), ensure_ascii=False)]
        )  # fmt: skip
    if not parsed.success:
        raise TransformError(f"ошибка в проекте dbt: {parsed.exception}")
    return parsed.result


@contextmanager
def _db_env(db: DatabaseSettings) -> Iterator[None]:
    """На время запуска dbt — параметры подключения из настроек приложения."""
    values = {
        "POSTGRES_HOST": db.host,
        "POSTGRES_PORT": str(db.port),
        "POSTGRES_DB": db.name,
        "POSTGRES_USER": db.user,
        "POSTGRES_PASSWORD": db.password,
    }
    saved = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _node_errors(results: list[Any]) -> list[str]:
    errors = []
    for r in results:
        if _status(r) == "error" or (r.node.resource_type == "test" and _status(r) == "fail"):
            text = " ".join(str(r.message or "").split())
            errors.append(f"{r.node.name}: {text[:300]}")
    return errors


def _status(run_result: Any) -> str:
    return str(getattr(run_result.status, "value", run_result.status))


# --- обработка --------------------------------------------------------------------


def run_transform(settings: Settings, conn: Any, *, project_dir: Path = DBT_DIR) -> TransformResult:
    """Запускает `dbt build` и записывает журнал. Подключение должно быть в режиме autocommit."""
    run_id = conn.execute(
        "INSERT INTO meta.transform_run DEFAULT VALUES RETURNING transform_run_id"
    ).fetchone()[0]
    log.info("Преобразование №%d: dbt build (%s)", run_id, project_dir)

    # Одновременно может идти только одна обработка
    conn.execute("SELECT pg_advisory_lock(%s)", (TRANSFORM_LOCK_KEY,))
    try:
        run = run_dbt(settings, ["build"], transform_run_id=run_id, project_dir=project_dir)
        steps = [_step_result(conn, r) for r in run.results if r.node.resource_type in STEP_TYPES]
        tests = [r for r in run.results if r.node.resource_type == "test"]
        checks = [dq.check_result(conn, r, run.manifest) for r in tests]
    except Exception as exc:
        _finish(conn, run_id, "failed", [], [], f"запуск dbt: {exc}")
        raise TransformError(f"запуск dbt: {exc}") from exc
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (TRANSFORM_LOCK_KEY,))

    for c in checks:
        if c.status == "failed" and c.rule.severity != "critical":
            note = f" ({c.message})" if c.message else ""
            log.warning("Проверка %s: нарушений %d%s", c.rule.code, c.failed_rows, note)

    status = "success" if run.success else "failed"
    _finish(conn, run_id, status, steps, checks, run.error)
    if not run.success:
        raise TransformError(run.error or "dbt завершился с ошибкой", checks, steps)
    log.info("Преобразование №%d завершено; %s", run_id, dq.summary(checks))
    return TransformResult(run_id, status, steps, checks)


def run_checks(settings: Settings, conn: Any, *, select: str | None = None) -> list[dq.CheckResult]:
    """Только проверки (`dbt test`) по текущим данным: ничего не меняет.

    select — выбрать проверки по правилам выбора dbt (например, имя проверки)."""
    command = ["test"] + (["--select", select] if select else [])
    run = run_dbt(settings, command)
    if run.manifest is None:
        raise TransformError(run.error or "dbt завершился с ошибкой")
    return [dq.check_result(conn, r, run.manifest) for r in run.results if r.node.resource_type == "test"]


def _step_result(conn: Any, run_result: Any) -> StepResult:
    node = run_result.node
    target = f"{node.schema}.{node.alias}"
    status = STEP_STATUSES.get(_status(run_result), "failed")
    rows = None
    if status == "success":
        rows = conn.execute(f'SELECT count(*) FROM "{node.schema}"."{node.alias}"').fetchone()[0]
    duration = round(float(run_result.execution_time or 0) * 1000)
    node_type = str(getattr(node.resource_type, "value", node.resource_type))
    return StepResult(node.name, node_type, target, status, rows, duration)


def _finish(
    conn: Any,
    run_id: int,
    status: str,
    steps: list[StepResult],
    checks: list[dq.CheckResult],
    error: str | None,
) -> None:
    dq.save_results(conn, checks, run_id)
    for s in steps:
        conn.execute(
            "INSERT INTO meta.transform_step "
            "(transform_run_id, step, node_type, target_table, status, rows_after, duration_ms) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (run_id, s.step, s.node_type, s.target, s.status, s.rows_after, s.duration_ms),
        )
    conn.execute(
        "UPDATE meta.transform_run SET finished_at = now(), status = %s, error_message = %s "
        "WHERE transform_run_id = %s",
        (status, error, run_id),
    )
