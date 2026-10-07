-- target: staging.openmeteo_daily
-- Разбор ответов Open-Meteo (история) из raw: одна строка на дату.
-- Одна и та же дата бывает в нескольких ответах (последние дни перезапрашиваются,
-- и источник их уточняет) — берём значение из самого свежего ответа.

TRUNCATE staging.openmeteo_daily;

INSERT INTO staging.openmeteo_daily (
    obs_date, weather_code, temp_mean, temp_min, temp_max, apparent_temp_mean, precip_mm, rain_mm,
    snowfall_cm, precip_hours, wind_speed_ms, wind_gust_ms, wind_dir_deg, radiation_mj_m2,
    humidity_pct, pressure_hpa, cloud_cover_pct, dew_point_c, snow_depth_cm, response_id, fetched_at
)
SELECT DISTINCT ON (x.day::date)
    x.day::date,
    meta.try_numeric(x.weather_code)::smallint,
    meta.try_numeric(x.temp_mean),
    meta.try_numeric(x.temp_min),
    meta.try_numeric(x.temp_max),
    meta.try_numeric(x.apparent_temp),
    meta.try_numeric(x.precip),
    meta.try_numeric(x.rain),
    meta.try_numeric(x.snowfall),
    meta.try_numeric(x.precip_hours),
    round(meta.try_numeric(x.wind_speed) / 3.6, 1),  -- км/ч -> м/с
    round(meta.try_numeric(x.wind_gusts) / 3.6, 1),
    meta.try_numeric(x.wind_dir)::smallint,
    meta.try_numeric(x.radiation),
    meta.try_numeric(x.humidity)::smallint,
    meta.try_numeric(x.pressure),
    meta.try_numeric(x.cloud_cover)::smallint,
    meta.try_numeric(x.dew_point),
    round(meta.try_numeric(x.snow_depth) * 100, 1),  -- м -> см
    r.response_id,
    r.fetched_at
FROM raw.openmeteo_archive r
CROSS JOIN LATERAL ROWS FROM (
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
) AS x (day, weather_code, temp_mean, temp_min, temp_max, apparent_temp, precip, rain, snowfall,
        precip_hours, wind_speed, wind_gusts, wind_dir, radiation, humidity, pressure, cloud_cover,
        dew_point, snow_depth)
WHERE r.is_valid
ORDER BY x.day::date, r.fetched_at DESC, r.response_id DESC;
