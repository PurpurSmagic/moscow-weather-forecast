-- Разбор сохранённых прогнозов Open-Meteo: одна строка на (дата выпуска, дата прогноза).
{{ config(indexes=[{'columns': ['issue_date', 'target_date'], 'unique': true}]) }}

select
    r.issue_date,
    x.day::date                                       as target_date,
    (x.day::date - r.issue_date)::smallint            as lead_days,
    meta.try_numeric(x.weather_code)::smallint        as weather_code,
    meta.try_numeric(x.temp_mean)                     as temp_mean,
    meta.try_numeric(x.temp_min)                      as temp_min,
    meta.try_numeric(x.temp_max)                      as temp_max,
    meta.try_numeric(x.precip)                        as precip_mm,
    meta.try_numeric(x.precip_prob)::smallint         as precip_prob_pct,
    round(meta.try_numeric(x.wind_speed) / 3.6, 1)    as wind_speed_ms,  -- км/ч -> м/с
    round(meta.try_numeric(x.wind_gusts) / 3.6, 1)    as wind_gust_ms,
    meta.try_numeric(x.humidity)::smallint            as humidity_pct,
    meta.try_numeric(x.pressure)                      as pressure_hpa,
    meta.try_numeric(x.cloud_cover)::smallint         as cloud_cover_pct,
    r.response_id
from {{ source('raw', 'openmeteo_forecast') }} r
cross join lateral rows from (
    jsonb_array_elements_text(r.payload -> 'daily' -> 'time'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'weather_code'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'temperature_2m_mean'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'temperature_2m_min'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'temperature_2m_max'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'precipitation_sum'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'precipitation_probability_max'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'wind_speed_10m_max'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'wind_gusts_10m_max'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'relative_humidity_2m_mean'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'pressure_msl_mean'),
    jsonb_array_elements_text(r.payload -> 'daily' -> 'cloud_cover_mean')
) as x (day, weather_code, temp_mean, temp_min, temp_max, precip, precip_prob, wind_speed, wind_gusts,
        humidity, pressure, cloud_cover)
where r.is_valid
