"""Журнал запусков моделей: meta.model_run."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pandas as pd


class ForecastError(RuntimeError):
    """Оценка качества или прогноз не выполнены."""


def start_run(conn: Any, kind: str) -> int:
    return int(
        conn.execute(
            "INSERT INTO meta.model_run (kind) VALUES (%s) RETURNING model_run_id", (kind,)
        ).fetchone()[0]
    )


def finish_run(
    conn: Any,
    run_id: int,
    status: str,
    *,
    data_until: date | None = None,
    params: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE meta.model_run SET finished_at = now(), status = %s, data_until = %s, "
        "params = %s::jsonb, error_message = %s WHERE model_run_id = %s",
        (status, data_until, json.dumps(params or {}, ensure_ascii=False), error, run_id),
    )


def last_date(history: pd.DataFrame) -> date:
    """Последний день, за который известна среднесуточная температура."""
    return history["temp_mean"].last_valid_index().date()
