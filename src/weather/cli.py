"""Командная строка проекта.

python -m weather run       — полный цикл: миграции + загрузка всех источников
python -m weather migrate   — применить новые миграции схемы БД
python -m weather ingest    — загрузить новые данные из источников
python -m weather loads     — журнал последних загрузок и отметки загрузки
python -m weather check     — проверить настройки, подключение, слои, миграции и источники

Коды возврата: 0 — успешно, 1 — ошибка или загрузка с ошибками, 2 — ошибка настроек.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from typing import Any

from weather import __version__
from weather.config import ConfigError, Settings, load_settings
from weather.logging_setup import setup_logging

log = logging.getLogger("weather")

SOURCE_CHOICES = ("openmeteo_archive", "openmeteo_forecast", "meteostat_daily")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="weather",
        description="Мониторинг погодных условий в Москве: прогнозно-аналитическая система",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", help="путь к YAML-файлу настроек (по умолчанию из WEATHER_CONFIG)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="КОМАНДА")

    run = commands.add_parser("run", help="полный цикл: миграции и загрузка всех источников")
    run.add_argument("--trigger", choices=("manual", "schedule"), default="manual", help="кто запустил")

    commands.add_parser("migrate", help="применить новые миграции схемы БД")

    ingest = commands.add_parser("ingest", help="загрузить новые данные из источников")
    ingest.add_argument(
        "--source",
        action="append",
        choices=SOURCE_CHOICES,
        help="загрузить только этот источник (можно указать несколько раз)",
    )
    ingest.add_argument("--trigger", choices=("manual", "schedule"), default="manual", help="кто запустил")

    loads = commands.add_parser("loads", help="журнал последних загрузок и отметки загрузки")
    loads.add_argument("--limit", type=int, default=15, help="сколько последних загрузок показать")

    commands.add_parser("check", help="проверить настройки, подключение, слои, миграции и источники")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        setup_logging("INFO")
        log.error("Ошибка настроек: %s", exc)
        return 2
    setup_logging(settings.log_level)

    handlers = {
        "run": cmd_run,
        "migrate": cmd_migrate,
        "ingest": cmd_ingest,
        "loads": cmd_loads,
        "check": cmd_check,
    }
    try:
        return handlers[args.command](settings, args)
    except Exception as exc:  # верхний уровень: понятное сообщение и код возврата
        log.error("%s: %s", type(exc).__name__, exc)
        log.debug("Подробности", exc_info=True)
        return 1


# --- команды ----------------------------------------------------------------------


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    code = cmd_migrate(settings, args)
    if code:
        return code
    args.source = None
    return cmd_ingest(settings, args)


def cmd_migrate(settings: Settings, args: argparse.Namespace) -> int:
    from weather import db, migrations

    db.wait_for_db(settings.db)
    with db.connect(settings.db, autocommit=True) as conn:
        applied = migrations.migrate(conn)
    if applied:
        log.info("Применено миграций: %d", len(applied))
    return 0


def cmd_ingest(settings: Settings, args: argparse.Namespace) -> int:
    from weather import db
    from weather.ingest.runner import run_ingest

    db.wait_for_db(settings.db)
    with db.connect(settings.db, autocommit=True) as conn:
        result = run_ingest(settings, conn, sources=args.source, trigger=args.trigger)

    print(f"\nЗагрузка №{result.run_id}: {result.status}")
    rows = []
    for o in result.outcomes:
        s = o.stats
        rows.append(
            [
                o.source_code,
                o.status,
                f"{o.period[0]}..{o.period[1]}" if o.period else "—",
                s.requests,
                s.retries,
                s.payloads_new,
                s.payloads_unchanged,
                s.payloads_invalid,
                s.records_received,
                f"{o.watermark_before or '—'} → {o.watermark_after or '—'}",
            ]
        )
    print(
        _table(
            [
                "источник",
                "статус",
                "период",
                "запросов",
                "повторов",
                "новых",
                "без изм.",
                "ошибок",
                "записей",
                "отметка загрузки",
            ],
            rows,
        )
    )
    for o in result.outcomes:
        for note in o.stats.notes:
            print(f"  {o.source_code}: {note}")
        if o.error:
            print(f"  {o.source_code}: ОШИБКА — {o.error}")
    return 0 if result.status == "success" else 1


def cmd_loads(settings: Settings, args: argparse.Namespace) -> int:
    from weather import db

    with db.connect(settings.db, autocommit=True) as conn:
        loads = conn.execute(
            "SELECT load_id, run_id, source_code, to_char(started_at AT TIME ZONE %s, 'YYYY-MM-DD HH24:MI'), "
            "status, coalesce(period_from::text || '..' || period_to::text, '—'), requests, retries, "
            "payloads_new, payloads_unchanged, payloads_invalid, records_received, "
            "coalesce(watermark_after::text, '—'), coalesce(error_message, '') "
            "FROM meta.load_log ORDER BY load_id DESC LIMIT %s",
            (settings.timezone, args.limit),
        ).fetchall()
        marks = conn.execute(
            "SELECT s.source_code, coalesce(w.loaded_until::text, '—'), "
            "coalesce(to_char(w.updated_at AT TIME ZONE %s, 'YYYY-MM-DD HH24:MI'), '—') "
            "FROM meta.source s LEFT JOIN meta.watermark w USING (source_code) ORDER BY s.source_code",
            (settings.timezone,),
        ).fetchall()

    print(f"Последние загрузки (время — {settings.timezone}):")
    print(
        _table(
            [
                "load_id",
                "запуск",
                "источник",
                "начало",
                "статус",
                "период",
                "запросов",
                "повторов",
                "новых",
                "без изм.",
                "ошибок",
                "записей",
                "отметка",
                "ошибка",
            ],
            [list(row[:-1]) + [_shorten(row[-1], 60)] for row in loads],
        )
    )
    print("\nОтметки загрузки:")
    print(_table(["источник", "загружено по", "обновлено"], [list(row) for row in marks]))
    return 0


def cmd_check(settings: Settings, args: argparse.Namespace) -> int:
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

        if conn.execute("SELECT to_regclass('meta.load_log') IS NOT NULL").fetchone()[0]:
            print("Источники:")
            for row in _sources_status(conn, settings):
                print("  " + row)

    if ok:
        print("Итог: система готова к работе")
        return 0
    print("Итог: есть проблемы — выполните `python -m weather migrate`")
    return 1


# --- вспомогательное ------------------------------------------------------------


def _sources_status(conn: Any, settings: Settings) -> list[str]:
    rows = conn.execute(
        "SELECT s.source_code, w.loaded_until, l.status, "
        "to_char(l.started_at AT TIME ZONE %s, 'YYYY-MM-DD HH24:MI') "
        "FROM meta.source s "
        "LEFT JOIN meta.watermark w USING (source_code) "
        "LEFT JOIN LATERAL (SELECT status, started_at FROM meta.load_log "
        "                   WHERE source_code = s.source_code ORDER BY load_id DESC LIMIT 1) l ON true "
        "ORDER BY s.source_code",
        (settings.timezone,),
    ).fetchall()
    enabled = {
        "openmeteo_archive": settings.sources.openmeteo_archive.enabled,
        "openmeteo_forecast": settings.sources.openmeteo_forecast.enabled,
        "meteostat_daily": settings.sources.meteostat_daily.enabled,
    }
    lines = []
    for code, loaded_until, status, started in rows:
        state = "включён" if enabled.get(code, False) else "отключён"
        last = f"последняя загрузка {started} — {status}" if status else "ещё не загружался"
        lines.append(f"{code:<19} {state}; данные по {loaded_until or '—'}; {last}")
    return lines


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[str(h) for h in headers]] + [[str(c) for c in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join("  " + line for line in lines)


def _shorten(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
