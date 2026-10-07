"""Проверки качества данных, история станции и реестр схем источников."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from fakes import FakeHttp

from weather import dq
from weather.ingest.runner import run_ingest, today_in
from weather.ingest.schema_registry import describe_change, openmeteo_fields
from weather.transform import TransformError, run_transform


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


def rule(**kw):
    base = {
        "code": "test_rule",
        "check_type": "полнота",
        "phase": "final",
        "severity": "critical",
        "table": "mart.weather_daily",
        "description": "тест",
        "sql": "SELECT 1 AS x",
    }
    return dq.Rule(**{**base, **kw})


def write_rules(tmp_path, text):
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    return path


RULE_YAML = """
rules:
  - code: {code}
    type: {type}
    phase: {phase}
    severity: {severity}
    table: {table}
    description: тест
    sql: SELECT 1
"""


# --- без базы ---------------------------------------------------------------------


def test_project_rules_are_valid_and_cover_all_check_types():
    rules = dq.load_rules()
    assert len({r.code for r in rules}) == len(rules)
    # все семь типов проверок из методички + сверка источников
    assert {r.check_type for r in rules} == set(dq.CHECK_TYPES)
    assert {r.phase for r in rules} == {"staging", "final"}
    assert {r.severity for r in rules} == {"warning", "quarantine", "critical"}
    for r in rules:
        if r.severity == "quarantine":
            assert r.phase == "staging" and r.key


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"type": "красота"}, "неизвестный тип"),
        ({"phase": "later"}, "phase"),
        ({"severity": "fatal"}, "severity"),
        ({"table": "weather_daily"}, "схема.таблица"),
        ({"severity": "quarantine", "phase": "final"}, "только на этапе staging"),
        ({"severity": "quarantine", "phase": "staging", "table": "staging.meteostat_daily"}, "нужен key"),
    ],
)
def test_bad_rules_are_rejected(tmp_path, fields, message):
    values = {
        "code": "r1",
        "type": "полнота",
        "phase": "final",
        "severity": "warning",
        "table": "core.fact_weather_daily",
    }
    path = write_rules(tmp_path, RULE_YAML.format(**{**values, **fields}))
    with pytest.raises(dq.RulesError, match=message):
        dq.load_rules(path)


def test_duplicate_rule_codes_are_rejected(tmp_path):
    values = {
        "code": "r1",
        "type": "полнота",
        "phase": "final",
        "severity": "warning",
        "table": "core.fact_weather_daily",
    }
    text = RULE_YAML.format(**values) + RULE_YAML.format(**values).replace("rules:\n", "")
    with pytest.raises(dq.RulesError, match="дважды"):
        dq.load_rules(write_rules(tmp_path, text))


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

    enabled = [r for r in dq.load_rules() if r.enabled]
    assert len(result.checks) == len(enabled)
    not_passed = {c.rule.code: c.status for c in result.checks if c.status != "passed"}
    assert not_passed == {}
    sql = "SELECT count(*) FROM meta.dq_result WHERE transform_run_id = %s"
    saved = one(conn, sql, result.transform_run_id)
    assert saved[0] == len(enabled)
    assert one(conn, "SELECT count(*) FROM staging.quarantine")[0] == 0


def test_bad_station_row_goes_to_quarantine(fresh_db):
    settings, conn, today = setup(fresh_db)
    bad_day = today - timedelta(days=5)
    ingest(settings, conn, today, station_temps={bad_day: 99.0})
    result = run_transform(settings, conn)

    check = next(c for c in result.checks if c.rule.code == "meteostat_temp_range")
    assert check.status == "failed" and check.quarantined == 1
    row_key, rule_code, data = one(conn, "SELECT row_key, rule_code, row_data FROM staging.quarantine")
    assert (str(row_key), rule_code) == (bad_day.isoformat(), "meteostat_temp_range")
    assert float(as_json(data)["temp_mean"]) == 99.0
    # плохого значения нет ни в staging, ни в core: день взят из Open-Meteo с поправкой
    assert one(conn, "SELECT count(*) FROM staging.meteostat_daily WHERE obs_date = %s", bad_day)[0] == 0
    source, temp = one(
        conn, "SELECT temp_source, temp_mean FROM mart.weather_daily WHERE obs_date = %s", bad_day
    )
    assert source == "openmeteo_adjusted" and temp < 50
    assert result.status == "success"


def test_critical_failure_rolls_back_and_keeps_results(fresh_db):
    settings, conn, today = setup(fresh_db)
    day = today - timedelta(days=3)
    ingest(settings, conn, today)
    run_transform(settings, conn)
    before = one(conn, "SELECT temp_mean FROM mart.weather_daily WHERE obs_date = %s", day)[0]

    # станция уточнила значение, но обработка не проходит критичную проверку
    conn.execute("UPDATE raw.meteostat_file SET fetched_at = fetched_at - interval '1 day'")
    ingest(settings, conn, today, file_version="v2", station_temps={day: -3.0})
    rules = dq.load_rules() + [rule(code="always_fails")]
    with pytest.raises(TransformError, match="always_fails") as error:
        run_transform(settings, conn, rules=rules)

    assert any(c.rule.code == "always_fails" and c.status == "failed" for c in error.value.checks)
    # витрина осталась как была
    assert one(conn, "SELECT temp_mean FROM mart.weather_daily WHERE obs_date = %s", day)[0] == before
    run_id, status = one(
        conn, "SELECT transform_run_id, status FROM meta.transform_run ORDER BY transform_run_id DESC LIMIT 1"
    )
    assert status == "failed"
    saved = one(
        conn,
        "SELECT status, failed_rows FROM meta.dq_result WHERE transform_run_id = %s AND rule_code = %s",
        run_id,
        "always_fails",
    )
    assert saved == ("failed", 1)

    # без лишнего правила новое значение доходит до витрины
    run_transform(settings, conn)
    assert float(one(conn, "SELECT temp_mean FROM mart.weather_daily WHERE obs_date = %s", day)[0]) == -3.0


def test_broken_rule_is_recorded_as_error(fresh_db):
    settings, conn, today = setup(fresh_db)
    ingest(settings, conn, today)
    broken = rule(code="broken_warning", severity="warning", sql="SELECT * FROM no_such_table")
    result = run_transform(settings, conn, rules=[broken])
    assert result.status == "success"
    assert result.checks[0].status == "error" and "no_such_table" in result.checks[0].message

    with pytest.raises(TransformError, match="broken_critical"):
        run_transform(settings, conn, rules=[replace(broken, code="broken_critical", severity="critical")])


def test_report_mode_does_not_move_rows(fresh_db):
    settings, conn, today = setup(fresh_db)
    ingest(settings, conn, today)
    run_transform(settings, conn)
    day = today - timedelta(days=2)
    conn.execute("UPDATE staging.meteostat_daily SET temp_mean = 99 WHERE obs_date = %s", (day,))

    rules = [r for r in dq.load_rules() if r.code == "meteostat_temp_range"]
    with conn.transaction():
        checks = dq.run_checks(conn, rules)
    assert checks[0].status == "failed" and checks[0].quarantined == 0
    assert one(conn, "SELECT count(*) FROM staging.quarantine")[0] == 0
    assert one(conn, "SELECT count(*) FROM staging.meteostat_daily WHERE temp_mean = 99")[0] == 1


def test_station_history_keeps_old_version(fresh_db):
    settings, conn, today = setup(fresh_db)
    ingest(settings, conn, today)
    run_transform(settings, conn)
    run_transform(settings, conn)  # описание не менялось — новой версии нет
    assert one(conn, "SELECT count(*) FROM core.dim_station")[0] == 1
    old_key = one(conn, "SELECT station_key FROM core.dim_station")[0]

    station = replace(settings.station, elevation_m=150.0, name="Москва, ВДНХ (новая)")
    moved = replace(settings, station=station)
    result = run_transform(moved, conn)
    rows = conn.execute(
        "SELECT station_key, name, elevation_m, is_current, valid_to IS NULL "
        "FROM core.dim_station ORDER BY station_key"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == old_key and rows[0][3] is False and rows[0][4] is False
    assert rows[1][1] == "Москва, ВДНХ (новая)" and float(rows[1][2]) == 150.0 and rows[1][3] is True
    # уже существующие записи по-прежнему ссылаются на версию, с которой были созданы
    assert one(conn, "SELECT count(DISTINCT station_key) FROM core.fact_weather_daily")[0] == 1
    assert one(conn, "SELECT min(station_key) FROM core.fact_weather_daily")[0] == old_key
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
    rules = [r for r in dq.load_rules() if r.code == "source_schema_changed"]
    with conn.transaction():
        checks = dq.run_checks(conn, rules)
    assert checks[0].status == "failed" and checks[0].failed_rows == 1
    assert checks[0].sample[0]["source_code"] == "openmeteo_archive"
