-- target: core.fact_forecast
-- Прогнозы Open-Meteo в общую таблицу прогнозов. Выпущенный прогноз не меняется,
-- поэтому только добавляем новые выпуски.

INSERT INTO core.fact_forecast (
    model_code, issue_date, target_date, lead_days, temp_mean, temp_min, temp_max,
    precip_mm, precip_prob_pct, weather_code, source_response_id, transform_run_id
)
SELECT
    'openmeteo',
    f.issue_date,
    f.target_date,
    f.lead_days,
    f.temp_mean,
    f.temp_min,
    f.temp_max,
    f.precip_mm,
    f.precip_prob_pct,
    (SELECT c.code FROM core.dim_weather_code c WHERE c.code = f.weather_code),
    f.response_id,
    current_setting('weather.transform_run_id')::bigint
FROM staging.openmeteo_forecast f
ON CONFLICT (model_code, issue_date, target_date) DO NOTHING;
