"""Проверки качества данных.

Проверки — это тесты dbt:
* dbt/tests/*.sql — запросы, которые возвращают строки-нарушения (пустой результат —
  проверка пройдена), их описание и уровень — в dbt/tests/tests.yml;
* проверки ключей и ссылок (unique, not_null, relationships, accepted_values) —
  рядом с моделями в dbt/models/*/*.yml и dbt/snapshots/snapshots.yml.

Уровни проверок:
* warning — нарушения записываются, обработка продолжается;
* quarantine — плохие строки убраны в staging.quarantine (модель dbt/models/staging/quarantine.sql),
  дальше они не идут;
* critical (severity: error) — следующие шаги обработки не выполняются, витрины остаются как были.

Тесты выполняются внутри `dbt build` (transform.py) или отдельно командой dq (`dbt test`).
Этот модуль переводит результаты тестов в записи meta.dq_result.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

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

# Тип проверки для стандартных тестов dbt, если он не указан в meta.check_type
GENERIC_CHECK_TYPES = {
    "unique": "уникальность",
    "not_null": "полнота",
    "relationships": "ссылочная целостность",
    "accepted_values": "допустимость значений",
}
# Статус теста dbt -> статус проверки
STATUSES = {"pass": "passed", "fail": "failed", "warn": "failed", "error": "error", "skipped": "skipped"}


@dataclass(frozen=True)
class Rule:
    """Описание проверки (теста dbt)."""

    code: str
    check_type: str
    phase: str
    severity: str
    table: str
    description: str


@dataclass
class CheckResult:
    rule: Rule
    status: str  # passed | failed | error | skipped
    failed_rows: int = 0
    sample: list[Any] = field(default_factory=list)
    message: str | None = None

    @property
    def quarantined(self) -> int:
        """Сколько строк по этой проверке лежит в карантине."""
        return self.failed_rows if self.rule.severity == "quarantine" and self.status == "failed" else 0

    @property
    def is_blocking(self) -> bool:
        """Критичная проверка не прошла или сама проверка не выполнилась."""
        return self.status == "error" or (self.rule.severity == "critical" and self.status == "failed")


# --- описание проверок из проекта dbt ---------------------------------------------


def describe_rule(node: Any, manifest: Any) -> Rule:
    """Описание проверки по узлу теста dbt."""
    meta = dict(node.config.meta or {})
    generic = getattr(node, "test_metadata", None)
    check_type = meta.get("check_type") or GENERIC_CHECK_TYPES.get(generic.name if generic else "", "")
    if meta.get("level") == "quarantine":
        severity = "quarantine"
    elif str(node.config.severity).lower() == "error":
        severity = "critical"
    else:
        severity = "warning"
    tested = _tested_nodes(node, manifest)
    # Этап: по каталогу (tests/staging, tests/final, models/staging), иначе — по слою проверяемой таблицы
    if "final" in node.fqn:
        phase = "final"
    elif "staging" in node.fqn:
        phase = "staging"
    else:
        core = any(str(n.resource_type) in ("model", "snapshot") and n.schema != "staging" for n in tested)
        phase = "final" if core else "staging"
    table = meta.get("table") or (_relation(tested[0]) if tested else "")
    return Rule(
        code=node.name,
        check_type=check_type,
        phase=phase,
        severity=severity,
        table=table,
        description=" ".join((node.description or "").split()),
    )


def project_rules(manifest: Any) -> list[Rule]:
    """Все проверки проекта (для проверки описаний и для вывода)."""
    tests = [n for n in manifest.nodes.values() if n.resource_type == "test"]
    return sorted((describe_rule(n, manifest) for n in tests), key=lambda r: (r.phase != "staging", r.code))


def _tested_nodes(node: Any, manifest: Any) -> list[Any]:
    """Таблицы, которые читает тест: сначала та, к которой тест привязан."""
    ids = list(node.depends_on.nodes)
    attached = getattr(node, "attached_node", None)
    if attached in ids:
        ids.remove(attached)
        ids.insert(0, attached)
    found = []
    for uid in ids:
        target = manifest.nodes.get(uid) or manifest.sources.get(uid)
        if target is not None:
            found.append(target)
    return found


def _relation(node: Any) -> str:
    name = getattr(node, "identifier", None) if node.resource_type == "source" else node.alias
    return f"{node.schema}.{name}"


# --- результаты -------------------------------------------------------------------


def check_result(conn: Any, run_result: Any, manifest: Any) -> CheckResult:
    """Результат теста dbt -> результат проверки (с примерами строк-нарушений)."""
    node = run_result.node
    rule = describe_rule(node, manifest)
    status = STATUSES.get(str(getattr(run_result.status, "value", run_result.status)), "error")
    failed_rows = int(run_result.failures or 0) if status == "failed" else 0
    message = None
    sample: list[Any] = []
    if status == "failed":
        sample = _sample(conn, node)
        if rule.severity == "quarantine":
            message = f"в карантин: {failed_rows} стр."
    elif status == "error":
        text = " ".join(str(run_result.message or "").split())
        message = text[:300] if text else "ошибка выполнения"
        log.warning("Проверка %s не выполнилась: %s", rule.code, message)
    return CheckResult(rule, status, failed_rows, sample, message)


def _sample(conn: Any, node: Any) -> list[Any]:
    """До SAMPLE_SIZE строк-нарушений: выполняем скомпилированный запрос теста."""
    sql = (getattr(node, "compiled_code", None) or "").strip().rstrip(";")
    if not sql:
        return []
    try:
        with conn.transaction():
            rows = conn.execute(f"SELECT to_jsonb(v)::text FROM ({sql}) v LIMIT {SAMPLE_SIZE}").fetchall()
    except Exception as exc:  # пример — не главное, из-за него проверка не должна падать
        log.debug("Не удалось получить примеры для %s: %s", node.name, exc)
        return []
    return [_json(row[0]) for row in rows]


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
    skipped = sum(r.status == "skipped" for r in results)
    text = (
        f"проверок {len(results)}: пройдено {passed}, предупреждений {warnings}, "
        f"в карантин строк {quarantined}, критичных {critical}, ошибок {errors}"
    )
    return text + (f", не выполнялись {skipped}" if skipped else "")


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text
