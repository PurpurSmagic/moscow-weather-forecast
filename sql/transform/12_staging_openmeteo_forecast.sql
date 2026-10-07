-- target: staging.openmeteo_forecast
-- Разбор сохранённых прогнозов Open-Meteo: одна строка на (дата выпуска, дата прогноза).

TRUNCATE staging.openmeteo_forecast;

INSERT INTO staging.openmeteo_forecast (
    issue_date, target_date, lead_days, weather_code, temp_mean, temp_min, temp_max,
    precip_mm, precip_prob_pct, wind_speed_ms, wind_gust_ms, humidity_pct, pressure_hpa,
    cloud_cover_pct, response_id
)
SELECT
    r.issue_date,
    x.day::date,
    (x.day::date - r.issue_date)::smallint,
    meta.try_numeric(x.weather_code)::smallint,
    meta.try_numeric(x.temp_mean),
    meta.try_numeric(x.temp_min),
    meta.try_numeric(x.temp_max),
    meta.try_numeric(x.precip),
    meta.try_numeric(x.precip_prob)::smallint,
    round(meta.try_numeric(x.wind_speed) / 3.6, 1),  -- км/ч -> м/с
    round(meta.try_numeric(x.wind_gusts) / 3.6, 1),
    meta.try_numeric(x.humidity)::smallint,
    meta.try_numeric(x.pressure),
    meta.try_numeric(x.cloud_cover)::smallint,
    r.response_id
FROM raw.openmeteo_forecast r
CROSS JOIN LATERAL ROWS FROM (
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
) AS x (day, weather_code, temp_mean, temp_min, temp_max, precip, precip_prob, wind_speed, wind_gusts,
        humidity, pressure, cloud_cover)
WHERE r.is_valid;
