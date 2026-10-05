"""Загрузка настроек.

Настройки берутся из двух мест:
* переменные окружения (и файл .env) — подключение к БД, уровень логирования,
  путь к файлу настроек;
* YAML-файл (по умолчанию config/settings.yaml) — параметры предметной области.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = Path("config/settings.yaml")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(ValueError):
    """Ошибка в настройках: понятное сообщение вместо трассировки стека."""


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)

    def connect_kwargs(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.name,
            "user": self.user,
            "password": self.password,
        }

    def describe(self) -> str:
        """Строка для логов — без пароля."""
        return f"{self.user}@{self.host}:{self.port}/{self.name}"


@dataclass(frozen=True)
class StationSettings:
    wmo_id: str
    name: str
    latitude: float
    longitude: float
    elevation_m: float


@dataclass(frozen=True)
class HeatingRule:
    threshold_c: float
    consecutive_days: int


@dataclass(frozen=True)
class Settings:
    db: DatabaseSettings
    station: StationSettings
    timezone: str
    history_start: date
    forecast_target: str
    forecast_horizon_days: int
    heating: HeatingRule
    ice_threshold_c: float
    log_level: str
    config_path: Path
    # Полное содержимое YAML — для разделов, которые появятся на следующих этапах
    raw: Mapping[str, Any] = field(repr=False, default_factory=dict)


def load_settings(
    config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Собирает настройки из окружения и YAML-файла.

    Если ``environ`` не передан, используется ``os.environ``, а перед этим
    подгружается файл .env из корня проекта (если он есть). Уже заданные
    переменные окружения .env не перезаписывает.
    """
    if environ is None:
        _load_dotenv()
        environ = os.environ

    path = Path(config_path or environ.get("WEATHER_CONFIG") or DEFAULT_CONFIG_PATH)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    data = _read_yaml(path)

    return Settings(
        db=_database_settings(environ),
        station=_station_settings(_section(data, "station")),
        timezone=_timezone(_section(data, "project")),
        history_start=_parse_date(_section(data, "history"), "start_date", "history"),
        forecast_target=str(_require(_section(data, "forecast"), "target", "forecast")),
        forecast_horizon_days=_int_in_range(_section(data, "forecast"), "horizon_days", "forecast", 1, 14),
        heating=_heating_rule(_section(_section(data, "decision_rules"), "heating")),
        ice_threshold_c=float(
            _require(
                _section(_section(data, "decision_rules"), "ice_risk"),
                "threshold_c",
                "decision_rules.ice_risk",
            )
        ),
        log_level=_log_level(environ),
        config_path=path,
        raw=data,
    )


# --- окружение ---------------------------------------------------------------


def _load_dotenv() -> None:
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)


def _database_settings(environ: Mapping[str, str]) -> DatabaseSettings:
    port_raw = environ.get("POSTGRES_PORT", "5432")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ConfigError(f"POSTGRES_PORT должен быть числом, получено: {port_raw!r}") from exc
    return DatabaseSettings(
        host=environ.get("POSTGRES_HOST", "localhost"),
        port=port,
        name=environ.get("POSTGRES_DB", "weather"),
        user=environ.get("POSTGRES_USER", "weather"),
        password=environ.get("POSTGRES_PASSWORD", "change_me"),
    )


def _log_level(environ: Mapping[str, str]) -> str:
    level = environ.get("LOG_LEVEL", "INFO").upper()
    if level not in LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL должен быть одним из {LOG_LEVELS}, получено: {level!r}")
    return level


# --- YAML --------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Не найден файл настроек: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Ошибка синтаксиса YAML в {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Файл {path} должен содержать словарь разделов")
    return data


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise ConfigError(f"В настройках нет раздела '{name}'")
    return value


def _require(section: Mapping[str, Any], key: str, where: str) -> Any:
    if section.get(key) is None:
        raise ConfigError(f"В разделе '{where}' не задан параметр '{key}'")
    return section[key]


def _float_in_range(section: Mapping[str, Any], key: str, where: str, low: float, high: float) -> float:
    value = float(_require(section, key, where))
    if not low <= value <= high:
        raise ConfigError(f"{where}.{key} = {value} вне допустимого диапазона [{low}; {high}]")
    return value


def _int_in_range(section: Mapping[str, Any], key: str, where: str, low: int, high: int) -> int:
    value = _require(section, key, where)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{where}.{key} должен быть целым числом, получено: {value!r}")
    if not low <= value <= high:
        raise ConfigError(f"{where}.{key} = {value} вне допустимого диапазона [{low}; {high}]")
    return value


def _parse_date(section: Mapping[str, Any], key: str, where: str) -> date:
    value = _require(section, key, where)
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{where}.{key}: ожидается дата ГГГГ-ММ-ДД, получено {value!r}") from exc


def _station_settings(section: Mapping[str, Any]) -> StationSettings:
    return StationSettings(
        wmo_id=str(_require(section, "wmo_id", "station")),
        name=str(_require(section, "name", "station")),
        latitude=_float_in_range(section, "latitude", "station", -90, 90),
        longitude=_float_in_range(section, "longitude", "station", -180, 180),
        elevation_m=float(_require(section, "elevation_m", "station")),
    )


def _timezone(section: Mapping[str, Any]) -> str:
    name = str(_require(section, "timezone", "project"))
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Неизвестный часовой пояс: {name!r}") from exc
    return name


def _heating_rule(section: Mapping[str, Any]) -> HeatingRule:
    where = "decision_rules.heating"
    return HeatingRule(
        threshold_c=float(_require(section, "threshold_c", where)),
        consecutive_days=_int_in_range(section, "consecutive_days", where, 1, 30),
    )
