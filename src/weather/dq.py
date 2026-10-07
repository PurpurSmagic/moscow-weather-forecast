"""Проверки качества данных.

Правила лежат в config/dq_rules.yaml. Каждое правило — SQL-запрос, который возвращает
строки-нарушения: пустой результат значит, что проверка пройдена.

Проверки выполняются внутри преобразования (transform.py) в два этапа:
* staging — после разбора данных источников, до сведения их в core.
  Строки, нарушающие правила с уровнем quarantine, переносятся в staging.quarantine
  и дальше не идут;
* final — после сборки витрин, перед сохранением результата.

Если не прошло правило с уровнем critical, преобразование отменяется целиком
и витрины остаются такими, какими были до запуска. Результаты всех проверок
записываются в meta.dq_result.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from weather.config import PROJECT_ROOT

log = logging.getLogger(__name__)

RULES_PATH = PROJECT_ROOT / "config" / "dq_rules.yaml"

CHECK_TYPES = (
    "полнота",
    "уникальность",
    "допустимость значений",
    "соответствие типов",
    "соответствие диапазонам",
    "ссылочная целостность",
    "актуальность",
    "сверка источников",
)
PHASES = ("staging", "final")
SEVERITIES = ("warning", "quarantine", "critical")
SAMPLE_SIZE = 5

CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
TABLE_RE = re.compile(r"^[a-z_]+\.[a-z_][a-z0-9_]*$")


class RulesError(ValueError):
    """Ошибка в файле правил."""


@dataclass(frozen=True)
class Rule:
    code: str
    check_type: str
    phase: str
    severity: str
    table: str
    description: str
    sql: str
    key: str | None = None
    enabled: bool = True


@dataclass
class CheckResult:
    rule: Rule
    status: str  # passed | failed | error
    failed_rows: int = 0
    sample: list[Any] = field(default_factory=list)
    message: str | None = None
    quarantined: int = 0

    @property
    def is_blocking(self) -> bool:
        """Критичное правило не прошло (или не смогло выполниться)."""
        return self.rule.severity == "critical" and self.status != "passed"


# --- правила ----------------------------------------------------------------------


def load_rules(path: Path = RULES_PATH) -> list[Rule]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RulesError(f"не удалось прочитать {path}: {exc}") from exc
    items = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise RulesError(f"{path.name}: нет списка rules")

    rules, seen = [], set()
    for number, item in enumerate(items, start=1):
        rule = _parse_rule(item, f"{path.name}, правило {number}")
        if rule.code in seen:
            raise RulesError(f"{path.name}: правило {rule.code} описано дважды")
        seen.add(rule.code)
        rules.append(rule)
    return rules


def _parse_rule(item: Any, where: str) -> Rule:
    if not isinstance(item, dict):
        raise RulesError(f"{where}: ожидается словарь")
    for name in ("code", "type", "phase", "severity", "table", "description", "sql"):
        if not isinstance(item.get(name), str) or not item[name].strip():
            raise RulesError(f"{where}: не заполнено поле {name}")
    code = item["code"]
    where = f"{where} ({code})"
    if not CODE_RE.match(code):
        raise RulesError(f"{where}: код — латиница в нижнем регистре, цифры и _")
    if item["type"] not in CHECK_TYPES:
        raise RulesError(f"{where}: неизвестный тип «{item['type']}», допустимы: {', '.join(CHECK_TYPES)}")
    if item["phase"] not in PHASES:
        raise RulesError(f"{where}: phase должен быть одним из: {', '.join(PHASES)}")
    if item["severity"] not in SEVERITIES:
        raise RulesError(f"{where}: severity должен быть одним из: {', '.join(SEVERITIES)}")
    if not TABLE_RE.match(item["table"]):
        raise RulesError(f"{where}: table должна быть в виде схема.таблица")
    key = item.get("key")
    if item["severity"] == "quarantine":
        if item["phase"] != "staging":
            raise RulesError(f"{where}: карантин возможен только на этапе staging")
        if not item["table"].startswith("staging."):
            raise RulesError(f"{where}: в карантин можно убирать только строки таблиц staging")
        if not isinstance(key, str) or not CODE_RE.match(key):
            raise RulesError(f"{where}: для карантина нужен key — колонка, по которой ищутся строки")
    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RulesError(f"{where}: enabled должен быть true или false")
    return Rule(
        code=code,
        check_type=item["type"],
        phase=item["phase"],
        severity=item["severity"],
        table=item["table"],
        description=" ".join(item["description"].split()),
        sql=item["sql"].strip().rstrip(";"),
        key=key,
        enabled=enabled,
    )


# --- выполнение -------------------------------------------------------------------


def run_checks(
    conn: Any,
    rules: list[Rule],
    phase: str | None = None,
    *,
    quarantine_run_id: int | None = None,
) -> list[CheckResult]:
    """Выполняет включённые правила этапа (или все, если phase не указан).

    quarantine_run_id — номер преобразования: если задан, строки по правилам
    с уровнем quarantine переносятся в staging.quarantine. Без него проверки только
    считают нарушения (так работает команда dq).

    Должно вызываться внутри транзакции: каждое правило выполняется в своей точке
    сохранения, поэтому ошибка в одном правиле не мешает остальным.
    """
    results = []
    for rule in rules:
        if rule.enabled and (phase is None or rule.phase == phase):
            results.append(run_rule(conn, rule, quarantine_run_id))
    return results


def run_rule(conn: Any, rule: Rule, quarantine_run_id: int | None = None) -> CheckResult:
    try:
        with conn.transaction():
            count = conn.execute(f"SELECT count(*) FROM ({rule.sql}) v").fetchone()[0]
            if not count:
                return CheckResult(rule, "passed")
            sample = [
                _json(row[0])
                for row in conn.execute(
                    f"SELECT to_jsonb(v)::text FROM ({rule.sql}) v LIMIT {SAMPLE_SIZE}"
                ).fetchall()
            ]
            moved = 0
            if rule.severity == "quarantine" and quarantine_run_id is not None:
                moved = _quarantine(conn, rule, quarantine_run_id)
    except Exception as exc:  # ошибка в самом правиле: записываем и идём дальше
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        log.warning("Проверка %s не выполнилась: %s", rule.code, message)
        return CheckResult(rule, "error", message=message)

    if moved:
        message = f"в карантин: {moved} стр."
    elif rule.severity == "quarantine" and quarantine_run_id is not None:
        message = "строки не найдены в таблице по ключу"
    else:
        message = None
    return CheckResult(rule, "failed", int(count), sample, message, moved)


def _quarantine(conn: Any, rule: Rule, run_id: int) -> int:
    """Переносит строки-нарушения из таблицы staging в staging.quarantine.

    Значения подставляются прямо в текст запроса (без параметров), чтобы знак %
    в SQL правила не путался с подстановкой. Код, таблица и ключ уже проверены
    регулярными выражениями при загрузке правил, run_id — целое число.
    """
    sql = f"""
        WITH bad AS (
            SELECT DISTINCT v.{rule.key} AS k FROM ({rule.sql}) v
        ),
        moved AS (
            DELETE FROM {rule.table} t USING bad WHERE t.{rule.key} = bad.k RETURNING t.*
        ),
        kept AS (
            INSERT INTO staging.quarantine (transform_run_id, rule_code, source_table, row_key, row_data)
            SELECT {int(run_id)}, '{rule.code}', '{rule.table}', m.{rule.key}::text, to_jsonb(m)
            FROM moved m
            RETURNING 1
        )
        SELECT count(*) FROM kept
    """
    return int(conn.execute(sql).fetchone()[0])


def save_results(conn: Any, results: list[CheckResult], transform_run_id: int | None) -> None:
    for r in results:
        conn.execute(
            "INSERT INTO meta.dq_result (transform_run_id, rule_code, check_type, phase, target_table, "
            "severity, status, failed_rows, sample, message) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
            (
                transform_run_id,
                r.rule.code,
                r.rule.check_type,
                r.rule.phase,
                r.rule.table,
                r.rule.severity,
                r.status,
                r.failed_rows,
                json.dumps(r.sample, ensure_ascii=False, default=str),
                r.message,
            ),
        )


def blocking(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.is_blocking]


def summary(results: list[CheckResult]) -> str:
    """Короткая строка для лога и вывода команды."""
    passed = sum(r.status == "passed" for r in results)
    warnings = sum(r.status == "failed" and r.rule.severity == "warning" for r in results)
    quarantined = sum(r.quarantined for r in results)
    critical = sum(r.status == "failed" and r.rule.severity == "critical" for r in results)
    errors = sum(r.status == "error" for r in results)
    return (
        f"проверок {len(results)}: пройдено {passed}, предупреждений {warnings}, "
        f"в карантин строк {quarantined}, критичных {critical}, ошибок {errors}"
    )


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text
