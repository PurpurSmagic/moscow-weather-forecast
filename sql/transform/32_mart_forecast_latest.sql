-- target: mart.forecast_latest
-- Последний выпуск прогноза каждой модели с теми же признаками, что и у факта.
-- Серия холодных дней продолжает фактическую: если вчера была 3-я холодная
-- подряд, а сегодня по прогнозу тоже холодно — это уже 4-й день.
-- Прогноз Open-Meteo считается для точки сетки, а факт — по станции, которая теплее.
-- Поэтому к температуре прогноза Open-Meteo добавляем ту же поправку по месяцам,
-- что и при заполнении пропусков станции.

TRUNCATE mart.forecast_latest;

INSERT INTO mart.forecast_latest (
    model_code, issue_date, target_date, lead_days, temp_mean, temp_min, temp_max, bias_correction, temp_norm,
    temp_anomaly, precip_mm, precip_prob_pct, weather_description, below_heating_threshold,
    cold_streak_days, heating_condition_met, zero_crossing, ice_risk
)
WITH params AS (
    SELECT
        current_setting('weather.heating_threshold_c')::numeric AS heat_t,
        current_setting('weather.heating_days')::int AS heat_days,
        current_setting('weather.ice_threshold_c')::numeric AS ice_t
),
last_actual AS (
    SELECT obs_date, cold_streak_days FROM mart.weather_daily ORDER BY obs_date DESC LIMIT 1
),
latest AS (
    -- последний выпуск каждой модели, только даты после последнего факта
    SELECT
        f.model_code, f.issue_date, f.target_date, f.lead_days, f.precip_mm, f.precip_prob_pct, f.weather_code,
        round(f.temp_mean + c.bias_mean, 1) AS temp_mean,
        round(f.temp_min + c.bias_min, 1) AS temp_min,
        round(f.temp_max + c.bias_max, 1) AS temp_max,
        c.bias_mean AS bias_correction
    FROM core.fact_forecast f
    LEFT JOIN core.source_bias_monthly b ON b.month = extract(month FROM f.target_date)
    CROSS JOIN LATERAL (
        SELECT
            CASE WHEN f.model_code = 'openmeteo' THEN coalesce(b.temp_mean_bias, 0) ELSE 0 END AS bias_mean,
            CASE WHEN f.model_code = 'openmeteo' THEN coalesce(b.temp_min_bias, 0) ELSE 0 END AS bias_min,
            CASE WHEN f.model_code = 'openmeteo' THEN coalesce(b.temp_max_bias, 0) ELSE 0 END AS bias_max
    ) c
    WHERE f.issue_date = (SELECT max(x.issue_date) FROM core.fact_forecast x WHERE x.model_code = f.model_code)
      AND f.target_date > coalesce((SELECT obs_date FROM last_actual), '-infinity'::date)
),
days AS (
    SELECT
        l.*,
        d.day_of_year,
        wc.description AS weather_description,
        coalesce(wc.is_freezing, false) AS is_freezing,
        l.temp_mean < p.heat_t AS below,
        p.heat_days,
        p.ice_t,
        -- продолжает ли прогноз фактический ряд без разрыва
        min(l.target_date) OVER (PARTITION BY l.model_code) = (SELECT obs_date FROM last_actual) + 1 AS continues
    FROM latest l
    JOIN core.dim_date d ON d.date_value = l.target_date
    LEFT JOIN core.dim_weather_code wc ON wc.code = l.weather_code
    CROSS JOIN params p
),
groups AS (
    SELECT days.*, sum(CASE WHEN below THEN 0 ELSE 1 END) OVER (PARTITION BY model_code ORDER BY target_date) AS grp
    FROM days
),
streaks AS (
    SELECT
        groups.*,
        CASE WHEN below
             THEN count(*) FILTER (WHERE below) OVER (PARTITION BY model_code, grp ORDER BY target_date)
                  + CASE WHEN grp = 0 AND continues
                         THEN coalesce((SELECT cold_streak_days FROM last_actual), 0) ELSE 0 END
             ELSE 0 END AS cold_streak
    FROM groups
)
SELECT
    s.model_code, s.issue_date, s.target_date, s.lead_days, s.temp_mean, s.temp_min, s.temp_max,
    s.bias_correction,
    n.temp_mean_norm,
    round(s.temp_mean - n.temp_mean_norm, 1),
    s.precip_mm, s.precip_prob_pct, s.weather_description,
    s.below,
    s.cold_streak,
    s.cold_streak >= s.heat_days,
    s.temp_min < s.ice_t AND s.temp_max > s.ice_t,
    (s.temp_min < s.ice_t AND s.temp_max > s.ice_t AND coalesce(s.precip_mm, 0) > 0) OR s.is_freezing
FROM streaks s
LEFT JOIN mart.climate_norm n ON n.day_of_year = s.day_of_year;
