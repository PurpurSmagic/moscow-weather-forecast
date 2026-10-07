"""Источник Meteostat: годовые сжатые CSV-файлы суточных данных станции."""

from __future__ import annotations

import csv
import gzip
import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from weather.ingest.base import AdvanceWatermark, Loader, LoadPlan, PayloadError, sha256_text
from weather.ingest.journal import LoadStats
from weather.ingest.schema_registry import register_schema

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedFile:
    columns: list[str]
    rows: list[dict[str, str]]


def decompress(content: bytes) -> str:
    try:
        return gzip.decompress(content).decode("utf-8")
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise PayloadError(f"файл не распаковывается как gzip/UTF-8: {exc}") from exc


def parse_csv(text: str, required_columns: Sequence[str]) -> ParsedFile:
    """Разбирает CSV по заголовку, а не по позициям: состав колонок у Meteostat
    различается от года к году (например, snwd есть не во всех файлах)."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise PayloadError("файл пустой") from exc
    missing = [name for name in required_columns if name not in header]
    if missing:
        raise PayloadError(f"нет обязательных колонок: {', '.join(missing)} — источник изменил формат")
    rows = []
    for number, values in enumerate(reader, start=2):
        if not values:
            continue
        if len(values) != len(header):
            raise PayloadError(f"строка {number}: {len(values)} полей вместо {len(header)}")
        rows.append(dict(zip(header, values, strict=True)))
    return ParsedFile(header, rows)


def row_date(row: dict[str, str]) -> date:
    try:
        return date(int(row["year"]), int(row["month"]), int(row["day"]))
    except (KeyError, ValueError) as exc:
        raise PayloadError(
            f"некорректная дата в строке: {row.get('year')}-{row.get('month')}-{row.get('day')}"
        ) from exc


def last_observed_date(rows: Sequence[dict[str, str]], before: date) -> date | None:
    """Последний прошедший день с температурой.

    В текущем году файл содержит и будущие даты — это прогноз модели DWD MOSMIX,
    им Meteostat заполняет ряд вперёд. Для отметки загрузки такие дни не считаются.
    """
    observed = [d for row in rows if row.get("temp") and (d := row_date(row)) < before]
    return max(observed) if observed else None


def describe_column_change(previous: Sequence[str] | None, current: Sequence[str]) -> str | None:
    if previous is None or list(previous) == list(current):
        return None
    added = [c for c in current if c not in previous]
    removed = [c for c in previous if c not in current]
    parts = []
    if added:
        parts.append("добавлены " + ", ".join(added))
    if removed:
        parts.append("удалены " + ", ".join(removed))
    return "состав колонок изменился: " + ("; ".join(parts) if parts else "другой порядок")


class MeteostatLoader(Loader):
    """Скачивает годовые файлы. Уже загруженный файл запрашивается условно (If-None-Match):
    если он не менялся, сервер отвечает 304 и файл не скачивается."""

    source_code = "meteostat_daily"

    @property
    def cfg(self):
        return self.settings.sources.meteostat_daily

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def plan(self, watermark: date | None) -> LoadPlan | None:
        start_year = self.settings.history_start.year
        if watermark is not None:
            start_year = max(start_year, (watermark - timedelta(days=self.cfg.overlap_days)).year)
        return LoadPlan(date(start_year, 1, 1), self.yesterday)

    def load(self, plan: LoadPlan, load_id: int, stats: LoadStats, advance: AdvanceWatermark) -> None:
        cfg = self.cfg
        for year in range(plan.period_from.year, plan.period_to.year + 1):
            url = cfg.url_template.format(year=year, station=cfg.station_id)
            previous = self.conn.execute(
                "SELECT etag, columns FROM raw.meteostat_file "
                "WHERE station_id = %s AND year = %s AND is_valid "
                "ORDER BY fetched_at DESC, file_id DESC LIMIT 1",
                (cfg.station_id, year),
            ).fetchone()
            headers = {"If-None-Match": previous[0]} if previous and previous[0] else None
            result = self.http.get(
                url,
                headers=headers,
                min_interval_s=cfg.min_interval_s,
                accept_statuses=frozenset({200, 304, 404}),
            )
            if result.status == 404:
                stats.notes.append(f"{year}: файла нет у источника")
                continue
            if result.not_modified:
                stats.payloads_unchanged += 1
                continue

            stats.payloads_received += 1
            text, columns, rows, error = "", [], [], None
            try:
                text = decompress(result.content)
                parsed = parse_csv(text, cfg.required_columns)
                columns, rows = parsed.columns, parsed.rows
                last = last_observed_date(rows, before=self.today)
            except PayloadError as exc:
                error, last = str(exc), None
                # заголовок нужен реестру схем, даже если файл не прошёл проверку
                columns = text.split("\n", 1)[0].strip().split(",") if text.strip() else []

            with self.conn.transaction():
                schema_change = (
                    register_schema(self.conn, self.source_code, columns, load_id) if columns else None
                )
                row = self.conn.execute(
                    "INSERT INTO raw.meteostat_file (load_id, station_id, year, url, etag, last_modified, "
                    "content, content_sha256, columns, row_count, is_valid, validation_error) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (station_id, year, content_sha256) DO NOTHING RETURNING file_id",
                    (
                        load_id,
                        cfg.station_id,
                        year,
                        result.url,
                        result.headers.get("ETag"),
                        result.headers.get("Last-Modified"),
                        text,
                        sha256_text(text),
                        columns,
                        len(rows),
                        error is None,
                        error,
                    ),
                ).fetchone()
                if last is not None:
                    advance(last)

            if schema_change:
                stats.notes.append(f"{year}: {schema_change}")
                log.warning("%s: %s — %s", self.source_code, year, schema_change)
            if error is not None:
                # Испорченный файл за один год не мешает загрузить остальные
                stats.payloads_invalid += 1
                stats.notes.append(f"{year}: {error}")
                log.warning("%s: %s — %s", self.source_code, year, error)
                continue
            change = describe_column_change(previous[1] if previous else None, columns)
            if change:
                stats.notes.append(f"{year}: {change}")
                log.warning("%s: %s — %s", self.source_code, year, change)
            stats.records_received += len(rows)
            if row is None:
                stats.payloads_unchanged += 1
            else:
                stats.payloads_new += 1
