"""Командная строка проекта.

python -m weather migrate   — применить новые миграции схемы БД
python -m weather check     — проверить настройки, подключение к БД, слои и миграции
"""

from __future__ import annotations

import argparse
import logging

from weather import __version__
from weather.config import ConfigError, Settings, load_settings
from weather.logging_setup import setup_logging

log = logging.getLogger("weather")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="weather",
        description="Мониторинг погодных условий в Москве: прогнозно-аналитическая система",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", help="путь к YAML-файлу настроек (по умолчанию из WEATHER_CONFIG)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="КОМАНДА")
    commands.add_parser("migrate", help="применить новые миграции схемы БД")
    commands.add_parser("check", help="проверить настройки, подключение к БД, слои и миграции")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        setup_logging("INFO")
        log.error("Ошибка настроек: %s", exc)
        return 2
    setup_logging(settings.log_level)

    handlers = {"migrate": cmd_migrate, "check": cmd_check}
    try:
        return handlers[args.command](settings)
    except Exception as exc:  # верхний уровень: понятное сообщение и код возврата
        log.error("%s: %s", type(exc).__name__, exc)
        log.debug("Подробности", exc_info=True)
        return 1


def cmd_migrate(settings: Settings) -> int:
    from weather import db, migrations

    db.wait_for_db(settings.db)
    with db.connect(settings.db, autocommit=True) as conn:
        applied = migrations.migrate(conn)
    if applied:
        log.info("Применено миграций: %d", len(applied))
    return 0


def cmd_check(settings: Settings) -> int:
    from weather import db, migrations

    ok = True
    s = settings.station
    print(f"Настройки: {settings.config_path}")
    print(f"  Станция: {s.name} (индекс ВМО {s.wmo_id}), {s.latitude}, {s.longitude}, {s.elevation_m:g} м")
    print(f"  История с {settings.history_start}; часовой пояс {settings.timezone}")
    print(
        f"  Прогноз: {settings.forecast_target} на {settings.forecast_horizon_days} сут.; "
        f"порог отопления +{settings.heating.threshold_c:g} °C × {settings.heating.consecutive_days} сут."
    )

    db.wait_for_db(settings.db, timeout_s=15)
    with db.connect(settings.db, autocommit=True) as conn:
        version = conn.execute("SHOW server_version").fetchone()[0]
        print(f"База данных: {settings.db.describe()} — PostgreSQL {version}")

        existing = {
            row[0]
            for row in conn.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)", (list(db.LAYERS),)
            ).fetchall()
        }
        print("Слои хранения:")
        for schema, purpose in db.LAYERS.items():
            mark = "ok" if schema in existing else "НЕТ"
            ok &= schema in existing
            print(f"  [{mark:>3}] {schema:<8} — {purpose}")

        applied, pending = migrations.status(conn)
        print(f"Миграции: применено {len(applied)}, ожидают {len(pending)}")
        for migration in pending:
            print(f"  ожидает: {migration.label}")
        ok &= not pending

    if ok:
        print("Итог: система готова к работе")
        return 0
    print("Итог: есть проблемы — выполните `python -m weather migrate`")
    return 1
