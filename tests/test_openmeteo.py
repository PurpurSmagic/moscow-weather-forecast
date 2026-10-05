import copy
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from weather.config import load_settings
from weather.ingest.base import PayloadError, year_chunks
from weather.ingest.openmeteo import (
    OpenMeteoArchiveLoader,
    OpenMeteoForecastLoader,
    last_date_with_value,
    payload_hash,
    validate_daily_payload,
)

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = load_settings(environ={})
ARCHIVE_VARS = SETTINGS.sources.openmeteo_archive.daily_variables


@pytest.fixture
def archive_payload():
    return json.loads((FIXTURES / "openmeteo_archive_2026-09-25_2026-10-05.json").read_text(encoding="utf-8"))


def validate(payload):
    return validate_daily_payload(payload, ARCHIVE_VARS, date(2026, 9, 25), date(2026, 10, 5))


# --- проверка структуры ответа ------------------------------------------------


def test_real_response_is_valid(archive_payload):
    assert validate(archive_payload) == 11


def test_missing_variable_is_schema_change(archive_payload):
    del archive_payload["daily"]["pressure_msl_mean"]
    with pytest.raises(PayloadError, match="нет переменных: pressure_msl_mean"):
        validate(archive_payload)


def test_changed_units_detected(archive_payload):
    archive_payload["daily_units"]["temperature_2m_mean"] = "°F"
    with pytest.raises(PayloadError, match="единица"):
        validate(archive_payload)


def test_wrong_period_detected(archive_payload):
    with pytest.raises(PayloadError, match="период ответа"):
        validate_daily_payload(archive_payload, ARCHIVE_VARS, date(2026, 9, 24), date(2026, 10, 5))


def test_series_length_mismatch_detected(archive_payload):
    archive_payload["daily"]["temperature_2m_max"].pop()
    with pytest.raises(PayloadError, match="длина рядов"):
        validate(archive_payload)


@pytest.mark.parametrize(
    "payload, message",
    [
        ([], "не является JSON-объектом"),
        ({"error": True, "reason": "limit"}, "ошибку: limit"),
        ({"latitude": 55}, "нет блока daily"),
    ],
)
def test_broken_payloads(payload, message):
    with pytest.raises(PayloadError, match=message):
        validate(payload)


# --- контрольная сумма и последняя дата ---------------------------------------


def test_hash_ignores_generation_time(archive_payload):
    other = copy.deepcopy(archive_payload)
    other["generationtime_ms"] = 999.0
    assert payload_hash(other) == payload_hash(archive_payload)
    other["daily"]["temperature_2m_mean"][0] += 0.1
    assert payload_hash(other) != payload_hash(archive_payload)


def test_last_date_skips_empty_recent_days(archive_payload):
    assert last_date_with_value(archive_payload) == date(2026, 10, 5)
    archive_payload["daily"]["temperature_2m_mean"][-2:] = [None, None]
    assert last_date_with_value(archive_payload) == date(2026, 10, 3)


def test_year_chunks():
    assert year_chunks(date(2024, 12, 20), date(2026, 1, 5)) == [
        (date(2024, 12, 20), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 5)),
    ]
    assert year_chunks(date(2026, 2, 1), date(2026, 1, 1)) == []


# --- планирование периода -------------------------------------------------------


class NoFetchToday:
    """Подставное подключение: архив сегодня ещё не запрашивался."""

    def __init__(self, fetched_on=None):
        self.fetched_on = fetched_on

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return (self.fetched_on,)


TODAY = date(2026, 10, 6)


def archive_loader(conn=None, **settings_changes):
    settings = replace(SETTINGS, **settings_changes)
    return OpenMeteoArchiveLoader(settings, http=None, conn=conn or NoFetchToday(), today=TODAY)


def test_archive_first_run_loads_full_history():
    plan = archive_loader().plan(None)
    assert (plan.period_from, plan.period_to) == (SETTINGS.history_start, date(2026, 10, 5))


def test_archive_next_run_loads_new_days_with_overlap():
    plan = archive_loader().plan(date(2026, 10, 1))
    # перекрытие 10 дней: перезапрашиваем 22.09–01.10 и добавляем 02.10–05.10
    assert (plan.period_from, plan.period_to) == (date(2026, 9, 22), date(2026, 10, 5))


def test_archive_same_day_rerun_is_skipped():
    assert archive_loader(conn=NoFetchToday(fetched_on=TODAY)).plan(date(2026, 10, 5)) is None
    assert archive_loader(conn=NoFetchToday(fetched_on=date(2026, 10, 5))).plan(date(2026, 10, 5)) is not None


def test_forecast_plan_once_per_day():
    loader = OpenMeteoForecastLoader(SETTINGS, http=None, conn=None, today=TODAY)
    plan = loader.plan(date(2026, 10, 5))
    assert (plan.period_from, plan.period_to) == (TODAY, date(2026, 10, 13))
    assert loader.plan(TODAY) is None
