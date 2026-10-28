"""Прогнозное ядро: признаки без утечек, проверка качества по времени, прогноз в базе."""

import json
from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("lightgbm", reason="lightgbm не установлен")

from fakes import FakeHttp  # noqa: E402

from weather.cli import FORECAST_MARTS  # noqa: E402
from weather.forecast.evaluate import evaluate_history, run_evaluate  # noqa: E402
from weather.forecast.features import HISTORY_COLUMNS, climate_norm, horizon_data, make_features  # noqa: E402
from weather.forecast.models import MODEL_NAMES  # noqa: E402
from weather.forecast.predict import forecast_history, run_forecast  # noqa: E402
from weather.ingest.runner import run_ingest, today_in  # noqa: E402
from weather.transform import run_transform  # noqa: E402

CODES = tuple(MODEL_NAMES)


def synthetic_history(start="2010-01-01", years=8, seed=1):
    """Ряд, похожий на погоду: годовой ход + «погода», которая держится несколько дней."""
    rng = np.random.default_rng(seed)
    index = pd.date_range(start, periods=int(365.25 * years), freq="D")
    season = 6 - 13 * np.cos(2 * np.pi * (index.day_of_year.to_numpy() - 15) / 365.25)
    weather = np.zeros(len(index))
    for i in range(1, len(index)):
        weather[i] = 0.8 * weather[i - 1] + rng.normal(0, 2.0)
    df = pd.DataFrame(index=index, columns=list(HISTORY_COLUMNS), dtype=float)
    df["temp_mean"] = np.round(season + weather, 1)
    df["temp_min"] = df["temp_mean"] - 4
    df["temp_max"] = df["temp_mean"] + 4
    df["pressure_hpa"] = 1013 - 2 * weather + rng.normal(0, 3, len(index))
    df["humidity_pct"] = 75.0
    return df


# --- без базы ---------------------------------------------------------------------


def test_features_do_not_look_into_future():
    history = synthetic_history(years=1)
    norm = climate_norm(history["temp_mean"])
    t = history.index[200]
    before = make_features(history, norm).loc[t]

    changed = history.copy()
    changed.loc[changed.index > t, "temp_mean"] += 30  # «будущее» резко поменялось
    after = make_features(changed, norm).loc[t]
    pd.testing.assert_series_equal(before, after)


def test_target_is_h_days_ahead():
    history = synthetic_history(years=1)
    norm = climate_norm(history["temp_mean"])
    x, y, target = horizon_data(history, make_features(history, norm), norm, 3)
    t = history.index[100]
    assert target[t] == t + timedelta(days=3)
    assert y[t] == history.loc[t + timedelta(days=3), "temp_mean"]
    assert x.loc[t, "temp_0"] == history.loc[t, "temp_mean"]
    assert x.loc[t, "temp_1"] == history.loc[t - timedelta(days=1), "temp_mean"]


def test_evaluation_beats_baselines_and_has_no_leakage():
    history = synthetic_history()
    args = (CODES, date(2015, 1, 1), date(2016, 1, 1), 8.0, 7)
    metrics, params, importance = evaluate_history(history, *args)

    assert len(metrics) == len(CODES) * 2 * 7
    test = {(m.model_code, m.horizon): m.mae for m in metrics if m.split == "test"}
    for code in ("ridge", "lightgbm"):
        assert test[(code, 1)] < test[("climatology", 1)]
        assert np.mean([test[(code, h)] for h in range(1, 8)]) < np.mean(
            [test[("persistence", h)] for h in range(1, 8)]
        )
    assert set(params["lightgbm"]["n_estimators"]) == {str(h) for h in range(1, 8)}
    assert set(importance) == {"ridge", "lightgbm"}

    # если поменять данные тестового периода, результаты подбора параметров не меняются:
    # значит, на проверочном периоде модели не видели ничего из теста
    shuffled = history.copy()
    after_test = shuffled.index >= pd.Timestamp("2016-01-01")
    shuffled.loc[after_test, "temp_mean"] = shuffled.loc[after_test, "temp_mean"].to_numpy()[::-1]
    metrics2, params2, _ = evaluate_history(shuffled, *args)
    assert [m for m in metrics if m.split == "validation"] == [m for m in metrics2 if m.split == "validation"]
    assert params == params2


def test_forecast_starts_after_last_known_day():
    history = synthetic_history(years=2)
    last = history.index[-1].date()
    rows = forecast_history(history, CODES, {}, 7)
    assert {r.model_code for r in rows} == set(CODES)
    assert {r.issue_date for r in rows} == {last + timedelta(days=1)}
    for code in CODES:
        targets = sorted(r.target_date for r in rows if r.model_code == code)
        assert targets == [last + timedelta(days=h) for h in range(1, 8)]
    persistence = {r.temp_mean for r in rows if r.model_code == "persistence"}
    assert persistence == {round(history["temp_mean"].iloc[-1], 1)}


# --- на базе ----------------------------------------------------------------------


def test_forecast_is_saved_and_rerun_adds_nothing(fresh_db):
    settings, conn = fresh_db
    today = today_in(settings.timezone)
    settings = replace(settings, history_start=today - timedelta(days=40))
    run_ingest(settings, conn, http=FakeHttp(settings, today), today=today, trigger="test")
    run_transform(settings, conn)

    result = run_forecast(settings, conn)
    assert result.inserted == len(CODES) * 7
    assert "по умолчанию" in result.params_source  # оценка качества ещё не запускалась
    run_transform(settings, conn, select=FORECAST_MARTS)
    counts = dict(
        conn.execute("SELECT model_code, count(*) FROM mart.forecast_latest GROUP BY model_code").fetchall()
    )
    assert counts == {"openmeteo": 8, **{code: 7 for code in CODES}}
    ridge = conn.execute("SELECT count(*) FROM core.fact_forecast WHERE model_code = 'ridge'").fetchone()[0]
    assert ridge == 7

    again = run_forecast(settings, conn)
    assert again.inserted == 0 and len(again.rows) == len(CODES) * 7
    runs = conn.execute("SELECT kind, status FROM meta.model_run ORDER BY model_run_id").fetchall()
    assert runs == [("forecast", "success"), ("forecast", "success")]


def test_evaluate_saves_metrics_and_forecast_uses_chosen_params(fresh_db):
    settings, conn = fresh_db
    history = synthetic_history()
    # для проверки достаточно таблицы с нужными колонками — без загрузки и dbt
    conn.execute(
        "CREATE TABLE core.fact_weather_daily (station_id text, obs_date date, valid_to timestamptz, "
        + ", ".join(f"{c} numeric" for c in HISTORY_COLUMNS)
        + ")"
    )
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO core.fact_weather_daily (station_id, obs_date, {', '.join(HISTORY_COLUMNS)}) "
            f"VALUES (%s, %s, {', '.join(['%s'] * len(HISTORY_COLUMNS))})",
            [
                (settings.station.wmo_id, d.date(), *[None if pd.isna(v) else float(v) for v in row])
                for d, row in history.iterrows()
            ],
        )
    models = replace(settings.models, validation_from=date(2015, 1, 1), test_from=date(2016, 1, 1))
    settings = replace(settings, models=models)

    result = run_evaluate(settings, conn)
    saved = conn.execute(
        "SELECT split, count(*) FROM meta.model_metric WHERE model_run_id = %s GROUP BY split ORDER BY split",
        (result.model_run_id,),
    ).fetchall()
    assert saved == [("test", len(CODES) * 7), ("validation", len(CODES) * 7)]
    params = conn.execute("SELECT params FROM meta.model_run WHERE model_run_id = %s", (result.model_run_id,))
    stored = params.fetchone()[0]
    stored = json.loads(stored) if isinstance(stored, str) else stored
    assert stored["test_from"] == "2016-01-01"
    assert stored["models"]["ridge"] == result.params["ridge"]

    forecast = run_forecast(settings, conn)
    assert forecast.params_source == f"из оценки качества №{result.model_run_id}"
