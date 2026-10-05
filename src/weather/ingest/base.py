"""Общая часть загрузчиков."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, ClassVar

from weather.config import Settings
from weather.http_client import HttpClient, SourceError
from weather.ingest.journal import LoadStats


class PayloadError(SourceError):
    """Ответ источника получен, но его структура не соответствует ожидаемой."""


@dataclass(frozen=True)
class LoadPlan:
    """Что загрузчик собирается запросить у источника в этом запуске."""

    period_from: date
    period_to: date


AdvanceWatermark = Callable[[date], None]


class Loader(ABC):
    """Загрузчик одного источника.

    ``plan`` по отметке загрузки решает, какой период запросить (или что запрашивать
    нечего). ``load`` запрашивает данные, сохраняет их в RAW и сдвигает отметку —
    в одной транзакции на каждую порцию, поэтому после сбоя следующий запуск
    продолжает с места остановки.
    """

    source_code: ClassVar[str]
    nothing_to_do: ClassVar[str] = "новых данных нет"

    def __init__(self, settings: Settings, http: HttpClient, conn: Any, today: date) -> None:
        self.settings = settings
        self.http = http
        self.conn = conn
        self.today = today

    @property
    def yesterday(self) -> date:
        return self.today - timedelta(days=1)

    @property
    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    def plan(self, watermark: date | None) -> LoadPlan | None: ...

    @abstractmethod
    def load(self, plan: LoadPlan, load_id: int, stats: LoadStats, advance: AdvanceWatermark) -> None: ...


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Делит период [start; end] на порции по календарным годам."""
    return (
        [
            (max(start, date(year, 1, 1)), min(end, date(year, 12, 31)))
            for year in range(start.year, end.year + 1)
        ]
        if start <= end
        else []
    )
