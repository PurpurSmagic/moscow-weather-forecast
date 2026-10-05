import gzip
from datetime import date
from pathlib import Path

import pytest

from weather.config import load_settings
from weather.ingest.base import PayloadError
from weather.ingest.meteostat import (
    MeteostatLoader,
    decompress,
    describe_column_change,
    last_observed_date,
    parse_csv,
)

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = load_settings(environ={})
REQUIRED = SETTINGS.sources.meteostat_daily.required_columns


def read(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_files_with_different_columns_parse_by_header():
    new = parse_csv(read("meteostat_27612_2026.csv"), REQUIRED)
    old = parse_csv(read("meteostat_27612_1991_head.csv"), REQUIRED)
    assert "snwd" in old.columns and "snwd" not in new.columns
    assert old.rows[0]["temp"] == "-0.7" and old.rows[0]["temp_source"] == "ghcnd"


def test_missing_required_column():
    text = read("meteostat_27612_2026.csv").replace("tmax,", "tmaximum,", 1)
    with pytest.raises(PayloadError, match="нет обязательных колонок: tmax"):
        parse_csv(text, REQUIRED)


def test_row_with_wrong_number_of_fields():
    text = read("meteostat_27612_2026.csv") + "2026,10,14,1.0\n"
    with pytest.raises(PayloadError, match="полей вместо"):
        parse_csv(text, REQUIRED)


def test_empty_file():
    with pytest.raises(PayloadError, match="пустой"):
        parse_csv("", REQUIRED)


def test_last_observed_date_ignores_future_model_rows():
    rows = parse_csv(read("meteostat_27612_2026.csv"), REQUIRED).rows
    assert max(date(int(r["year"]), int(r["month"]), int(r["day"])) for r in rows) == date(2026, 10, 13)
    assert last_observed_date(rows, before=date(2026, 10, 6)) == date(2026, 10, 5)


def test_decompress():
    assert decompress(gzip.compress("год,месяц\n".encode())) == "год,месяц\n"
    with pytest.raises(PayloadError, match="gzip"):
        decompress(b"not a gzip")


def test_column_change_description():
    assert describe_column_change(None, ["a", "b"]) is None
    assert describe_column_change(["a", "b"], ["a", "b"]) is None
    assert describe_column_change(["a", "snwd"], ["a", "cldc"]) == (
        "состав колонок изменился: добавлены cldc; удалены snwd"
    )


def test_plan_years():
    loader = MeteostatLoader(SETTINGS, http=None, conn=None, today=date(2026, 1, 10))
    first = loader.plan(None)
    assert first.period_from == date(SETTINGS.history_start.year, 1, 1)
    # перекрытие 30 дней от 05.01.2026 захватывает декабрь 2025 → проверяем оба года
    assert loader.plan(date(2026, 1, 5)).period_from == date(2025, 1, 1)
    assert MeteostatLoader(SETTINGS, None, None, date(2026, 10, 6)).plan(
        date(2026, 10, 5)
    ).period_from == date(2026, 1, 1)
