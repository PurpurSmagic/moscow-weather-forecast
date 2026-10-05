from datetime import date
from pathlib import Path

import pytest
import yaml

from weather.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, ConfigError, load_settings

BASE_ENV = {"POSTGRES_PASSWORD": "secret"}


def write_config(tmp_path: Path, **overrides) -> Path:
    """Копия рабочего settings.yaml с изменёнными разделами."""
    data = yaml.safe_load((PROJECT_ROOT / DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    for dotted, value in overrides.items():
        section, key = dotted.split("__")
        data[section][key] = value
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_project_config_loads():
    settings = load_settings(environ=BASE_ENV)
    assert settings.station.wmo_id == "27612"
    assert settings.timezone == "Europe/Moscow"
    assert settings.history_start == date(1991, 1, 1)
    assert settings.forecast_horizon_days == 7
    assert settings.heating.threshold_c == 8.0
    assert settings.heating.consecutive_days == 5


def test_database_settings_from_environment():
    env = {
        "POSTGRES_HOST": "db",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "w",
        "POSTGRES_USER": "u",
        "POSTGRES_PASSWORD": "p",
    }
    db = load_settings(environ=env).db
    assert db.connect_kwargs() == {"host": "db", "port": 5432, "dbname": "w", "user": "u", "password": "p"}


def test_password_not_exposed_in_logs():
    db = load_settings(environ={"POSTGRES_PASSWORD": "top-secret"}).db
    assert "top-secret" not in repr(db)
    assert "top-secret" not in db.describe()


def test_config_path_from_environment(tmp_path):
    path = write_config(tmp_path, forecast__horizon_days=3)
    settings = load_settings(environ={**BASE_ENV, "WEATHER_CONFIG": str(path)})
    assert settings.forecast_horizon_days == 3


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"forecast__horizon_days": 30}, "horizon_days"),
        ({"forecast__horizon_days": "7"}, "целым числом"),
        ({"station__latitude": 155}, "latitude"),
        ({"history__start_date": "01.01.1991"}, "ГГГГ-ММ-ДД"),
        ({"project__timezone": "Moscow/Mars"}, "часовой пояс"),
    ],
)
def test_invalid_values_rejected(tmp_path, overrides, message):
    path = write_config(tmp_path, **overrides)
    with pytest.raises(ConfigError, match=message):
        load_settings(path, environ=BASE_ENV)


def test_invalid_port_rejected():
    with pytest.raises(ConfigError, match="POSTGRES_PORT"):
        load_settings(environ={"POSTGRES_PORT": "abc"})


def test_invalid_log_level_rejected():
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load_settings(environ={"LOG_LEVEL": "LOUD"})


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="Не найден"):
        load_settings(tmp_path / "nope.yaml", environ=BASE_ENV)
