-- V007. История изменений справочника станций и реестр схем источников.

-- ----------------------------------------------------------------------------
-- 1. Справочник станций с историей (SCD2).
-- Если в настройках поменяются название, координаты или высота станции,
-- старая запись закрывается (valid_to, is_current = false) и добавляется новая.
-- Факты ссылаются на ту версию станции, которая была текущей при их создании.
-- ----------------------------------------------------------------------------

ALTER TABLE core.fact_weather_daily DROP CONSTRAINT fact_weather_daily_station_id_fkey;
ALTER TABLE core.dim_station DROP CONSTRAINT dim_station_pkey;

ALTER TABLE core.dim_station ADD COLUMN station_key serial PRIMARY KEY;
ALTER TABLE core.dim_station ADD COLUMN valid_from timestamptz;
ALTER TABLE core.dim_station ADD COLUMN valid_to timestamptz;
ALTER TABLE core.dim_station ADD COLUMN is_current boolean NOT NULL DEFAULT true;
UPDATE core.dim_station SET valid_from = updated_at;
ALTER TABLE core.dim_station ALTER COLUMN valid_from SET NOT NULL;
ALTER TABLE core.dim_station ALTER COLUMN valid_from SET DEFAULT now();
ALTER TABLE core.dim_station DROP COLUMN updated_at;
ALTER TABLE core.dim_station ADD CONSTRAINT ck_dim_station_current CHECK (is_current = (valid_to IS NULL));
CREATE UNIQUE INDEX ux_dim_station_current ON core.dim_station (station_id) WHERE is_current;

COMMENT ON TABLE core.dim_station IS
    'Справочник метеостанций с историей изменений: одна строка — одна версия описания станции';
COMMENT ON COLUMN core.dim_station.station_key IS 'Номер версии станции (на него ссылаются факты)';
COMMENT ON COLUMN core.dim_station.station_id IS 'Код станции (индекс ВМО), одинаковый у всех версий';

ALTER TABLE core.fact_weather_daily ADD COLUMN station_key integer REFERENCES core.dim_station (station_key);
UPDATE core.fact_weather_daily f
SET station_key = s.station_key
FROM core.dim_station s
WHERE s.station_id = f.station_id AND s.is_current;
ALTER TABLE core.fact_weather_daily ALTER COLUMN station_key SET NOT NULL;
COMMENT ON COLUMN core.fact_weather_daily.station_key IS 'Версия станции, текущая на момент создания записи';

-- ----------------------------------------------------------------------------
-- 2. Реестр схем источников.
-- Для каждого источника хранятся все встречавшиеся схемы ответа: для Open-Meteo —
-- переменные и их единицы измерения, для Meteostat — колонки CSV-файла.
-- Новая схема регистрируется при загрузке и попадает в журнал загрузок.
-- ----------------------------------------------------------------------------

CREATE TABLE meta.source_schema (
    schema_id      bigserial   PRIMARY KEY,
    source_code    text        NOT NULL REFERENCES meta.source (source_code),
    schema_hash    char(32)    NOT NULL,
    fields         jsonb       NOT NULL,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    first_load_id  bigint      REFERENCES meta.load_log (load_id),
    last_load_id   bigint      REFERENCES meta.load_log (load_id),
    times_seen     integer     NOT NULL DEFAULT 1 CHECK (times_seen > 0),
    UNIQUE (source_code, schema_hash)
);
COMMENT ON TABLE meta.source_schema IS
    'Реестр схем источников: какие наборы полей приходили от источника, когда впервые и последний раз';
COMMENT ON COLUMN meta.source_schema.fields IS
    'Open-Meteo: {"переменная": "единица"}; Meteostat: ["колонка", ...] в порядке файла';
COMMENT ON COLUMN meta.source_schema.schema_hash IS 'MD5 от fields — по нему схемы сравниваются';

-- Заполняем реестр по уже загруженным данным
INSERT INTO meta.source_schema (source_code, schema_hash, fields, first_seen_at, last_seen_at,
                                first_load_id, last_load_id, times_seen)
SELECT source_code, md5(fields::text), fields, min(fetched_at), max(fetched_at),
       min(load_id), max(load_id), count(*)
FROM (
    SELECT 'openmeteo_archive' AS source_code, r.fetched_at, r.load_id,
           coalesce((SELECT jsonb_object_agg(k, r.payload -> 'daily_units' -> k)
                     FROM jsonb_object_keys(r.payload -> 'daily') AS k), '{}'::jsonb) AS fields
    FROM raw.openmeteo_archive r
    WHERE jsonb_typeof(r.payload -> 'daily') = 'object'
    UNION ALL
    SELECT 'openmeteo_forecast', r.fetched_at, r.load_id,
           coalesce((SELECT jsonb_object_agg(k, r.payload -> 'daily_units' -> k)
                     FROM jsonb_object_keys(r.payload -> 'daily') AS k), '{}'::jsonb)
    FROM raw.openmeteo_forecast r
    WHERE jsonb_typeof(r.payload -> 'daily') = 'object'
    UNION ALL
    SELECT 'meteostat_daily', f.fetched_at, f.load_id, to_jsonb(f.columns)
    FROM raw.meteostat_file f
    WHERE cardinality(f.columns) > 0
) x
GROUP BY source_code, fields;
