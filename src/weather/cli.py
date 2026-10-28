"""Командная строка проекта.

python -m weather run       — полный цикл: миграции, загрузка, обработка, прогноз
python -m weather migrate   — применить новые миграции схемы БД
python -m weather ingest    — загрузить новые данные из источников
python -m weather transform — обработать данные: raw -> staging -> core -> mart (dbt build)
python -m weather evaluate  — оценить качество моделей прогноза на истории
python -m weather forecast  — построить прогноз моделями на 7 суток
python -m weather show      — погода за последние дни, прогноз и признаки для решений
python -m weather loads     — журнал последних загрузок и отметки загрузки
python -m weather dq        — проверки качества данных (dbt test, без изменения данных)
python -m weather docs      — документация dbt: описание таблиц и схема зависимостей (lineage)
python -m weather check     — проверить настройки, подключение, слои, миграции и источники

Коды возврата: 0 — успешно, 1 — ошибка, загрузка с ошибками или не прошла критичная
проверка качества, 2 — ошибка настроек.
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

    run = commands.add_parser("run", help="полный цикл: миграции, загрузка, обработка данных и прогноз")
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

    commands.add_parser("transform", help="обработать данные: raw -> staging -> core -> mart (dbt build)")
    commands.add_parser(
        "evaluate", help="оценить качество моделей прогноза на истории (обучение, проверка, тест)"
    )
    commands.add_parser("forecast", help="построить прогноз моделями на 7 суток и обновить витрины")

    show = commands.add_parser("show", help="погода за последние дни, прогноз и признаки для решений")
    show.add_argument("--days", type=int, default=7, help="сколько последних дней факта показать")

    loads = commands.add_parser("loads", help="журнал последних загрузок и отметки загрузки")
    loads.add_argument("--limit", type=int, default=15, help="сколько последних загрузок показать")

    dq = commands.add_parser("dq", help="проверки качества данных (dbt test, без изменения данных)")
    dq.add_argument("--select", help="только эти проверки (имя проверки или правило выбора dbt)")
    dq.add_argument("--all", action="store_true", help="показать и пройденные проверки")

    docs = commands.add_parser("docs", help="документация dbt и схема зависимостей таблиц (lineage)")
    docs.add_argument("--port", type=int, default=8080, help="порт веб-страницы документации")
    docs.add_argument("--no-serve", action="store_true", help="только собрать документацию, не открывать")

    commands.add_parser("check", help="проверить настройки, подключение, слои, миграции и источники")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        setup_logging("INFO")
        log.error("Ошибка настроек: %s", exc)
        return 2
    setup_logging(settings.log_level, settings.timezone)

    handlers = {
        "run": cmd_run,
        "migrate": cmd_migrate,
        "ingest": cmd_ingest,
        "transform": cmd_transform,
        "evaluate": cmd_evaluate,
        "forecast": cmd_forecast,
        "show": cmd_show,
        "loads": cmd_loads,
        "dq": cmd_dq,
        "docs": cmd_docs,
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
    ingest_code = cmd_ingest(settings, args)
    # Обработка идёт, даже если какой-то источник не загрузился: используются данные,
    # загруженные раньше
    transform_code = cmd_transform(settings, args)
    # Параметры моделей подбираются один раз — при первом запуске (дальше: команда evaluate)
    if not _evaluated(settings):
        from weather.forecast.journal import ForecastError

        try:
            cmd_evaluate(settings, args)
        except ForecastError as exc:
            log.warning("Оценка качества моделей пропущена: %s", exc)
    # Прогноз по свежим данным; после него обновляются витрины с прогнозами
    forecast_code = cmd_forecast(settings, args)
    return ingest_code or transform_code or forecast_code


def _evaluated(settings: Settings) -> bool:
    """Была ли хотя бы одна успешная оценка качества моделей."""
    from weather import db

    with db.connect(settings.db, autocommit=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM meta.model_run WHERE kind = 'evaluate' AND status = 'success' LIMIT 1"
        ).fetchone()
    return row is not None


def cmd_transform(settings: Settings, args: argparse.Namespace) -> int:
    from weather import db
    from weather.transform import TransformError, run_transform

    db.wait_for_db(settings.db)
    with db.connect(settings.db, autocommit=True) as conn:
        try:
            result = run_transform(settings, conn)
        except TransformError as exc:
            if exc.steps:
                print("\nШаги (после ошибки зависящие шаги не выполнялись, их таблицы не менялись):")
                _print_steps(exc.steps)
            if exc.checks:
                print("\nПроверки качества:")
                _print_checks(exc.checks, show_passed=False)
            raise
    print(f"\nПреобразование №{result.transform_run_id}: {result.status}")
    _print_steps(result.steps)
    print("\nПроверки качества:")
    _print_checks(result.checks, show_passed=False)
    return 0


def _print_steps(steps: list[Any]) -> None:
    types = {"model": "модель", "seed": "справочник", "snapshot": "история"}
    statuses = {"success": "ок", "failed": "ошибка", "skipped": "пропущен"}
    print(
        _table(
            ["шаг", "тип", "таблица", "статус", "строк", "время, мс"],
            [
                [s.step, types.get(s.node_type, s.node_type), s.target, statuses.get(s.status, s.status),
                 "" if s.rows_after is None else s.rows_after, s.duration_ms]
                for s in steps
            ],
        )
    )  # fmt: skip


# Шаги dbt, которые зависят от моделей прогноза: общая таблица прогнозов и витрины
FORECAST_MARTS = "source:forecast+ source:meta.model_run+"


def cmd_evaluate(settings: Settings, args: argparse.Namespace) -> int:
    from weather import db
    from weather.forecast.evaluate import run_evaluate
    from weather.forecast.models import MODEL_NAMES
    from weather.transform import run_transform

    db.wait_for_db(settings.db)
    with db.connect(settings.db, autocommit=True) as conn:
        result = run_evaluate(settings, conn)
        run_transform(settings, conn, select=FORECAST_MARTS)

    m = settings.models
    print(f"\nОценка качества №{result.model_run_id}")
    print(
        f"  обучение — дни до {m.validation_from}; "
        f"подбор параметров — с {m.validation_from} до {m.test_from};"
    )
    print(f"  тест — с {m.test_from} по {result.data_until}")
    horizons = range(1, settings.forecast_horizon_days + 1)
    threshold = {}
    for x in result.metrics:
        if x.split == "test":
            threshold.setdefault(x.model_code, []).append(x.threshold_accuracy)
    rows = []
    for code, by_h in result.mae_table("test").items():
        mean = sum(by_h.values()) / len(by_h)
        acc = sum(threshold[code]) / len(threshold[code])
        rows.append([MODEL_NAMES[code], *[f"{by_h[h]:.2f}" for h in horizons], f"{mean:.2f}", f"{acc:.0%}"])
    print("\nСредняя абсолютная ошибка на тесте (MAE, °C) по горизонтам прогноза, сутки:")
    print(
        _table(
            ["модель", *[str(h) for h in horizons], "среднее", f"порог {settings.heating.threshold_c:+g}°"],
            rows,
        )
    )
    print(
        f"  порог {settings.heating.threshold_c:+g}° — доля дней, где прогноз и факт "
        "по одну сторону порога отопления"
    )

    print("\nВыбранные параметры:")
    for code, params in result.params.items():
        shown = {k: v for k, v in params.items() if k != "n_estimators"}
        if "n_estimators" in params:
            trees = params["n_estimators"].values()
            shown["деревьев"] = f"{min(trees)}–{max(trees)}"
        print(f"  {MODEL_NAMES[code]}: " + (", ".join(f"{k}={v}" for k, v in shown.items()) or "нет"))
    for code, values in result.importance.items():
        top = ", ".join(
            f"{k} ({v:+.2f})" if code == "ridge" else f"{k} ({v:.0%})" for k, v in list(values.items())[:5]
        )
        print(f"  главные признаки {MODEL_NAMES[code]}: {top}")
    return 0


def cmd_forecast(settings: Settings, args: argparse.Namespace) -> int:
    from weather import db
    from weather.forecast.models import MODEL_NAMES
    from weather.forecast.predict import run_forecast
    from weather.transform import run_transform

    db.wait_for_db(settings.db)
    with db.connect(settings.db, autocommit=True) as conn:
        result = run_forecast(settings, conn)
        run_transform(settings, conn, select=FORECAST_MARTS)

    print(
        f"\nПрогноз №{result.model_run_id}: выпуск {result.issue_date}, "
        f"параметры моделей {result.params_source}"
    )
    if result.inserted == 0:
        print("  прогноз с этой датой выпуска уже был — новых строк нет")
    codes = settings.models.codes
    table: dict[Any, dict[str, float]] = {}
    for r in result.rows:
        table.setdefault(r.target_date, {})[r.model_code] = r.temp_mean
    print(
        _table(
            ["дата", *[MODEL_NAMES[c] for c in codes]],
            [[d, *[values.get(c, "") for c in codes]] for d, values in sorted(table.items())],
        )
    )
    return 0


def cmd_dq(settings: Settings, args: argparse.Namespace) -> int:
    """Проверки по текущим данным (dbt test): ничего не меняет, результаты пишет в meta.dq_result."""
    from weather import db, dq
    from weather.transform import run_checks

    with db.connect(settings.db, autocommit=True) as conn:
        if not conn.execute("SELECT to_regclass('mart.weather_daily') IS NOT NULL").fetchone()[0]:
            print("Таблиц для проверок ещё нет — сначала выполните: python -m weather run")
            return 1
        checks = run_checks(settings, conn, select=args.select)
        dq.save_results(conn, checks, None)
        quarantine = conn.execute(
            "SELECT rule_code, source_table, count(*), min(row_key), max(row_key) "
            "FROM staging.quarantine GROUP BY rule_code, source_table ORDER BY rule_code"
        ).fetchall()

    print(f"\nПроверки качества данных ({len(checks)}):")
    _print_checks(checks, show_passed=args.all)
    if quarantine:
        print("\nВ карантине после последней обработки:")
        print(
            _table(
                ["правило", "таблица", "строк", "ключи"],
                [[r, t, n, k1 if k1 == k2 else f"{k1} … {k2}"] for r, t, n, k1, k2 in quarantine],
            )
        )
    return 1 if dq.blocking(checks) else 0


def cmd_docs(settings: Settings, args: argparse.Namespace) -> int:
    """Документация dbt: описания таблиц и колонок, тесты и граф зависимостей (lineage)."""
    from weather.transform import DBT_DIR, run_dbt

    run = run_dbt(settings, ["docs", "generate"])
    if not run.success:
        log.error("Документация не собрана: %s", run.error)
        return 1
    print(f"Документация собрана: {DBT_DIR / 'target' / 'index.html'}")
    if args.no_serve:
        return 0
    print(f"Откройте в браузере http://localhost:{args.port} (остановить — Ctrl+C)")
    run_dbt(settings, ["docs", "serve", "--host", "0.0.0.0", "--port", str(args.port), "--no-browser"])
    return 0


def _print_checks(checks: list[Any], show_passed: bool) -> None:
    from weather import dq

    print("  " + dq.summary(checks))
    shown = [c for c in checks if show_passed or c.status != "passed"]
    if not shown:
        return
    status_names = {"passed": "ок", "failed": "нарушения", "error": "ошибка", "skipped": "не выполнялась"}
    print(
        _table(
            ["правило", "тип", "уровень", "статус", "строк", "пример / сообщение"],
            [
                [
                    c.rule.code,
                    c.rule.check_type,
                    c.rule.severity,
                    status_names[c.status],
                    c.failed_rows,
                    _shorten(_example(c), 70),
                ]
                for c in shown
            ],
        )
    )


def _example(check: Any) -> str:
    if check.status == "error" or not check.sample:
        return check.message or ""
    first = check.sample[0]
    text = ", ".join(f"{k}={v}" for k, v in first.items()) if isinstance(first, dict) else str(first)
    return f"{check.message}; {text}" if check.message else text


def cmd_show(settings: Settings, args: argparse.Namespace) -> int:
    from weather import db

    with db.connect(settings.db, autocommit=True) as conn:
        if not conn.execute("SELECT to_regclass('mart.weather_daily') IS NOT NULL").fetchone()[0]:
            print("Витрин ещё нет — сначала выполните: python -m weather run")
            return 1
        actual = conn.execute(
            "SELECT obs_date, temp_mean, temp_norm, temp_anomaly, temp_source, cold_streak_days, "
            "heating_condition_met, precip_mm, weather_description "
            "FROM mart.weather_daily ORDER BY obs_date DESC LIMIT %s",
            (args.days,),
        ).fetchall()
        forecast = conn.execute(
            "SELECT target_date, temp_mean, temp_min, temp_max, temp_anomaly, cold_streak_days, "
            "heating_condition_met, ice_risk, weather_description, issue_date "
            "FROM mart.forecast_latest WHERE model_code = 'openmeteo' ORDER BY target_date"
        ).fetchall()
        models = conn.execute(
            "SELECT model_code, target_date, temp_mean, heating_condition_met FROM mart.forecast_latest "
            "WHERE model_code <> 'openmeteo' ORDER BY target_date"
        ).fetchall()
        best = None
        if conn.execute("SELECT to_regclass('mart.model_quality') IS NOT NULL").fetchone()[0]:
            best = conn.execute(
                "SELECT model_code, avg(mae) FROM mart.model_quality WHERE split = 'test' "
                "GROUP BY model_code ORDER BY avg(mae) LIMIT 1"
            ).fetchone()
    if not actual:
        print("Витрины пустые — сначала выполните: python -m weather run")
        return 1

    h = settings.heating
    cold = f"дней < {h.threshold_c:+g}°"
    print(f"{settings.station.name}: последние {len(actual)} дн. (данные по {actual[0][0]})")
    print(
        _table(
            ["дата", "t ср", "норма", "откл.", "источник", cold, "осадки, мм", "погода"],
            [
                [d, t, n, _signed(a), "станция" if src == "station" else "Open-Meteo*", k, p, w]
                for d, t, n, a, src, k, _, p, w in reversed(actual)
            ],
        )
    )
    if any(row[4] != "station" for row in actual):
        print("  * у станции нет наблюдения за день — взято из Open-Meteo с поправкой на смещение")

    if forecast:
        print(f"\nПрогноз Open-Meteo от {forecast[0][9]} (приведён к станции):")
        print(
            _table(
                ["дата", "t ср", "t мин", "t макс", "откл.", cold, "отопление", "гололёд", "погода"],
                [
                    [d, t, tmin, tmax, _signed(a), k, "да" if heat else "—", "риск" if ice else "—", w]
                    for d, t, tmin, tmax, a, k, heat, ice, w, _ in forecast
                ],
            )
        )

    if models:
        _print_model_forecast(models, forecast)

    print()
    streak, met = actual[0][5], actual[0][6]
    if met:
        print(f"Отопление: условие выполнено — {streak} дн. подряд ниже {h.threshold_c:+g} °C.")
    else:
        print(
            f"Отопление: сейчас {streak} дн. подряд ниже {h.threshold_c:+g} °C, нужно {h.consecutive_days}."
        )
        heat_day = next((row[0] for row in forecast if row[6]), None)
        if heat_day:
            print(f"  По прогнозу условие выполнится {heat_day}.")
        elif forecast:
            print("  По прогнозу на неделю условие не выполнится.")
        if best and not met:
            from weather.forecast.models import MODEL_NAMES

            day = next((d for code, d, _, heat in models if code == best[0] and heat), None)
            verdict = f"выполнится {day}" if day else "на неделю не выполнится"
            print(
                f"  По лучшей модели ({MODEL_NAMES.get(best[0], best[0])}, средняя ошибка на тесте "
                f"{float(best[1]):.1f} °C) условие {verdict}."
            )
    ice_days = [str(row[0]) for row in forecast if row[7]]
    print(
        "Гололёд: " + (f"риск по прогнозу — {', '.join(ice_days)}." if ice_days else "по прогнозу риска нет.")
    )
    return 0


def _print_model_forecast(models: list[Any], openmeteo: list[Any]) -> None:
    """Таблица прогнозов среднесуточной температуры: Open-Meteo и собственные модели."""
    from weather.forecast.models import MODEL_NAMES

    codes = [c for c in MODEL_NAMES if any(row[0] == c for row in models)]
    table: dict[Any, dict[str, Any]] = {}
    for code, day, temp, _ in models:
        table.setdefault(day, {})[code] = temp
    for day, temp, *_ in openmeteo:
        table.setdefault(day, {})["openmeteo"] = temp
    print("\nПрогноз среднесуточной температуры моделями, °C:")
    print(
        _table(
            ["дата", "Open-Meteo", *[MODEL_NAMES[c] for c in codes]],
            [[d, v.get("openmeteo", ""), *[v.get(c, "") for c in codes]] for d, v in sorted(table.items())],
        )
    )


def _signed(value: Any) -> str:
    return "" if value is None else f"{value:+.1f}"


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
        transforms = conn.execute(
            "SELECT transform_run_id, to_char(started_at AT TIME ZONE %s, 'YYYY-MM-DD HH24:MI'), status, "
            "coalesce(error_message, '') FROM meta.transform_run ORDER BY transform_run_id DESC LIMIT 5",
            (settings.timezone,),
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
    print("\nПоследние преобразования:")
    print(
        _table(
            ["№", "начало", "статус", "ошибка"],
            [list(row[:-1]) + [_shorten(row[-1], 80)] for row in transforms],
        )
    )
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

    print(f"Обработка: dbt {_dbt_version()} (проект в каталоге dbt/)")

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


def _dbt_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("dbt-core")
    except PackageNotFoundError:
        return "не установлен"


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
