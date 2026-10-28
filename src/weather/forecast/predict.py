"""Прогноз на 7 суток (команда forecast).

Модели обучаются на всей истории, затем по признакам последнего известного дня t
строится прогноз на дни t+1 ... t+7. Дата выпуска прогноза — t+1.
Параметры моделей берутся из последней успешной оценки качества (команда evaluate),
а если её ещё не было — значения по умолчанию из models.py.

Прогнозы пишутся в core.model_forecast. Повторный запуск с теми же данными ничего
не добавляет: выпущенный прогноз не меняется.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from weather.config import Settings
from weather.forecast.features import climate_norm, horizon_data, load_history, make_features
from weather.forecast.journal import ForecastError, finish_run, last_date, start_run
from weather.forecast.models import DEFAULT_PARAMS, make_model

log = logging.getLogger(__name__)

INSERT_SQL = """
    INSERT INTO core.model_forecast (model_code, issue_date, target_date, lead_days, temp_mean, model_run_id)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (model_code, issue_date, target_date) DO NOTHING
"""


@dataclass(frozen=True)
class ForecastRow:
    model_code: str
    issue_date: date
    target_date: date
    lead_days: int
    temp_mean: float


@dataclass
class ForecastResult:
    model_run_id: int
    issue_date: date
    rows: list[ForecastRow]
    inserted: int
    params_source: str  # откуда взяты параметры моделей


def latest_params(conn: Any) -> tuple[dict[str, dict[str, Any]], str]:
    """Параметры из последней успешной оценки качества или значения по умолчанию."""
    row = conn.execute(
        "SELECT model_run_id, params -> 'models' FROM meta.model_run "
        "WHERE kind = 'evaluate' AND status = 'success' ORDER BY model_run_id DESC LIMIT 1"
    ).fetchone()
    if row is None or not row[1]:
        return DEFAULT_PARAMS, "по умолчанию (оценка качества ещё не запускалась)"
    return {**DEFAULT_PARAMS, **row[1]}, f"из оценки качества №{row[0]}"


def forecast_history(
    history: pd.DataFrame, codes: tuple[str, ...], params: dict[str, dict[str, Any]], horizon_days: int
) -> list[ForecastRow]:
    """Обучает модели на всей истории и строит прогноз от последнего известного дня."""
    t = pd.Timestamp(last_date(history))
    history = history[history.index <= t]
    norm = climate_norm(history["temp_mean"])
    features = make_features(history, norm)
    rows = []
    for h in range(1, horizon_days + 1):
        x, y, _ = horizon_data(history, features, norm, h)
        known = y.notna()
        for code in codes:
            model = make_model(code, params.get(code, DEFAULT_PARAMS[code]), h).fit(x[known], y[known])
            value = float(model.predict(x.loc[[t]])[0])
            if math.isfinite(value):
                target = (t + timedelta(days=h)).date()
                rows.append(ForecastRow(code, (t + timedelta(days=1)).date(), target, h - 1, round(value, 1)))
    return rows


def run_forecast(settings: Settings, conn: Any) -> ForecastResult:
    history = load_history(conn, settings.station.wmo_id)
    horizon = settings.forecast_horizon_days
    if history.empty or history["temp_mean"].count() < 2 * horizon:
        raise ForecastError("мало фактических данных для прогноза — сначала выполните: python -m weather run")
    params, source = latest_params(conn)
    until = last_date(history)
    issue = until + timedelta(days=1)
    run_id = start_run(conn, "forecast")
    log.info("Прогноз №%d по данным до %s, параметры %s", run_id, until, source)

    # модели, у которых прогноза с этой датой выпуска ещё нет
    done = {r.model_code for r in stored_forecast(conn, issue)}
    missing = tuple(code for code in settings.models.codes if code not in done)
    try:
        rows = forecast_history(history, missing, params, horizon) if missing else []
    except Exception as exc:
        finish_run(conn, run_id, "failed", data_until=until, error=str(exc))
        raise ForecastError(f"прогноз: {exc}") from exc

    inserted = 0
    for r in rows:
        cur = conn.execute(
            INSERT_SQL, (r.model_code, r.issue_date, r.target_date, r.lead_days, r.temp_mean, run_id)
        )
        inserted += cur.rowcount
    used = {code: params.get(code, DEFAULT_PARAMS[code]) for code in missing}
    finish_run(conn, run_id, "success", data_until=until, params={"models": used, "source": source})
    if not missing:
        log.info("Прогноз с датой выпуска %s уже есть — новых строк нет", issue)
    return ForecastResult(run_id, issue, stored_forecast(conn, issue), inserted, source)


def stored_forecast(conn: Any, issue: date) -> list[ForecastRow]:
    """Прогнозы моделей с этой датой выпуска, уже записанные в базу."""
    rows = conn.execute(
        "SELECT model_code, issue_date, target_date, lead_days, temp_mean FROM core.model_forecast "
        "WHERE issue_date = %s ORDER BY model_code, target_date",
        (issue,),
    ).fetchall()
    return [ForecastRow(code, i, t, lead, float(temp)) for code, i, t, lead, temp in rows]
