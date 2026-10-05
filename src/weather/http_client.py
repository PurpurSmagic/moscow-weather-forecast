"""HTTP-клиент для источников данных: таймауты, повторы при сбоях, ограничение частоты.

Повторяются только временные сбои: таймаут, обрыв соединения, HTTP 429 (превышен
лимит запросов) и 5xx (ошибка на стороне сервера). Пауза между попытками растёт
экспоненциально: base, 2·base, 4·base… (не больше backoff_max_s) плюс случайная
добавка, чтобы не бить в сервер синхронно. Если сервер прислал Retry-After —
ждём столько, сколько он просит; на 429 без Retry-After — сразу максимальную
паузу, потому что лимиты частоты обычно считаются по минутам. Если же источник
сообщает, что исчерпан часовой или суточный лимит, повторы не делаются: загрузка
завершается с ошибкой и продолжится при следующем запуске с той же отметки.

Ошибки запроса (4xx, кроме 429) не повторяются: повтор даст тот же результат.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import requests
from requests.structures import CaseInsensitiveDict

from weather.config import HttpSettings

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Признаки длинного окна лимита в ответе 429 (Open-Meteo: «Daily/Hourly API request limit exceeded»)
LONG_LIMIT = re.compile(r"daily|hourly|tomorrow|next hour", re.IGNORECASE)


class SourceError(RuntimeError):
    """Базовая ошибка обращения к источнику."""


class SourceUnavailable(SourceError):
    """Источник недоступен после всех попыток (сеть, таймаут, 429, 5xx)."""


class SourceRequestRejected(SourceError):
    """Источник отклонил запрос (4xx) — повтор не поможет."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    url: str
    headers: Mapping[str, str]
    content: bytes

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


class HttpClient:
    """Синхронный клиент с повторами. Счётчики requests/retries обнуляются через reset_stats()."""

    def __init__(
        self,
        settings: HttpSettings,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": settings.user_agent})
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None
        self.requests = 0
        self.retries = 0

    def reset_stats(self) -> None:
        self.requests = 0
        self.retries = 0

    def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        *,
        min_interval_s: float | None = None,
        accept_statuses: frozenset[int] = frozenset({200, 304}),
    ) -> HttpResult:
        cfg = self.settings
        interval = cfg.min_interval_s if min_interval_s is None else min_interval_s
        last_error = ""
        rate_limited = False
        for attempt in range(1, cfg.max_attempts + 1):
            self._respect_interval(interval)
            self.requests += 1
            retry_after: float | None = None
            rate_limited = False
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=cfg.timeout_s)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = f"{type(exc).__name__}: {_short(_root_cause(exc), 160)}"
            else:
                if response.status_code in accept_statuses:
                    return HttpResult(
                        response.status_code,
                        response.url,
                        CaseInsensitiveDict(response.headers),
                        response.content,
                    )
                if response.status_code not in RETRY_STATUSES:
                    raise SourceRequestRejected(
                        f"HTTP {response.status_code} от {url}: {_short(response.text)}"
                    )
                reason = error_reason(response.text)
                last_error = f"HTTP {response.status_code}" + (f": {reason}" if reason else "")
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                rate_limited = response.status_code == 429
                if rate_limited and LONG_LIMIT.search(reason):
                    # Исчерпан часовой или суточный лимит — повторять в ближайшие минуты бесполезно
                    raise SourceUnavailable(
                        f"{url}: лимит запросов источника исчерпан ({reason}) — "
                        "загрузка продолжится при следующем запуске"
                    )

            if attempt == cfg.max_attempts:
                break
            if retry_after is not None and retry_after > cfg.backoff_max_s:
                raise SourceUnavailable(
                    f"{url}: {last_error}, сервер просит подождать {retry_after:.0f} с — "
                    "дольше допустимого, загрузка отложена до следующего запуска"
                )
            if retry_after is not None:
                delay = retry_after
            elif rate_limited:
                # Лимит частоты обычно считается по минутам — короткие паузы не помогут
                delay = cfg.backoff_max_s
            else:
                delay = self._backoff(attempt)
            log.warning(
                "%s: %s — повтор через %.1f с (попытка %d из %d)",
                url,
                last_error,
                delay,
                attempt + 1,
                cfg.max_attempts,
            )
            self.retries += 1
            self._sleep(delay)

        raise SourceUnavailable(f"{url}: источник недоступен после {cfg.max_attempts} попыток ({last_error})")

    def _backoff(self, attempt: int) -> float:
        cfg = self.settings
        return min(cfg.backoff_max_s, cfg.backoff_base_s * 2 ** (attempt - 1)) + random.uniform(0, 1)

    def _respect_interval(self, interval: float) -> None:
        if self._last_request_at is not None and interval > 0:
            wait = self._last_request_at + interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()


def error_reason(body: str) -> str:
    """Текст причины из JSON-ответа об ошибке ({"error": true, "reason": "..."}), если он есть."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return ""
    reason = data.get("reason") if isinstance(data, dict) else None
    return _short(str(reason), 200) if reason else ""


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After бывает числом секунд или HTTP-датой."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, moment.timestamp() - time.time())


def _root_cause(exc: BaseException) -> str:
    """Самая глубокая причина исключения — без длинного URL с параметрами от requests."""
    while exc.__cause__ is not None or (exc.__context__ is not None and not exc.__suppress_context__):
        exc = exc.__cause__ or exc.__context__
    if exc.args and isinstance(exc.args[0], BaseException):
        return _root_cause(exc.args[0])
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
