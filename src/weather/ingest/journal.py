"""Журнал запусков и загрузок, отметки инкрементальной загрузки (схема meta)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any


@dataclass
class LoadStats:
    """Счётчики одной загрузки — попадают в meta.load_log."""

    requests: int = 0
    retries: int = 0
    payloads_received: int = 0
    payloads_new: int = 0
    payloads_unchanged: int = 0
    payloads_invalid: int = 0
    records_received: int = 0
    notes: list[str] = field(default_factory=list)


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class Journal:
    """Запись в meta.pipeline_run, meta.load_log и meta.watermark.

    Подключение должно быть в режиме autocommit: строки журнала фиксируются сразу,
    чтобы запуск, упавший на середине, всё равно остался в журнале.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # --- запуски ---------------------------------------------------------------

    def start_run(self, trigger: str, params: dict[str, Any]) -> int:
        row = self.conn.execute(
            "INSERT INTO meta.pipeline_run (trigger, params) VALUES (%s, %s::jsonb) RETURNING run_id",
            (trigger, to_json(params)),
        ).fetchone()
        return int(row[0])

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE meta.pipeline_run SET finished_at = now(), status = %s, error_message = %s "
            "WHERE run_id = %s",
            (status, error, run_id),
        )

    # --- загрузки --------------------------------------------------------------

    def start_load(
        self,
        run_id: int,
        source_code: str,
        period_from: date | None,
        period_to: date | None,
        watermark_before: date | None,
    ) -> int:
        row = self.conn.execute(
            "INSERT INTO meta.load_log (run_id, source_code, period_from, period_to, watermark_before) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING load_id",
            (run_id, source_code, period_from, period_to, watermark_before),
        ).fetchone()
        return int(row[0])

    def finish_load(
        self,
        load_id: int,
        status: str,
        stats: LoadStats,
        watermark_after: date | None,
        error: str | None = None,
    ) -> None:
        counters = asdict(stats)
        notes = counters.pop("notes")
        self.conn.execute(
            "UPDATE meta.load_log SET finished_at = now(), status = %s, "
            "requests = %s, retries = %s, payloads_received = %s, payloads_new = %s, "
            "payloads_unchanged = %s, payloads_invalid = %s, records_received = %s, "
            "watermark_after = %s, error_message = %s, details = %s::jsonb "
            "WHERE load_id = %s",
            (
                status,
                counters["requests"],
                counters["retries"],
                counters["payloads_received"],
                counters["payloads_new"],
                counters["payloads_unchanged"],
                counters["payloads_invalid"],
                counters["records_received"],
                watermark_after,
                error,
                to_json({"notes": notes}),
                load_id,
            ),
        )

    # --- отметки загрузки ------------------------------------------------------

    def watermark(self, source_code: str) -> date | None:
        row = self.conn.execute(
            "SELECT loaded_until FROM meta.watermark WHERE source_code = %s", (source_code,)
        ).fetchone()
        return row[0] if row else None

    def advance_watermark(self, source_code: str, loaded_until: date, load_id: int) -> None:
        """Сдвигает отметку только вперёд: повторная загрузка старого периода её не откатывает."""
        self.conn.execute(
            "INSERT INTO meta.watermark (source_code, loaded_until, load_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (source_code) DO UPDATE SET "
            "  load_id = CASE WHEN EXCLUDED.loaded_until > meta.watermark.loaded_until "
            "                 THEN EXCLUDED.load_id ELSE meta.watermark.load_id END, "
            "  loaded_until = GREATEST(meta.watermark.loaded_until, EXCLUDED.loaded_until), "
            "  updated_at = now()",
            (source_code, loaded_until, load_id),
        )
