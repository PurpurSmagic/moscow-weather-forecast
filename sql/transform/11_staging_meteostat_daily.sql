-- target: staging.meteostat_daily
-- Разбор годовых CSV-файлов Meteostat из raw. Колонки ищем по заголовку
-- (raw.meteostat_file.columns), потому что их набор в разные годы разный.

TRUNCATE staging.meteostat_daily;

WITH files AS (
    -- последняя версия файла за каждый год
    SELECT DISTINCT ON (station_id, year) file_id, station_id, year, columns, content, fetched_at
    FROM raw.meteostat_file
    WHERE is_valid
    ORDER BY station_id, year, fetched_at DESC, file_id DESC
),
rows AS (
    SELECT f.file_id, f.columns, f.fetched_at, string_to_array(l.line, ',') AS v
    FROM files f
    CROSS JOIN LATERAL string_to_table(f.content, E'\n') WITH ORDINALITY AS l (line, n)
    WHERE l.n > 1 AND l.line <> ''
),
parsed AS (
    SELECT
        make_date(
            v[array_position(columns, 'year')]::int,
            v[array_position(columns, 'month')]::int,
            v[array_position(columns, 'day')]::int
        ) AS obs_date,
        meta.try_numeric(v[array_position(columns, 'temp')]) AS temp_mean,
        meta.try_numeric(v[array_position(columns, 'tmin')]) AS temp_min,
        meta.try_numeric(v[array_position(columns, 'tmax')]) AS temp_max,
        meta.try_numeric(v[array_position(columns, 'prcp')]) AS precip_mm,
        meta.try_numeric(v[array_position(columns, 'snwd')]) AS snow_depth_cm,
        round(meta.try_numeric(v[array_position(columns, 'wspd')]) / 3.6, 1) AS wind_speed_ms,  -- км/ч -> м/с
        meta.try_numeric(v[array_position(columns, 'pres')]) AS pressure_hpa,
        meta.try_numeric(v[array_position(columns, 'rhum')])::smallint AS humidity_pct,
        meta.try_numeric(v[array_position(columns, 'cldc')])::smallint AS cloud_cover_okta,
        nullif(v[array_position(columns, 'temp_source')], '') AS temp_source,
        nullif(v[array_position(columns, 'prcp_source')], '') AS precip_source,
        file_id,
        fetched_at
    FROM rows
)
INSERT INTO staging.meteostat_daily (
    obs_date, temp_mean, temp_min, temp_max, precip_mm, snow_depth_cm, wind_speed_ms,
    pressure_hpa, humidity_pct, cloud_cover_okta, temp_source, precip_source, is_model_temp,
    file_id, fetched_at
)
SELECT DISTINCT ON (obs_date)
    obs_date, temp_mean, temp_min, temp_max, precip_mm, snow_depth_cm, wind_speed_ms,
    pressure_hpa, humidity_pct, cloud_cover_okta, temp_source, precip_source,
    coalesce(temp_source = 'dwd_mosmix', false),
    file_id, fetched_at
FROM parsed
-- даты после дня скачивания — это прогноз модели, а не наблюдения
WHERE obs_date < (fetched_at AT TIME ZONE current_setting('weather.timezone'))::date
ORDER BY obs_date, fetched_at DESC;
