"""Проверки качества данных (тесты dbt), история станции и реестр схем источников."""

import json
import shutil
from dataclasses import replace
from datetime import timedelta

import pytest
from fakes import FakeHttp

from weather import dq
from weather.config import load_settings
from weather.ingest.runner import run_ingest, today_in
from weather.ingest.schema_registry import describe_change, openmeteo_fields
from weather.transform import DBT_DIR, TransformError, load_manifest, run_checks, run_transform

# Правила прежней версии (config/dq_rules.yaml) — все они должны остаться проверками dbt
OLD_RULES = {
    "openmeteo_not_empty", "openmeteo_temp_range", "openmeteo_temp_order", "meteostat_temp_range",
    "meteostat_temp_order", "openmeteo_values_allowed", "meteostat_values_allowed", "weather_code_known",
    "raw_openmeteo_types", "raw_meteostat_types", "fact_dates_complete", "fact_temp_present",
    "fact_one_current_version", "station_one_current_version", "forecast_one_issue", "fact_station_ref",
    "fact_raw_ref", "mart_matches_core", "fresh_actual", "fresh_forecast", "fresh_loads", "sources_agree",
    "temp_jump", "source_schema_changed",
}  # fmt: skip


def setup(fresh_db, days_of_history=20):
    settings, conn = fresh_db
    today = today_in(settings.timezone)
    settings = replace(settings, history_start=today - timedelta(days=days_of_history))
    return settings, conn, today


def ingest(settings, conn, today, **fake):
    return run_ingest(settings, conn, http=FakeHttp(settings, today, **fake), today=today, trigger="test")


def one(conn, sql, *params):
    return conn.execute(sql, params or None).fetchone()


def as_json(value):
    return json.loads(value) if isinstance(value, str) else value


def project_copy(tmp_path, tests):
    """Копия проекта dbt с дополнительными проверками {имя: SQL}."""
    project = tmp_path / "dbt"
    shutil.copytree(DBT_DIR, project, ignore=shutil.ignore_patterns("target", "logs"))
    for name, sql in tests.items():
        (project / "tests" / "staging" / f"{name}.sql").write_text(sql, encoding="utf-8")
    return project


# --- без базы ---------------------------------------------------------------------


def test_project_checks_are_described_and_cover_all_check_types():
    pytest.importorskip("dbt", reason="dbt не установлен")
    rules = dq.project_rules(load_manifest(load_settings()))
    codes = [r.code for r in rules]
    assert len(codes) == len(set(codes))
    assert OLD_RULES <= set(codes)
    for r in rules:
        assert r.check_type in dq.CHECK_TYPES, r.code
        assert r.severity in dq.SEVERITIES, r.code
        assert r.phase in dq.PHASES, r.code
        assert r.description, f"{r.code}: нет описания"
        assert "." in r.table, r.code
    assert {r.check_type for r in rules} == set(dq.CHECK_TYPES)
    quarantine = {r.code for r in rules if r.severity == "quarantine"}
    assert quarantine == {"openmeteo_temp_range", "openmeteo_temp_order", "meteostat_temp_range",
                          "meteostat_temp_order"}  # fmt: skip
    assert {r.phase for r in rules if r.code.startswith("raw_")} == {"staging"}
    assert next(r for r in rules if r.code == "fact_temp_present").severity == "critical"


def test_schema_change_description():
    old = {"temperature_2m_mean": "°C", "pressure_msl_mean": "hPa"}
    new = {"temperature_2m_mean": "°F", "cloud_cover_mean": "%"}
    text = describe_change(old, new)
    assert "добавлены cloud_cover_mean" in text
    assert "удалены pressure_msl_mean" in text
    assert "temperature_2m_mean (°C → °F)" in text
    assert describe_change(["a", "b"], ["b", "a"]).endswith("изменился порядок полей")
    assert openmeteo_fields({"reason": "ошибка"}) == {}


# --- на базе ----------------------------------------------------------------------


def test_clean_data_passes_all_checks(fresh_db):
    settings, conn, today = setup(fresh_db)
    ingest(settings, conn, today)
    result = run_transform(settings, conn)

    not_passed = {c.rule.code: c.status for c in result.checks if c.status != "passed"}
    assert not_passed == {}
    assert OLD_RULES <= {c.rule.code for c in result.checks}
    sql = "SELECT count(*) FROM meta.dq_result WHERE transform_run_id = %s"
    assert one(conn, sql, result.transform_run_id)[0] == len(result.checks)
    assert one(conn, "SELECT count(*) FROM staging.quarantine")[0] == 0


def test_bad_station_row_goes_to_quarantine(fresh_db):
    settings, conn, today = setup(fresh_db)
    bad_day = today - timedelta(days=5)
    ingest(settings, conn, today, station_temps={bad_day: 99.0})
    result = run_transform(settings, conn)

    check = next(c for c in result.checks if c.rule.code == "meteostat_temp_range")
    assert check.status == "failed" and check.quarantined == 1
    assert check.sample[0]["obs_date"] == bad_day.isoformat()
    row_key, rule_code, data, run_id = one(
        conn, "SELECT row_key, rule_code, row_data, transform_run_id FROM staging.quarantine"
    )
    assert (row_key, rule_code) == (bad_day.isoformat(), "meteostat_temp_range")
    assert run_id == result.transform_run_id
    assert float(as_json(data)["temp_mean"]) == 99.0
    # плохого значения нет ни в staging, ни в core: день взят из Open-Meteo с поправкой
    assert one(conn, "SELECT count(*) FROM staging.meteostat_daily WHERE obs_date = %s", bad_day)[0] == 0
    source, temp = one(
        conn, "SELECT temp_source, temp_mean FROM mart.weather_daily WHERE obs_date = %s", bad_day
    )
    assert source == "openmeteo_adjusted" and temp < 50
    assert result.status == "success"


def test_critical_failure_stops_processing_and_keeps_results(fresh_db, tmp_path):
    settings, conn, today = setup(fresh_db)
    day = today - timedelta(days=3)
    ingest(settings, conn, today)
    run_transform(settings, conn)
    before = one(conn, "SELECT temp_mean FROM mart.weather_daily WHERE obs_date = %s", day)[0]

    # станция уточнила значение, но обработка не проходит критичную проверку
    conn.execute("UPDATE raw.meteostat_file SET fetched_at = fetched_at - interval '1 day'")
    ingest(settings, conn, today, file_version="v2", station_temps={day: -3.0})
    always_fails = "{{ config(severity='error') }}\nselect obs_date from {{ ref('meteostat_daily') }} limit 1"
    project = project_copy(tmp_path, {"always_fails": always_fails})
    with pytest.raises(TransformError, match="always_fails") as error:
        run_transform(settings, conn, project_dir=project)

    assert any(c.rule.code == "always_fails" and c.status == "failed" for c in error.value.checks)
    assert {s.status for s in error.value.steps if s.target.startswith("mart.")} == {"skipped"}
    # витрина осталась как была
    assert one(conn, "SELECT temp_mean FROM mart.weather_daily WHERE obs_date = %s", day)[0] == before
    run_id, status = one(
        conn, "SELECT transform_run_id, status FROM meta.transform_run ORDER BY transform_run_id DESC LIMIT 1"
    )
    assert status == "failed"
    saved = one(
        conn,
        "SELECT status, failed_rows, severity FROM meta.dq_result "
        "WHERE transform_run_id = %s AND rule_code = %s",
        run_id,
        "always_fails",
    )
    assert saved == ("failed", 1, "critical")
    sql = "SELECT count(*) FROM meta.dq_result WHERE transform_run_id = %s AND status = 'skipped'"
    assert one(conn, sql, run_id)[0] > 0  # проверки витрин не выполнялись

    # без лишней проверки новое значение доходит до витрины
    run_transform(settings, conn)
    assert float(one(conn, "SELECT temp_mean FROM mart.weather_daily WHERE obs_date = %s", day)[0]) == -3.0


def test_broken_check_is_recorded_as_error(fresh_db, tmp_path):
    settings, conn, today = setup(fresh_db)
    ingest(settings, conn, today)
    project = project_copy(tmp_path, {"broken_check": "select * from no_such_table"})
    with pytest.raises(TransformError, match="broken_check"):
        run_transform(settings, conn, project_dir=project)
    status, message = one(conn, "SELECT status, message FROM meta.dq_result WHERE rule_code = 'broken_check'")
    assert status == "error" and "no_such_table" in message


def test_report_mode_does_not_change_data(fresh_db):
    settings, conn, today = setup(fresh_db)
    ingest(settings, conn, today)
    run_transform(settings, conn)
    day = today - timedelta(days=2)
    conn.execute("UPDATE staging.meteostat_daily SET pressure_hpa = 500 WHERE obs_date = %s", (day,))

    checks = run_checks(settings, conn, select="meteostat_values_allowed")
    assert [(c.rule.code, c.status, c.failed_rows) for c in checks] == [
        ("meteostat_values_allowed", "failed", 1)
    ]
    assert one(conn, "SELECT count(*) FROM staging.quarantine")[0] == 0
    assert one(conn, "SELECT count(*) FROM staging.meteostat_daily WHERE pressure_hpa = 500")[0] == 1
    assert one(conn, "SELECT count(*) FROM meta.transform_run")[0] == 1  # обработка не запускалась


def test_station_history_keeps_old_version(fresh_db):
    settings, conn, today = setup(fresh_db)
    ingest(settings, conn, today)
    run_transform(settings, conn)
    run_transform(settings, conn)  # описание не менялось — новой версии нет
    assert one(conn, "SELECT count(*) FROM core.dim_station")[0] == 1
    old_key = one(conn, "SELECT station_key FROM core.dim_station")[0]

    station = replace(settings.station, elevation_m=150.0, name="Москва, ВДНХ (новая)")
    result = run_transform(replace(settings, station=station), conn)
    rows = conn.execute(
        "SELECT station_key, name, elevation_m, valid_to IS NULL FROM core.dim_station ORDER BY valid_from"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == old_key and rows[0][3] is False
    assert rows[1][1] == "Москва, ВДНХ (новая)" and float(rows[1][2]) == 150.0 and rows[1][3] is True
    # уже существующие записи по-прежнему ссылаются на версию, с которой были созданы
    keys = one(conn, "SELECT count(DISTINCT station_key), min(station_key) FROM core.fact_weather_daily")
    assert keys == (1, old_key)
    check = next(c for c in result.checks if c.rule.code == "station_one_current_version")
    assert check.status == "passed"


def test_schema_registry_notices_changed_format(fresh_db):
    settings, conn, today = setup(fresh_db)
    ingest(settings, conn, today)
    counts = dict(conn.execute("SELECT source_code, count(*) FROM meta.source_schema GROUP BY 1").fetchall())
    assert counts == {"openmeteo_archive": 1, "openmeteo_forecast": 1, "meteostat_daily": 1}
    fields = one(conn, "SELECT fields FROM meta.source_schema WHERE source_code = 'meteostat_daily'")[0]
    columns = as_json(fields)
    assert columns[:4] == ["year", "month", "day", "temp"]

    # тот же формат — новая схема не появляется, растёт счётчик
    conn.execute("UPDATE raw.openmeteo_archive SET fetched_at = fetched_at - interval '1 day'")
    ingest(settings, conn, today)
    assert one(conn, "SELECT count(*) FROM meta.source_schema")[0] == 3
    archive = "SELECT {} FROM meta.source_schema WHERE source_code = 'openmeteo_archive'"
    assert one(conn, archive.format("times_seen"))[0] == 2

    # источник перестал отдавать переменную: схема записана, изменение видно в журнале
    conn.execute("UPDATE raw.openmeteo_archive SET fetched_at = fetched_at - interval '1 day'")
    result = run_ingest(
        settings,
        conn,
        http=FakeHttp(settings, today, drop_variable="pressure_msl_mean"),
        today=today,
        sources=["openmeteo_archive"],
    )
    notes = result.outcomes[0].stats.notes
    assert any("новая схема источника: удалены pressure_msl_mean" in n for n in notes)
    details = one(conn, "SELECT details FROM meta.load_log ORDER BY load_id DESC LIMIT 1")[0]
    assert any("удалены pressure_msl_mean" in n for n in as_json(details)["notes"])
    assert one(conn, archive.format("count(*)"))[0] == 2

    # проверка source_schema_changed замечает новую схему (первая была раньше, чем сутки назад)
    conn.execute(
        "UPDATE meta.source_schema SET first_seen_at = first_seen_at - interval '3 days' "
        "WHERE source_code = 'openmeteo_archive' AND times_seen = 2"
    )
    checks = run_checks(settings, conn, select="source_schema_changed")
    assert checks[0].status == "failed" and checks[0].failed_rows == 1
    assert checks[0].sample[0]["source_code"] == "openmeteo_archive"
