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
class HttpSettings:
    timeout_s: float = 60
    max_attempts: int = 5
    backoff_base_s: float = 2
    backoff_max_s: float = 60
    min_interval_s: float = 1.0
    user_agent: str = "moscow-weather-forecast"


@dataclass(frozen=True)
class OpenMeteoArchiveSource:
    enabled: bool
    url: str
    daily_variables: tuple[str, ...]
    overlap_days: int
    min_interval_s: float


@dataclass(frozen=True)
class OpenMeteoForecastSource:
    enabled: bool
    url: str
    daily_variables: tuple[str, ...]
    forecast_days: int
    min_interval_s: float


@dataclass(frozen=True)
class MeteostatSource:
    enabled: bool
    url_template: str
    station_id: str
    overlap_days: int
    required_columns: tuple[str, ...]
    min_interval_s: float


@dataclass(frozen=True)
class SourcesSettings:
    openmeteo_archive: OpenMeteoArchiveSource
    openmeteo_forecast: OpenMeteoForecastSource
    meteostat_daily: MeteostatSource


@dataclass(frozen=True)
class ProcessingSettings:
    climate_norm_from_year: int
    climate_norm_to_year: int
    bias_period_from: date
    bias_period_to: date


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
    http: HttpSettings
    sources: SourcesSettings
    processing: ProcessingSettings
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
        http=_http_settings(_section(data, "http")),
        sources=_sources_settings(_section(data, "sources")),
        processing=_processing_settings(_section(data, "processing")),
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


# --- HTTP и источники данных -----------------------------------------------------


def _http_settings(section: Mapping[str, Any]) -> HttpSettings:
    where = "http"
    return HttpSettings(
        timeout_s=_float_in_range(section, "timeout_s", where, 1, 600),
        max_attempts=_int_in_range(section, "max_attempts", where, 1, 10),
        backoff_base_s=_float_in_range(section, "backoff_base_s", where, 0, 60),
        backoff_max_s=_float_in_range(section, "backoff_max_s", where, 0, 600),
        min_interval_s=_float_in_range(section, "min_interval_s", where, 0, 60),
        user_agent=str(_require(section, "user_agent", where)),
    )


def _sources_settings(section: Mapping[str, Any]) -> SourcesSettings:
    archive = _section(section, "openmeteo_archive")
    forecast = _section(section, "openmeteo_forecast")
    meteostat = _section(section, "meteostat_daily")
    return SourcesSettings(
        openmeteo_archive=OpenMeteoArchiveSource(
            enabled=_bool(archive, "enabled", "sources.openmeteo_archive"),
            url=_url(archive, "url", "sources.openmeteo_archive"),
            daily_variables=_variables(archive, "sources.openmeteo_archive"),
            overlap_days=_int_in_range(archive, "overlap_days", "sources.openmeteo_archive", 0, 60),
            min_interval_s=_float_in_range(archive, "min_interval_s", "sources.openmeteo_archive", 0, 60),
        ),
        openmeteo_forecast=OpenMeteoForecastSource(
            enabled=_bool(forecast, "enabled", "sources.openmeteo_forecast"),
            url=_url(forecast, "url", "sources.openmeteo_forecast"),
            daily_variables=_variables(forecast, "sources.openmeteo_forecast"),
            forecast_days=_int_in_range(forecast, "forecast_days", "sources.openmeteo_forecast", 2, 16),
            min_interval_s=_float_in_range(forecast, "min_interval_s", "sources.openmeteo_forecast", 0, 60),
        ),
        meteostat_daily=MeteostatSource(
            enabled=_bool(meteostat, "enabled", "sources.meteostat_daily"),
            url_template=_url_template(meteostat, "sources.meteostat_daily"),
            station_id=str(_require(meteostat, "station_id", "sources.meteostat_daily")),
            overlap_days=_int_in_range(meteostat, "overlap_days", "sources.meteostat_daily", 0, 366),
            required_columns=_str_list(meteostat, "required_columns", "sources.meteostat_daily"),
            min_interval_s=_float_in_range(meteostat, "min_interval_s", "sources.meteostat_daily", 0, 60),
        ),
    )


def _bool(section: Mapping[str, Any], key: str, where: str) -> bool:
    value = _require(section, key, where)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key} должен быть true или false, получено: {value!r}")
    return value


def _url(section: Mapping[str, Any], key: str, where: str) -> str:
    value = str(_require(section, key, where))
    if not value.startswith("https://"):
        raise ConfigError(f"{where}.{key} должен начинаться с https://, получено: {value!r}")
    return value


def _url_template(section: Mapping[str, Any], where: str) -> str:
    value = _url(section, "url_template", where)
    for placeholder in ("{year}", "{station}"):
        if placeholder not in value:
            raise ConfigError(f"{where}.url_template должен содержать {placeholder}")
    return value


def _str_list(section: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = _require(section, key, where)
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{where}.{key} должен быть непустым списком строк")
    if len(set(value)) != len(value):
        raise ConfigError(f"{where}.{key} содержит повторяющиеся значения")
    return tuple(value)


def _variables(section: Mapping[str, Any], where: str) -> tuple[str, ...]:
    variables = _str_list(section, "daily_variables", where)
    if "temperature_2m_mean" not in variables:
        raise ConfigError(
            f"{where}.daily_variables должен включать temperature_2m_mean — это целевой показатель"
        )
    return variables


def _processing_settings(section: Mapping[str, Any]) -> ProcessingSettings:
    where = "processing"
    norm_from = _int_in_range(section, "climate_norm_from_year", where, 1940, 2100)
    norm_to = _int_in_range(section, "climate_norm_to_year", where, 1940, 2100)
    if norm_from > norm_to:
        raise ConfigError(f"{where}: climate_norm_from_year больше climate_norm_to_year")
    bias_from = _parse_date(section, "bias_period_from", where)
    bias_to = _parse_date(section, "bias_period_to", where)
    if bias_from > bias_to:
        raise ConfigError(f"{where}: bias_period_from позже bias_period_to")
    return ProcessingSettings(norm_from, norm_to, bias_from, bias_to)
