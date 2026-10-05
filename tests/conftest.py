"""Общие фикстуры. Интеграционные тесты получают отдельную временную базу данных.

Если драйвер psycopg не установлен или PostgreSQL недоступен, такие тесты
пропускаются. В Docker база доступна: docker compose run --rm app pytest
"""

import uuid
from dataclasses import replace

import pytest


@pytest.fixture
def fresh_db():
    """Новая пустая база с применёнными миграциями. Удаляется после теста."""
    psycopg = pytest.importorskip("psycopg", reason="драйвер psycopg не установлен")
    from weather import db, migrations
    from weather.config import load_settings

    settings = load_settings()
    try:
        admin = db.connect(settings.db, autocommit=True, timeout_s=3)
    except psycopg.OperationalError:
        pytest.skip(f"PostgreSQL {settings.db.describe()} недоступен")

    name = f"weather_test_{uuid.uuid4().hex[:10]}"
    admin.execute(f"CREATE DATABASE {name}")
    test_settings = replace(settings, db=replace(settings.db, name=name))
    conn = db.connect(test_settings.db, autocommit=True)
    try:
        migrations.migrate(conn)
        yield test_settings, conn
    finally:
        conn.close()
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()
