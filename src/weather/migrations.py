"""Миграции схемы базы данных.

Каждое изменение структуры базы — отдельный файл ``sql/migrations/VNNN__описание.sql``.
Применённые миграции записываются в ``meta.schema_migrations`` вместе с контрольной
суммой файла. Поэтому:

* повторный запуск ничего не меняет — применяются только новые файлы (идемпотентность);
* правка уже применённого файла обнаруживается и останавливает запуск — изменения
  схемы вносятся только новыми миграциями, и история изменений видна в репозитории.

Модуль не импортирует psycopg: функции работают с любым объектом подключения,
у которого есть ``execute()`` и ``transaction()``. Это позволяет тестировать логику
без живой базы.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weather.config import PROJECT_ROOT

log = logging.getLogger(__name__)

MIGRATIONS_DIR = PROJECT_ROOT / "sql" / "migrations"
FILENAME_RE = re.compile(r"^V(?P<version>\d{3,})__(?P<name>[a-z0-9_]+)\.sql$")
# Произвольная константа для pg_advisory_lock: не даёт двум процессам
# применять миграции одновременно.
ADVISORY_LOCK_KEY = 27612_0001

BOOTSTRAP_SQL = """
CREATE SCHEMA IF NOT EXISTS meta;
CREATE TABLE IF NOT EXISTS meta.schema_migrations (
    version      integer     PRIMARY KEY,
    name         text        NOT NULL,
    checksum     char(64)    NOT NULL,
    applied_at   timestamptz NOT NULL DEFAULT now(),
    duration_ms  integer     NOT NULL CHECK (duration_ms >= 0)
);
COMMENT ON TABLE meta.schema_migrations IS
    'Применённые миграции схемы БД: версия, имя файла, контрольная сумма SHA-256, время применения';
"""


class MigrationError(RuntimeError):
    """Миграции нельзя применить: нарушен порядок или изменён применённый файл."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str

    @property
    def label(self) -> str:
        return f"V{self.version:03d}__{self.name}"


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str


def checksum(sql: str) -> str:
    """SHA-256 текста миграции. Окончания строк приводятся к LF, чтобы
    клонирование на Windows (CRLF) не меняло контрольную сумму."""
    normalized = sql.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Находит файлы миграций и сортирует их по версии."""
    if not directory.is_dir():
        raise MigrationError(f"Нет каталога миграций: {directory}")
    migrations: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"Имя файла миграции не соответствует шаблону VNNN__описание.sql: {path.name}"
            )
        version = int(match["version"])
        if version in migrations:
            raise MigrationError(
                f"Две миграции с версией {version}: {migrations[version].path.name} и {path.name}"
            )
        sql = path.read_text(encoding="utf-8")
        migrations[version] = Migration(version, match["name"], path, sql, checksum(sql))
    return [migrations[v] for v in sorted(migrations)]


def plan(available: list[Migration], applied: list[AppliedMigration]) -> list[Migration]:
    """Проверяет согласованность базы и файлов и возвращает миграции к применению."""
    by_version = {m.version: m for m in available}
    for done in applied:
        migration = by_version.get(done.version)
        if migration is None:
            raise MigrationError(
                f"Миграция V{done.version:03d}__{done.name} применена в базе, но её файла нет"
            )
        if migration.checksum != done.checksum.strip():
            raise MigrationError(
                f"Файл {migration.path.name} изменён после применения. "
                "Применённые миграции не правят — изменения вносятся новой миграцией."
            )
    applied_versions = {done.version for done in applied}
    pending = [m for m in available if m.version not in applied_versions]
    if pending and applied_versions and pending[0].version < max(applied_versions):
        raise MigrationError(
            f"Миграция {pending[0].label} старше уже применённой V{max(applied_versions):03d}: "
            "новые миграции должны получать следующий номер"
        )
    return pending


def read_applied(conn: Any) -> list[AppliedMigration]:
    rows = conn.execute(
        "SELECT version, name, checksum FROM meta.schema_migrations ORDER BY version"
    ).fetchall()
    return [AppliedMigration(int(v), str(n), str(c)) for v, n, c in rows]


def migrate(conn: Any, directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Применяет новые миграции. Каждая — в отдельной транзакции.

    ``conn`` должен быть в режиме autocommit: транзакции открываются явно.
    Возвращает список применённых в этом запуске миграций.
    """
    available = discover(directory)
    with conn.transaction():
        conn.execute(BOOTSTRAP_SQL)

    conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    try:
        pending = plan(available, read_applied(conn))
        if not pending:
            log.info("Схема БД актуальна: новых миграций нет")
            return []
        for migration in pending:
            started = time.perf_counter()
            with conn.transaction():
                conn.execute(migration.sql)
                duration_ms = round((time.perf_counter() - started) * 1000)
                conn.execute(
                    "INSERT INTO meta.schema_migrations (version, name, checksum, duration_ms) "
                    "VALUES (%s, %s, %s, %s)",
                    (migration.version, migration.name, migration.checksum, duration_ms),
                )
            log.info("Применена миграция %s (%d мс)", migration.label, duration_ms)
        return pending
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


def status(conn: Any, directory: Path = MIGRATIONS_DIR) -> tuple[list[AppliedMigration], list[Migration]]:
    """Возвращает (применённые, ожидающие) без изменения базы."""
    available = discover(directory)
    exists = conn.execute("SELECT to_regclass('meta.schema_migrations') IS NOT NULL").fetchone()[0]
    applied = read_applied(conn) if exists else []
    return applied, plan(available, applied)
