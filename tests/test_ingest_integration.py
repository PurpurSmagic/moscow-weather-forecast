"""Загрузка целиком на настоящей базе: источники подменены, всё остальное — как в работе.

Проверяется то, что требует методичка: инкрементальность, идемпотентность,
устойчивость к сбоям и журнал загрузок.
"""

from dataclasses import replace
from datetime import date, timedelta

from fakes import FakeHttp

from weather.ingest.journal import Journal
from weather.ingest.runner import run_ingest, today_in


def setup(fresh_db, days_of_history=20):
    settings, conn = fresh_db
    today = today_in(settings.timezone)
    settings = replace(settings, history_start=today - timedelta(days=days_of_history))
    return settings, conn, today


def counts(conn):
    return {
        table: conn.execute(f"SELECT count(*) FROM raw.{table}").fetchone()[0]
        for table in ("openmeteo_archive", "openmeteo_forecast", "meteostat_file")
    }


def watermarks(conn):
    rows = conn.execute("SELECT source_code, loaded_until FROM meta.watermark").fetchall()
    return {code: day for code, day in rows}


# --- тесты ------------------------------------------------------------------------


def test_first_run_loads_everything_and_rerun_changes_nothing(fresh_db):
    settings, conn, today = setup(fresh_db)
    years = len({settings.history_start.year, today.year})

    first = run_ingest(settings, conn, http=FakeHttp(settings, today), today=today, trigger="test")
    assert first.status == "success"
    assert counts(conn) == {"openmeteo_archive": years, "openmeteo_forecast": 1, "meteostat_file": years}
    yesterday = today - timedelta(days=1)
    assert watermarks(conn) == {
        "openmeteo_archive": yesterday,
        "openmeteo_forecast": today,
        "meteostat_daily": yesterday,  # будущие строки прогноза модели не считаются
    }

    second = run_ingest(settings, conn, http=FakeHttp(settings, today), today=today, trigger="test")
    by_source = {o.source_code: o for o in second.outcomes}
    assert second.status == "success"
    assert by_source["openmeteo_archive"].status == "skipped"
    assert by_source["openmeteo_forecast"].status == "skipped"
    assert by_source["meteostat_daily"].stats.payloads_unchanged == years  # 304 Not Modified
    assert counts(conn) == {"openmeteo_archive": years, "openmeteo_forecast": 1, "meteostat_file": years}

    # журнал: по строке на источник в каждом запуске
    statuses = conn.execute(
        "SELECT run_id, source_code, status FROM meta.load_log ORDER BY load_id"
    ).fetchall()
    assert len(statuses) == 6


def test_unavailable_source_does_not_stop_others(fresh_db):
    settings, conn, today = setup(fresh_db)
    result = run_ingest(settings, conn, http=FakeHttp(settings, today, broken={"meteostat"}), today=today)
    by_source = {o.source_code: o for o in result.outcomes}
    assert result.status == "partial"
    assert by_source["meteostat_daily"].status == "failed"
    assert "недоступен" in by_source["meteostat_daily"].error
    assert by_source["openmeteo_archive"].status == "success"
    assert "meteostat_daily" not in watermarks(conn)
    run_status = conn.execute("SELECT status, error_message FROM meta.pipeline_run").fetchone()
    assert run_status[0] == "partial" and "meteostat_daily" in run_status[1]


def test_interrupted_history_resumes_from_watermark(fresh_db):
    settings, conn, today = setup(fresh_db, days_of_history=800)  # три календарных года
    only_archive = {"sources": ["openmeteo_archive"]}

    http = FakeHttp(settings, today, fail_on_chunk=2)
    first = run_ingest(settings, conn, http=http, today=today, **only_archive)
    outcome = first.outcomes[0]
    assert outcome.status == "partial" and outcome.stats.payloads_new == 1
    assert watermarks(conn)["openmeteo_archive"] == date(settings.history_start.year, 12, 31)

    conn.execute("UPDATE raw.openmeteo_archive SET fetched_at = fetched_at - interval '1 day'")
    second = run_ingest(settings, conn, http=FakeHttp(settings, today), today=today, **only_archive)
    assert second.outcomes[0].status == "success"
    assert second.outcomes[0].period[0] > settings.history_start  # история не грузится заново
    assert watermarks(conn)["openmeteo_archive"] == today - timedelta(days=1)


def test_changed_response_format_is_kept_but_not_used(fresh_db):
    settings, conn, today = setup(fresh_db)
    http = FakeHttp(settings, today, drop_variable="pressure_msl_mean")
    result = run_ingest(settings, conn, http=http, today=today, sources=["openmeteo_archive"])
    outcome = result.outcomes[0]
    assert outcome.status == "failed" and outcome.stats.payloads_invalid == 1
    valid, error = conn.execute("SELECT is_valid, validation_error FROM raw.openmeteo_archive").fetchone()
    assert valid is False and "pressure_msl_mean" in error
    assert "openmeteo_archive" not in watermarks(conn)


def test_watermark_moves_only_forward(fresh_db):
    settings, conn, today = setup(fresh_db)
    journal = Journal(conn)
    run_id = journal.start_run("test", {})
    load_id = journal.start_load(run_id, "openmeteo_archive", None, None, None)
    journal.advance_watermark("openmeteo_archive", date(2026, 10, 5), load_id)
    journal.advance_watermark("openmeteo_archive", date(2026, 9, 1), load_id)
    assert journal.watermark("openmeteo_archive") == date(2026, 10, 5)
