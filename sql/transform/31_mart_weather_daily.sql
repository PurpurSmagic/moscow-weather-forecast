-- target: mart.weather_daily
-- Фактическая погода по дням для дашборда: норма, отклонение от нормы и признаки
-- для решений ЖКХ:
--   * cold_streak_days — сколько дней подряд среднесуточная ниже порога отопления;
--   * heating_condition_met — серия достигла нужной длины (по умолчанию 5 дней ниже +8 °C);
--   * ice_risk — переход через 0 °C при осадках или переохлаждённые осадки.

TRUNCATE mart.weather_daily;

INSERT INTO mart.weather_daily (
    obs_date, year, month, month_name, season, day_of_year, temp_mean, temp_min, temp_max,
    temp_source, temp_norm, temp_anomaly, precip_mm, snowfall_cm, snow_depth_cm, wind_speed_ms,
    wind_gust_ms, pressure_hpa, humidity_pct, cloud_cover_pct, weather_code, weather_description,
    below_heating_threshold, cold_streak_days, heating_condition_met, zero_crossing, ice_risk
)
WITH params AS (
    SELECT
        current_setting('weather.heating_threshold_c')::numeric AS heat_t,
        current_setting('weather.heating_days')::int AS heat_days,
        current_setting('weather.ice_threshold_c')::numeric AS ice_t
),
days AS (
    SELECT
        f.*,
        d.year, d.month, d.month_name, d.season, d.day_of_year,
        wc.description AS weather_description,
        coalesce(wc.is_freezing, false) AS is_freezing,
        f.temp_mean < p.heat_t AS below,
        p.heat_days,
        p.ice_t
    FROM core.fact_weather_daily f
    JOIN core.dim_date d ON d.date_value = f.obs_date
    LEFT JOIN core.dim_weather_code wc ON wc.code = f.weather_code
    CROSS JOIN params p
    WHERE f.is_current
      AND f.station_id = current_setting('weather.station_id')
),
groups AS (
    -- номер группы растёт на каждом «тёплом» дне: холодные дни подряд попадают в одну группу
    SELECT days.*, sum(CASE WHEN below THEN 0 ELSE 1 END) OVER (ORDER BY obs_date) AS grp
    FROM days
),
streaks AS (
    SELECT
        groups.*,
        CASE WHEN below
             THEN count(*) FILTER (WHERE below) OVER (PARTITION BY grp ORDER BY obs_date)
             ELSE 0 END AS cold_streak
    FROM groups
)
SELECT
    s.obs_date, s.year, s.month, s.month_name, s.season, s.day_of_year,
    s.temp_mean, s.temp_min, s.temp_max, s.temp_source,
    n.temp_mean_norm,
    round(s.temp_mean - n.temp_mean_norm, 1),
    s.precip_mm, s.snowfall_cm, s.snow_depth_cm, s.wind_speed_ms, s.wind_gust_ms,
    s.pressure_hpa, s.humidity_pct, s.cloud_cover_pct, s.weather_code, s.weather_description,
    s.below,
    s.cold_streak,
    s.cold_streak >= s.heat_days,
    s.temp_min < s.ice_t AND s.temp_max > s.ice_t,
    (s.temp_min < s.ice_t AND s.temp_max > s.ice_t AND coalesce(s.precip_mm, 0) > 0) OR s.is_freezing
FROM streaks s
LEFT JOIN mart.climate_norm n ON n.day_of_year = s.day_of_year;
