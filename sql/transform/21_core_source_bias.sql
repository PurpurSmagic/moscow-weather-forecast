-- target: core.source_bias_monthly
-- Станция ВДНХ в среднем теплее точки сетки Open-Meteo (городской остров тепла, разная
-- высота). Считаем это смещение по месяцам на днях, где есть оба источника, за
-- фиксированный опорный период — чтобы поправка не менялась при каждой загрузке.

TRUNCATE core.source_bias_monthly;

INSERT INTO core.source_bias_monthly (
    month, temp_mean_bias, temp_min_bias, temp_max_bias, days, period_from, period_to
)
SELECT
    extract(month FROM s.obs_date)::smallint,
    round(avg(s.temp_mean - o.temp_mean), 2),
    round(coalesce(avg(s.temp_min - o.temp_min), 0), 2),
    round(coalesce(avg(s.temp_max - o.temp_max), 0), 2),
    count(*),
    current_setting('weather.bias_from')::date,
    current_setting('weather.bias_to')::date
FROM staging.meteostat_daily s
JOIN staging.openmeteo_daily o USING (obs_date)
WHERE s.temp_mean IS NOT NULL
  AND o.temp_mean IS NOT NULL
  AND NOT s.is_model_temp
  AND s.obs_date BETWEEN current_setting('weather.bias_from')::date AND current_setting('weather.bias_to')::date
GROUP BY 1;
