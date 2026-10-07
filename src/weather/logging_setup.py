"""Единый формат логов для всех команд."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo


def setup_logging(level: str = "INFO", timezone: str | None = None) -> None:
    """Время в логах — в часовом поясе проекта (в Docker часы контейнера идут по UTC)."""
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    if timezone:
        tz = ZoneInfo(timezone)
        formatter.converter = lambda ts: datetime.fromtimestamp(ts, tz).timetuple()
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[handler], force=True)
