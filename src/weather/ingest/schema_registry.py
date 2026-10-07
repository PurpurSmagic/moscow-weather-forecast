"""Реестр схем источников (meta.source_schema).

При каждой загрузке схема полученных данных записывается в реестр:
* Open-Meteo — переменные из блока daily и их единицы измерения;
* Meteostat — колонки CSV-файла в порядке следования.

Если схема новая, а раньше у источника были другие, возвращается описание отличий —
оно попадает в журнал загрузок.
"""

from __future__ import annotations

import json
from typing import Any


def openmeteo_fields(payload: Any) -> dict[str, Any]:
    """Схема ответа Open-Meteo: {переменная: единица}."""
    if not isinstance(payload, dict) or not isinstance(payload.get("daily"), dict):
        return {}
    units = payload.get("daily_units")
    units = units if isinstance(units, dict) else {}
    return {key: units.get(key) for key in payload["daily"]}


def register_schema(conn: Any, source_code: str, fields: dict | list, load_id: int) -> str | None:
    """Записывает схему в реестр. Возвращает описание изменений, если схема встретилась впервые
    и у источника до этого была другая схема; иначе None.

    Пустая схема (ответ без данных, например сообщение об ошибке) не регистрируется."""
    if not fields:
        return None
    text = json.dumps(fields, ensure_ascii=False)
    row = conn.execute(
        "INSERT INTO meta.source_schema (source_code, schema_hash, fields, first_load_id, last_load_id) "
        "VALUES (%s, md5(%s::jsonb::text), %s::jsonb, %s, %s) "
        "ON CONFLICT (source_code, schema_hash) DO UPDATE SET last_seen_at = now(), "
        "  last_load_id = EXCLUDED.last_load_id, times_seen = meta.source_schema.times_seen + 1 "
        "RETURNING xmax = 0, schema_id",
        (source_code, text, text, load_id, load_id),
    ).fetchone()
    is_new, schema_id = bool(row[0]), row[1]
    if not is_new:
        return None
    previous = conn.execute(
        "SELECT fields FROM meta.source_schema WHERE source_code = %s AND schema_id <> %s "
        "ORDER BY last_seen_at DESC, schema_id DESC LIMIT 1",
        (source_code, schema_id),
    ).fetchone()
    if previous is None:
        return None  # первая схема источника — сравнивать не с чем
    old = json.loads(previous[0]) if isinstance(previous[0], str) else previous[0]
    return describe_change(old, fields)


def describe_change(old: dict | list, new: dict | list) -> str:
    old_names, new_names = list(old), list(new)
    parts = []
    added = [n for n in new_names if n not in old_names]
    removed = [n for n in old_names if n not in new_names]
    if added:
        parts.append("добавлены " + ", ".join(added))
    if removed:
        parts.append("удалены " + ", ".join(removed))
    if isinstance(old, dict) and isinstance(new, dict):
        units = [f"{n} ({old[n]} → {new[n]})" for n in new_names if n in old and old[n] != new[n]]
        if units:
            parts.append("изменились единицы: " + ", ".join(units))
    if not parts:
        parts.append("изменился порядок полей")
    return "новая схема источника: " + "; ".join(parts)
