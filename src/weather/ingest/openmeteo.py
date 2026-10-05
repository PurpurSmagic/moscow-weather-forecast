"""Источник Open-Meteo: история (Historical Weather API) и прогноз (Forecast API), формат JSON."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from weather.ingest.base import AdvanceWatermark, Loader, LoadPlan, PayloadError, sha256_text, year_chunks
from weather.ingest.journal import LoadStats, to_json

log = logging.getLogger(__name__)

# Поля ответа, которые меняются от запроса к запросу при тех же данных.
# В контрольную сумму не входят, иначе повторный запрос выглядел бы как новые данные.
VOLATILE_FIELDS = frozenset({"generationtime_ms"})
TARGET_VARIABLE = "temperature_2m_mean"


def payload_hash(payload: dict[str, Any]) -> str:
    stable = {key: value for key, value in payload.items() if key not in VOLATILE_FIELDS}
    return sha256_text(json.dumps(stable, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def validate_daily_payload(
    payload: Any, variables: Sequence[str], expected_from: date, expected_to: date
) -> int:
    """Проверяет структуру ответа и возвращает число суток в нём.

    Ловит изменения на стороне источника: пропавшие переменные, другую длину рядов,
    другие единицы измерения, неожиданный период.
    """
    if not isinstance(payload, dict):
        raise PayloadError("ответ не является JSON-объектом")
    if payload.get("error"):
        raise PayloadError(f"источник вернул ошибку: {payload.get('reason')}")
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        raise PayloadError("в ответе нет блока daily")
    times = daily.get("time")
    if not isinstance(times, list) or not times:
        raise PayloadError("в блоке daily нет списка дат time")

    missing = [name for name in variables if name not in daily]
    if missing:
        raise PayloadError(f"в ответе нет переменных: {', '.join(missing)} — источник изменил формат")
    wrong_length = [
        name for name in variables if not isinstance(daily[name], list) or len(daily[name]) != len(times)
    ]
    if wrong_length:
        raise PayloadError(f"длина рядов не совпадает с числом дат: {', '.join(wrong_length)}")

    try:
        first, last = date.fromisoformat(times[0]), date.fromisoformat(times[-1])
    except (TypeError, ValueError) as exc:
        raise PayloadError(f"даты в неожиданном формате: {times[0]!r}") from exc
    if (first, last) != (expected_from, expected_to):
        raise PayloadError(
            f"период ответа {first}..{last} вместо запрошенного {expected_from}..{expected_to}"
        )
    if len(times) != (last - first).days + 1:
        raise PayloadError("в ряду дат есть пропуски или повторы")

    unit = payload.get("daily_units", {}).get(TARGET_VARIABLE)
    if unit != "°C":
        raise PayloadError(f"единица {TARGET_VARIABLE}: {unit!r} вместо '°C'")
    return len(times)


def last_date_with_value(payload: dict[str, Any], variable: str = TARGET_VARIABLE) -> date | None:
    """Последняя дата, для которой у переменной есть значение (свежие дни бывают пустыми)."""
    daily = payload["daily"]
    for day, value in zip(reversed(daily["time"]), reversed(daily[variable]), strict=True):
        if value is not None:
            return date.fromisoformat(day)
    return None


def _decode_json(content: bytes) -> Any:
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayloadError(f"ответ не является корректным JSON: {exc}") from exc


class OpenMeteoArchiveLoader(Loader):
    """История погоды. Первый запуск загружает всё с history.start_date порциями,
    следующие — только новые дни плюс перекрытие для уточнённых значений."""

    source_code = "openmeteo_archive"

    @property
    def cfg(self):
        return self.settings.sources.openmeteo_archive

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    nothing_to_do = "данные по вчерашний день уже загружены сегодня"

    def plan(self, watermark: date | None) -> LoadPlan | None:
        end = self.yesterday  # архив отдаёт данные по вчерашний день включительно
        if watermark is not None and watermark >= end and self._fetched_today():
            # Перекрытие перезапрашиваем раз в сутки: повторный запуск в тот же день ничего не меняет
            return None
        start = self.settings.history_start
        if watermark is not None:
            start = max(start, watermark + timedelta(days=1 - self.cfg.overlap_days))
        return LoadPlan(start, end) if start <= end else None

    def _fetched_today(self) -> bool:
        row = self.conn.execute(
            "SELECT (max(fetched_at) AT TIME ZONE %s)::date FROM raw.openmeteo_archive WHERE is_valid",
            (self.settings.timezone,),
        ).fetchone()
        return bool(row and row[0] == self.today)

    def load(self, plan: LoadPlan, load_id: int, stats: LoadStats, advance: AdvanceWatermark) -> None:
        cfg, station = self.cfg, self.settings.station
        chunks = year_chunks(plan.period_from, plan.period_to)
        log.info("%s: %d порц. за %s..%s", self.source_code, len(chunks), plan.period_from, plan.period_to)
        for chunk_from, chunk_to in chunks:
            params = {
                "latitude": station.latitude,
                "longitude": station.longitude,
                "start_date": chunk_from.isoformat(),
                "end_date": chunk_to.isoformat(),
                "daily": ",".join(cfg.daily_variables),
                "timezone": self.settings.timezone,
            }
            result = self.http.get(cfg.url, params=params, min_interval_s=cfg.min_interval_s)
            payload = _decode_json(result.content)
            stats.payloads_received += 1
            try:
                records = validate_daily_payload(payload, cfg.daily_variables, chunk_from, chunk_to)
                error = None
            except PayloadError as exc:
                records, error = 0, str(exc)

            with self.conn.transaction():
                row = self.conn.execute(
                    "INSERT INTO raw.openmeteo_archive (load_id, period_from, period_to, request_url, "
                    "request_params, payload, payload_sha256, is_valid, validation_error) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s) "
                    "ON CONFLICT (period_from, period_to, payload_sha256) DO NOTHING RETURNING response_id",
                    (
                        load_id,
                        chunk_from,
                        chunk_to,
                        result.url,
                        to_json(params),
                        to_json(payload),
                        payload_hash(payload),
                        error is None,
                        error,
                    ),
                ).fetchone()
                if error is None:
                    last = last_date_with_value(payload)
                    if last is not None:
                        advance(last)

            if error is not None:
                stats.payloads_invalid += 1
                # Структура ответа сломалась — следующие порции придут такими же, останавливаемся
                raise PayloadError(f"{chunk_from}..{chunk_to}: {error}")
            stats.records_received += records
            if row is None:
                stats.payloads_unchanged += 1
            else:
                stats.payloads_new += 1
            log.info(
                "%s: %s..%s — %d сут.%s",
                self.source_code,
                chunk_from,
                chunk_to,
                records,
                "" if row else " (без изменений)",
            )


class OpenMeteoForecastLoader(Loader):
    """Прогноз на неделю вперёд. Один снимок на дату выпуска (сегодня по Москве)."""

    source_code = "openmeteo_forecast"
    nothing_to_do = "прогноз на сегодня уже сохранён"

    @property
    def cfg(self):
        return self.settings.sources.openmeteo_forecast

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def plan(self, watermark: date | None) -> LoadPlan | None:
        if watermark is not None and watermark >= self.today:
            return None
        return LoadPlan(self.today, self.today + timedelta(days=self.cfg.forecast_days - 1))

    def load(self, plan: LoadPlan, load_id: int, stats: LoadStats, advance: AdvanceWatermark) -> None:
        cfg, station = self.cfg, self.settings.station
        params = {
            "latitude": station.latitude,
            "longitude": station.longitude,
            "daily": ",".join(cfg.daily_variables),
            "forecast_days": cfg.forecast_days,
            "timezone": self.settings.timezone,
        }
        result = self.http.get(cfg.url, params=params, min_interval_s=cfg.min_interval_s)
        payload = _decode_json(result.content)
        stats.payloads_received += 1
        try:
            records = validate_daily_payload(payload, cfg.daily_variables, plan.period_from, plan.period_to)
            error = None
        except PayloadError as exc:
            records, error = 0, str(exc)

        with self.conn.transaction():
            row = self.conn.execute(
                "INSERT INTO raw.openmeteo_forecast (load_id, issue_date, request_url, request_params, "
                "payload, payload_sha256, is_valid, validation_error) "
                "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s) "
                "ON CONFLICT (issue_date) WHERE is_valid DO NOTHING RETURNING response_id",
                (
                    load_id,
                    plan.period_from,
                    result.url,
                    to_json(params),
                    to_json(payload),
                    payload_hash(payload),
                    error is None,
                    error,
                ),
            ).fetchone()
            if error is None:
                advance(plan.period_from)

        if error is not None:
            stats.payloads_invalid += 1
            raise PayloadError(error)
        stats.records_received += records
        if row is None:
            stats.payloads_unchanged += 1
        else:
            stats.payloads_new += 1
        log.info("%s: прогноз от %s на %d сут.", self.source_code, plan.period_from, records)
