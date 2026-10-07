"""Преобразования raw -> staging -> core -> mart на настоящей базе с подменёнными источниками."""

from dataclasses import replace
from datetime import timedelta

import pytest
from fakes import FakeHttp

from weather.ingest.runner import run_ingest, today_in
from weather.transform import TransformError, discover_steps, run_transform


def setup(fresh_db, days_of_history=20):
    settings, conn = fresh_db
    today = today_in(settings.timezone)
    settings = replace(settings, history_start=today - timedelta(days=days_of_history))
    return settings, conn, today


def load(settings, conn, today, **fake):
    run_ingest(settings, conn, http=FakeHttp(settings, today, **fake), today=today, trigger="test")
    return run_transform(settings, conn)


def one(conn, sql, *params):
    return conn.execute(sql, params or None).fetchone()


# --- без базы ---------------------------------------------------------------------


def test_project_steps_have_targets():
    steps = discover_steps()
    assert [s.name[:2] for s in steps] == sorted(s.name[:2] for s in steps)
    assert {s.target.split(".")[0] for s in steps} == {"staging", "core", "mart"}


def test_step_without_target_is_rejected(tmp_path):
    (tmp_path / "10_x.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(TransformError, match="target"):
        discover_steps(tmp_path)


# --- на базе ----------------------------------------------------------------------


def test_layers_are_filled_and_rerun_adds_no_versions(fresh_db):
    settings, conn, today = setup(fresh_db)
    result = load(settings, conn, today)
    rows = {s.target: s.rows_after for s in result.steps}
    assert rows["staging.openmeteo_daily"] == 20
    assert rows["core.fact_weather_daily"] == 20
    assert rows["mart.weather_daily"] == 20
    assert rows["mart.forecast_latest"] == 8
    assert one(conn, "SELECT count(*) FROM core.dim_station")[0] == 1

    run_transform(settings, conn)
    assert one(conn, "SELECT count(*) FROM core.fact_weather_daily")[0] == 20  # новых версий нет
    statuses = conn.execute("SELECT status FROM meta.transform_run ORDER BY transform_run_id").fetchall()
    assert statuses == [("success",), ("success",)]


def test_station_first_gaps_filled_from_openmeteo_with_bias(fresh_db):
    settings, conn, today = setup(fresh_db)
    # опорный период смещения — тестовые дни, чтобы поправка была ненулевой
    processing = replace(settings.processing, bias_period_from=settings.history_start, bias_period_to=today)
    settings = replace(settings, processing=processing)
    gap, model_day = today - timedelta(days=5), today - timedelta(days=3)
    load(settings, conn, today, station_gaps=(gap,), station_model_days=(model_day,))

    temp, source = one(
        conn, "SELECT temp_mean, temp_source FROM core.fact_weather_daily WHERE obs_date = %s",
        today - timedelta(days=10),
    )  # fmt: skip
    assert (float(temp), source) == (5.0, "station")

    for day in (gap, model_day):
        temp, source, om = one(
            conn,
            "SELECT temp_mean, temp_source, temp_mean_openmeteo "
            "FROM core.fact_weather_daily WHERE obs_date = %s",
            day,
        )
        bias = one(conn, "SELECT temp_mean_bias FROM core.source_bias_monthly WHERE month = %s", day.month)[0]
        assert source == "openmeteo_adjusted"
        assert round(float(om) + float(bias), 1) == float(temp)


def test_changed_source_value_creates_new_version(fresh_db):
    settings, conn, today = setup(fresh_db)
    load(settings, conn, today)
    day = today - timedelta(days=2)

    # Meteostat выложил новую версию файла: за один день значение уточнено
    load(settings, conn, today, file_version="v2", station_temps={day: 7.0})

    versions = conn.execute(
        "SELECT temp_mean, is_current, valid_from, valid_to FROM core.fact_weather_daily "
        "WHERE obs_date = %s ORDER BY version_id",
        (day,),
    ).fetchall()
    assert len(versions) == 2
    old, new = versions
    assert float(old[0]) == 5.0 and old[1] is False and old[3] is not None
    assert float(new[0]) == 7.0 and new[1] is True and new[3] is None
    # остальные даты не изменились — у них по одной версии
    assert one(conn, "SELECT count(*) FROM core.fact_weather_daily")[0] == 21


def test_heating_streak_and_forecast_continue_actual_series(fresh_db):
    settings, conn, today = setup(fresh_db)
    d = {i: today - timedelta(days=i) for i in range(1, 9)}
    station = {d[8]: 7.0, d[7]: 9.0, d[6]: 7.0, d[5]: 7.0, d[4]: 7.0, d[3]: 7.0, d[2]: 6.0, d[1]: 7.5}
    forecast = {
        "temperature_2m_mean": {today: 7.0, today + timedelta(days=1): 10.0, today + timedelta(days=2): 7.0},
        "temperature_2m_min": {today + timedelta(days=3): -2.0},
        "temperature_2m_max": {today + timedelta(days=3): 3.0},
        "precipitation_sum": {today + timedelta(days=3): 1.5},
    }
    load(settings, conn, today, station_temps=station, station_default=10.0, forecast_overrides=forecast)

    actual = dict(
        conn.execute(
            "SELECT obs_date, cold_streak_days FROM mart.weather_daily WHERE obs_date >= %s", (d[8],)
        ).fetchall()
    )
    assert [actual[d[i]] for i in range(8, 0, -1)] == [1, 0, 1, 2, 3, 4, 5, 6]
    assert (
        one(conn, "SELECT heating_condition_met FROM mart.weather_daily WHERE obs_date = %s", d[2])[0] is True
    )
    assert (
        one(conn, "SELECT heating_condition_met FROM mart.weather_daily WHERE obs_date = %s", d[3])[0]
        is False
    )

    rows = conn.execute(
        "SELECT target_date, cold_streak_days, ice_risk FROM mart.forecast_latest ORDER BY target_date"
    ).fetchall()
    streaks = {r[0]: r[1] for r in rows}
    assert streaks[today] == 7  # продолжает фактическую серию из 6 дней
    assert streaks[today + timedelta(days=1)] == 0
    assert streaks[today + timedelta(days=2)] == 1
    assert {r[0]: r[2] for r in rows}[today + timedelta(days=3)] is True  # 0 °C внутри суток и осадки


def test_failed_step_rolls_back_everything(fresh_db, tmp_path):
    settings, conn, today = setup(fresh_db)
    load(settings, conn, today)
    before = one(conn, "SELECT count(*) FROM staging.openmeteo_forecast")[0]

    (tmp_path / "10_clear.sql").write_text(
        "-- target: staging.openmeteo_forecast\nTRUNCATE staging.openmeteo_forecast;", encoding="utf-8"
    )
    (tmp_path / "20_broken.sql").write_text(
        "-- target: staging.openmeteo_forecast\nSELECT * FROM no_such_table;", encoding="utf-8"
    )
    with pytest.raises(TransformError, match="20_broken"):
        run_transform(settings, conn, directory=tmp_path)

    assert one(conn, "SELECT count(*) FROM staging.openmeteo_forecast")[0] == before  # TRUNCATE откатился
    status, error = one(
        conn, "SELECT status, error_message FROM meta.transform_run ORDER BY transform_run_id DESC LIMIT 1"
    )
    assert status == "failed" and "no_such_table" in error
    steps = conn.execute(
        "SELECT step, status FROM meta.transform_step WHERE transform_run_id = "
        "(SELECT max(transform_run_id) FROM meta.transform_run) ORDER BY step"
    ).fetchall()
    assert steps == [("10_clear", "success"), ("20_broken", "failed")]
