-- target: core.fact_weather_daily
-- Сведение двух источников в одну запись на дату и история изменений (SCD2).
--
-- Правила сведения:
--   * температура — со станции (Meteostat), если там есть наблюдение;
--     если нет (пропуск или только модель DWD MOSMIX) — из Open-Meteo с поправкой
--     на среднее смещение станции за этот месяц (core.source_bias_monthly);
--   * осадки — со станции, если есть наблюдение, иначе из Open-Meteo;
--   * остальное (ветер, давление, облачность, снег и т. д.) — из Open-Meteo:
--     у станции эти данные есть не за все годы.
--
-- История: если для даты значения изменились (источник уточнил данные),
-- текущая версия закрывается (valid_to, is_current = false) и добавляется новая.

CREATE TEMP TABLE tmp_weather_daily ON COMMIT DROP AS
WITH joined AS (
    SELECT
        coalesce(o.obs_date, s.obs_date) AS obs_date,
        o.temp_mean, o.temp_min, o.temp_max, o.precip_mm, o.rain_mm, o.snowfall_cm, o.snow_depth_cm,
        o.wind_speed_ms, o.wind_gust_ms, o.wind_dir_deg, o.pressure_hpa, o.humidity_pct,
        o.cloud_cover_pct, o.dew_point_c, o.radiation_mj_m2, o.weather_code, o.response_id,
        s.temp_mean AS s_temp_mean,
        s.temp_min AS s_temp_min,
        s.temp_max AS s_temp_max,
        s.precip_mm AS s_precip_mm,
        s.precip_source AS s_precip_source,
        s.file_id AS s_file_id,
        (s.temp_mean IS NOT NULL AND NOT s.is_model_temp) AS station_ok,
        b.temp_mean_bias,
        b.temp_min_bias,
        b.temp_max_bias
    FROM staging.openmeteo_daily o
    FULL JOIN staging.meteostat_daily s ON s.obs_date = o.obs_date
    LEFT JOIN core.source_bias_monthly b ON b.month = extract(month FROM coalesce(o.obs_date, s.obs_date))
),
golden AS (
    SELECT
        current_setting('weather.station_id') AS station_id,
        j.obs_date,
        CASE WHEN station_ok THEN s_temp_mean
             ELSE round(j.temp_mean + coalesce(temp_mean_bias, 0), 1) END AS temp_mean,
        CASE WHEN station_ok AND s_temp_min IS NOT NULL THEN s_temp_min
             ELSE round(j.temp_min + coalesce(temp_min_bias, 0), 1) END AS temp_min,
        CASE WHEN station_ok AND s_temp_max IS NOT NULL THEN s_temp_max
             ELSE round(j.temp_max + coalesce(temp_max_bias, 0), 1) END AS temp_max,
        CASE WHEN station_ok THEN 'station' ELSE 'openmeteo_adjusted' END AS temp_source,
        s_temp_mean AS temp_mean_station,
        j.temp_mean AS temp_mean_openmeteo,
        CASE WHEN s_precip_mm IS NOT NULL AND coalesce(s_precip_source, '') <> 'dwd_mosmix' THEN s_precip_mm
             ELSE j.precip_mm END AS precip_mm,
        CASE WHEN s_precip_mm IS NOT NULL AND coalesce(s_precip_source, '') <> 'dwd_mosmix' THEN 'station'
             WHEN j.precip_mm IS NOT NULL THEN 'openmeteo' END AS precip_source,
        j.rain_mm,
        j.snowfall_cm,
        j.snow_depth_cm,
        j.wind_speed_ms,
        j.wind_gust_ms,
        j.wind_dir_deg,
        j.pressure_hpa,
        j.humidity_pct,
        j.cloud_cover_pct,
        j.dew_point_c,
        j.radiation_mj_m2,
        (SELECT c.code FROM core.dim_weather_code c WHERE c.code = j.weather_code) AS weather_code,
        j.response_id AS openmeteo_response_id,
        s_file_id AS meteostat_file_id
    FROM joined j
)
SELECT
    g.*,
    md5(concat_ws('|', g.temp_mean, g.temp_min, g.temp_max, g.temp_source, g.temp_mean_station,
                  g.temp_mean_openmeteo, g.precip_mm, g.precip_source, g.rain_mm, g.snowfall_cm,
                  g.snow_depth_cm, g.wind_speed_ms, g.wind_gust_ms, g.wind_dir_deg, g.pressure_hpa,
                  g.humidity_pct, g.cloud_cover_pct, g.dew_point_c, g.radiation_mj_m2, g.weather_code)) AS row_hash
FROM golden g
WHERE g.temp_mean IS NOT NULL
  AND g.obs_date >= current_setting('weather.history_start')::date;

-- 1. Закрываем текущие версии, у которых изменились значения
UPDATE core.fact_weather_daily f
SET valid_to = now(),
    is_current = false
FROM tmp_weather_daily t
WHERE f.is_current
  AND f.station_id = t.station_id
  AND f.obs_date = t.obs_date
  AND f.row_hash <> t.row_hash;

-- 2. Добавляем версии для новых дат и для дат, где версия только что закрыта
INSERT INTO core.fact_weather_daily (
    station_id, obs_date, temp_mean, temp_min, temp_max, temp_source, temp_mean_station,
    temp_mean_openmeteo, precip_mm, precip_source, rain_mm, snowfall_cm, snow_depth_cm,
    wind_speed_ms, wind_gust_ms, wind_dir_deg, pressure_hpa, humidity_pct, cloud_cover_pct,
    dew_point_c, radiation_mj_m2, weather_code, openmeteo_response_id, meteostat_file_id,
    row_hash, valid_from, valid_to, is_current, transform_run_id
)
SELECT
    t.station_id, t.obs_date, t.temp_mean, t.temp_min, t.temp_max, t.temp_source, t.temp_mean_station,
    t.temp_mean_openmeteo, t.precip_mm, t.precip_source, t.rain_mm, t.snowfall_cm, t.snow_depth_cm,
    t.wind_speed_ms, t.wind_gust_ms, t.wind_dir_deg, t.pressure_hpa, t.humidity_pct, t.cloud_cover_pct,
    t.dew_point_c, t.radiation_mj_m2, t.weather_code, t.openmeteo_response_id, t.meteostat_file_id,
    t.row_hash, now(), NULL, true, current_setting('weather.transform_run_id')::bigint
FROM tmp_weather_daily t
WHERE NOT EXISTS (
    SELECT 1
    FROM core.fact_weather_daily f
    WHERE f.is_current AND f.station_id = t.station_id AND f.obs_date = t.obs_date
);
