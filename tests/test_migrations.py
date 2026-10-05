from contextlib import contextmanager
from pathlib import Path

import pytest

from weather import migrations
from weather.migrations import AppliedMigration, MigrationError, checksum, discover, plan


def make_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, sql in files.items():
        (tmp_path / name).write_text(sql, encoding="utf-8")
    return tmp_path


def applied(migration) -> AppliedMigration:
    return AppliedMigration(migration.version, migration.name, migration.checksum)


# --- поиск файлов ---------------------------------------------------------------


def test_project_migrations_are_valid_and_sequential():
    found = discover()
    assert found, "в sql/migrations должна быть хотя бы одна миграция"
    assert [m.version for m in found] == list(range(1, len(found) + 1))


def test_discover_sorts_by_version(tmp_path):
    d = make_dir(tmp_path, {"V002__b.sql": "select 2;", "V001__a.sql": "select 1;"})
    assert [m.label for m in discover(d)] == ["V001__a", "V002__b"]


@pytest.mark.parametrize("bad_name", ["001__a.sql", "V1__a.sql", "V001_a.sql", "V001__Upper.sql"])
def test_discover_rejects_bad_names(tmp_path, bad_name):
    d = make_dir(tmp_path, {bad_name: "select 1;"})
    with pytest.raises(MigrationError, match="шаблону"):
        discover(d)


def test_discover_rejects_duplicate_versions(tmp_path):
    d = make_dir(tmp_path, {"V001__a.sql": "select 1;", "V001__b.sql": "select 2;"})
    with pytest.raises(MigrationError, match="Две миграции"):
        discover(d)


def test_checksum_ignores_windows_line_endings():
    assert checksum("select 1;\nselect 2;\n") == checksum("select 1;\r\nselect 2;\r\n")


# --- планирование ---------------------------------------------------------------


def test_plan_returns_only_new(tmp_path):
    m1, m2 = discover(make_dir(tmp_path, {"V001__a.sql": "select 1;", "V002__b.sql": "select 2;"}))
    assert plan([m1, m2], []) == [m1, m2]
    assert plan([m1, m2], [applied(m1)]) == [m2]
    assert plan([m1, m2], [applied(m1), applied(m2)]) == []


def test_plan_detects_changed_migration(tmp_path):
    (m1,) = discover(make_dir(tmp_path, {"V001__a.sql": "select 1;"}))
    with pytest.raises(MigrationError, match="изменён после применения"):
        plan([m1], [AppliedMigration(1, "a", "0" * 64)])


def test_plan_detects_missing_file(tmp_path):
    (m1,) = discover(make_dir(tmp_path, {"V001__a.sql": "select 1;"}))
    with pytest.raises(MigrationError, match="файла нет"):
        plan([m1], [applied(m1), AppliedMigration(2, "b", "0" * 64)])


def test_plan_rejects_out_of_order(tmp_path):
    m1, m2 = discover(make_dir(tmp_path, {"V001__a.sql": "select 1;", "V002__b.sql": "select 2;"}))
    with pytest.raises(MigrationError, match="следующий номер"):
        plan([m1, m2], [applied(m2)])


# --- применение (на подставном подключении) -------------------------------------


class FakeConnection:
    """Имитирует нужную часть psycopg.Connection и запоминает выполненный SQL."""

    def __init__(self):
        self.executed: list[str] = []
        self.applied: list[tuple] = []
        self.transactions = 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if sql.startswith("INSERT INTO meta.schema_migrations"):
            version, name, chk, _ = params
            self.applied.append((version, name, chk))
        rows = self.applied if "FROM meta.schema_migrations" in sql else []
        return FakeCursor(rows)


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_migrate_applies_each_file_once(tmp_path):
    d = make_dir(tmp_path, {"V001__a.sql": "create schema a;", "V002__b.sql": "create schema b;"})
    conn = FakeConnection()

    first = migrations.migrate(conn, d)
    assert [m.label for m in first] == ["V001__a", "V002__b"]
    assert "create schema a;" in conn.executed and "create schema b;" in conn.executed
    assert any("pg_advisory_lock" in sql for sql in conn.executed)
    assert any("pg_advisory_unlock" in sql for sql in conn.executed)

    executed_before = len(conn.executed)
    second = migrations.migrate(conn, d)
    assert second == []
    assert "create schema a;" not in conn.executed[executed_before:]


def test_migrate_releases_lock_on_error(tmp_path):
    d = make_dir(tmp_path, {"V001__a.sql": "select 1;"})
    conn = FakeConnection()
    conn.applied.append((1, "a", "0" * 64))  # в базе другая контрольная сумма
    with pytest.raises(MigrationError):
        migrations.migrate(conn, d)
    assert "pg_advisory_unlock" in conn.executed[-1]
