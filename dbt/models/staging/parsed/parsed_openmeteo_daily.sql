-- Разбор ответов Open-Meteo (история) из raw: одна строка на дату.
-- Одна и та же дата бывает в нескольких ответах (последние дни перезапрашиваются,
-- и источник их уточняет) — берём значение из самого свежего ответа.
{{ config(materialized='ephemeral') }}

select distinct on (x.day::date)
    x.day::date                                           as obs_date,
    meta.try_numeric(x.weather_code)::smallint            as weather_code,
    meta.try_numeric(x.temp_mean)                         as temp_mean,
    meta.try_numeric(x.temp_min)                          as temp_min,
    meta.try_numeric(x.temp_max)                          as temp_max,
    meta.try_numeric(x.apparent_temp)                     as apparent_temp_mean,
    meta.try_numeric(x.precip)                            as precip_mm,
    meta.try_numeric(x.rain)                              as rain_mm,
    meta.try_numeric(x.snowfall)                          as snowfall_cm,
    meta.try_numeric(x.precip_hours)                      as precip_hours,
    round(meta.try_numeric(x.wind_speed) / 3.6, 1)        as wind_speed_ms,  -- км/ч -> м/с
    round(meta.try_numeric(x.wind_gusts) / 3.6, 1)        as wind_gust_ms,
    meta.try_numeric(x.wind_dir)::smallint                as wind_dir_deg,
    meta.try_numeric(x.radiation)                         as radiation_mj_m2,
    meta.try_numeric(x.humidity)::smallint                as humidity_pct,
    meta.try_numeric(x.pressure)                          as pressure_hpa,
    meta.try_numeric(x.cloud_cover)::smallint             as cloud_cover_pct,
    meta.try_numeric(x.dew_point)                         as dew_point_c,
    round(meta.try_numeric(x.snow_depth) * 100, 1)        as snow_depth_cm,  -- м -> см
    r.response_id,
    r.fetched_at
from {{ source('raw', 'openmeteo_archive') }} r
cross join lateral rows from (
    jsonb_array_elements_text(r.payload -> 'daily' -> 'time'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'weather_code'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'temperature_2m_mean'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'temperature_2m_min'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'temperature_2m_max'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'apparent_temperature_mean'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'precipitation_sum'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'rain_sum'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'snowfall_sum'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'precipitation_hours'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'wind_speed_10m_max'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'wind_gusts_10m_max'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'wind_direction_10m_dominant'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'shortwave_radiation_sum'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'relative_humidity_2m_mean'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'pressure_msl_mean'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'cloud_cover_mean'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'dew_point_2m_mean'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'snow_depth_max')
) as x (day, weather_code, temp_mean, temp_min, temp_max, apparent_temp, precip, rain, snowfall,
        precip_hours, wind_speed, wind_gusts, wind_dir, radiation, humidity, pressure, cloud_cover,
        dew_point, snow_depth)
where r.is_valid
order by x.day::date, r.fetched_at desc, r.response_id desc
