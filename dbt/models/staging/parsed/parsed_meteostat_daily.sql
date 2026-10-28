-- Разбор годовых CSV-файлов Meteostat из raw. Колонки ищем по заголовку
-- (raw.meteostat_file.columns), потому что их набор в разные годы разный.
{{ config(materialized='ephemeral') }}

with files as (
    -- последняя версия файла за каждый год
    select distinct on (station_id, year) file_id, station_id, year, columns, content, fetched_at
    from {{ source('raw', 'meteostat_file') }}
    where is_valid
    order by station_id, year, fetched_at desc, file_id desc
),

lines as (
    select f.file_id, f.columns, f.fetched_at, string_to_array(l.line, ',') as v
    from files f
    cross join lateral string_to_table(f.content, E'\n') with ordinality as l (line, n)
    where l.n > 1 and l.line <> ''
),

parsed as (
    select
        make_date(
            v[array_position(columns, 'year')]::int,
            v[array_position(columns, 'month')]::int,
            v[array_position(columns, 'day')]::int
        ) as obs_date,
        meta.try_numeric(v[array_position(columns, 'temp')]) as temp_mean,
        meta.try_numeric(v[array_position(columns, 'tmin')]) as temp_min,
        meta.try_numeric(v[array_position(columns, 'tmax')]) as temp_max,
        meta.try_numeric(v[array_position(columns, 'prcp')]) as precip_mm,
        meta.try_numeric(v[array_position(columns, 'snwd')]) as snow_depth_cm,
        round(meta.try_numeric(v[array_position(columns, 'wspd')]) / 3.6, 1) as wind_speed_ms,  -- км/ч -> м/с
        meta.try_numeric(v[array_position(columns, 'pres')]) as pressure_hpa,
        meta.try_numeric(v[array_position(columns, 'rhum')])::smallint as humidity_pct,
        meta.try_numeric(v[array_position(columns, 'cldc')])::smallint as cloud_cover_okta,
        nullif(v[array_position(columns, 'temp_source')], '') as temp_source,
        nullif(v[array_position(columns, 'prcp_source')], '') as precip_source,
        file_id,
        fetched_at
    from lines
)

select distinct on (obs_date)
    obs_date, temp_mean, temp_min, temp_max, precip_mm, snow_depth_cm, wind_speed_ms,
    pressure_hpa, humidity_pct, cloud_cover_okta, temp_source, precip_source,
    coalesce(temp_source = 'dwd_mosmix', false) as is_model_temp,
    file_id,
    fetched_at
from parsed
-- даты после дня скачивания — это прогноз модели, а не наблюдения
where obs_date < (fetched_at at time zone '{{ var("timezone") }}')::date
order by obs_date, fetched_at desc
