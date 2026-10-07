"""Подменённые источники для интеграционных тестов: отвечают как Open-Meteo и Meteostat,
но без сети и с заранее заданными значениями."""

import csv
import gzip
import io
import json
from datetime import date, timedelta

from weather.http_client import HttpResult, SourceUnavailable


def default_value(day: date, name: str = "") -> float:
    base = 1005 if name == "pressure_msl_mean" else 5  # давление — в правдоподобных гПа
    return round(base + (day.toordinal() % 7) * 0.5, 1)


def fake_daily_payload(variables, start: date, end: date, drop=(), overrides=None):
    """Ответ в формате Open-Meteo. overrides = {переменная: {дата: значение}}."""
    overrides = overrides or {}
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    daily = {"time": [d.isoformat() for d in days]}
    for name in variables:
        if name in drop:
            continue
        values = overrides.get(name, {})
        daily[name] = [values.get(d, default_value(d, name)) for d in days]
    if "weather_code" in daily:
        daily["weather_code"] = [overrides.get("weather_code", {}).get(d, 3) for d in days]
    return {
        "latitude": 55.85,
        "longitude": 37.65,
        "generationtime_ms": 0.5,
        "daily_units": {name: "°C" for name in variables},
        "daily": daily,
    }


def fake_meteostat_csv(year, today, temps=None, default=5.0, gaps=(), model_days=()):
    """Годовой файл Meteostat: прошедшие дни — наблюдения, 7 дней вперёд — прогноз модели.
    gaps — дни без температуры; model_days — прошедшие дни, где есть только значение модели."""
    temps = temps or {}
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["year", "month", "day", "temp", "temp_source", "tmin", "tmin_source",
                     "tmax", "tmax_source", "prcp", "prcp_source"])  # fmt: skip
    day = date(year, 1, 1)
    last = min(date(year, 12, 31), today + timedelta(days=7))
    while day <= last:
        source = "dwd_mosmix" if day >= today or day in model_days else "dwd_poi"
        temp = "" if day in gaps else temps.get(day, default)
        tmin, tmax = (1.0, 9.0) if temp == "" else (temp - 4, temp + 4)
        writer.writerow([day.year, day.month, day.day, temp, source if temp != "" else "", tmin, source,
                         tmax, source, 0.0, source])  # fmt: skip
        day += timedelta(days=1)
    return gzip.compress(out.getvalue().encode("utf-8"))


class FakeHttp:
    """Вместо сети: отвечает по URL. Можно «сломать» источник, порцию или формат ответа."""

    def __init__(
        self,
        settings,
        today,
        broken=(),
        fail_on_chunk=None,
        drop_variable=None,
        archive_overrides=None,
        forecast_overrides=None,
        station_temps=None,
        station_default=5.0,
        station_gaps=(),
        station_model_days=(),
        file_version="v1",
    ):
        self.settings, self.today = settings, today
        self.broken = set(broken)
        self.fail_on_chunk = fail_on_chunk
        self.drop_variable = drop_variable
        self.archive_overrides = archive_overrides
        self.forecast_overrides = forecast_overrides
        self.station = {
            "temps": station_temps,
            "default": station_default,
            "gaps": station_gaps,
            "model_days": station_model_days,
        }
        self.file_version = file_version
        self.archive_calls = 0
        self.requests = self.retries = 0

    def reset_stats(self):
        self.requests = self.retries = 0

    def get(
        self, url, params=None, headers=None, *, min_interval_s=None, accept_statuses=frozenset({200, 304})
    ):
        self.requests += 1
        sources = self.settings.sources
        if url == sources.openmeteo_archive.url:
            if "archive" in self.broken:
                raise SourceUnavailable("архив недоступен")
            self.archive_calls += 1
            if self.fail_on_chunk == self.archive_calls:
                raise SourceUnavailable("обрыв на середине истории")
            start, end = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
            drop = (self.drop_variable,) if self.drop_variable else ()
            payload = fake_daily_payload(
                sources.openmeteo_archive.daily_variables, start, end, drop, self.archive_overrides
            )
            return HttpResult(200, url, {}, json.dumps(payload).encode())
        if url == sources.openmeteo_forecast.url:
            end = self.today + timedelta(days=params["forecast_days"] - 1)
            payload = fake_daily_payload(
                sources.openmeteo_forecast.daily_variables, self.today, end, overrides=self.forecast_overrides
            )
            return HttpResult(200, url, {}, json.dumps(payload).encode())
        if "meteostat" in url:
            if "meteostat" in self.broken:
                raise SourceUnavailable("Meteostat недоступен")
            year = int(url.split("/")[-2])
            etag = f'"{year}-{self.file_version}"'
            if headers and headers.get("If-None-Match") == etag:
                return HttpResult(304, url, {}, b"")
            content = fake_meteostat_csv(year, self.today, **self.station)
            return HttpResult(200, url, {"ETag": etag}, content)
        raise AssertionError(f"неожиданный URL {url}")
