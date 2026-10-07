-- target: mart.climate_norm
-- Климатическая норма: среднее за базовый период (по умолчанию 1991–2020, как у ВМО)
-- для каждого дня года. Чтобы норма не «прыгала» от дня к дню, берём окно ±7 дней.

TRUNCATE mart.climate_norm;

INSERT INTO mart.climate_norm (
    day_of_year, temp_mean_norm, temp_min_norm, temp_max_norm, temp_mean_std, precip_norm_mm, years, period
)
WITH base AS (
    SELECT
        extract(doy FROM obs_date)::int AS doy,
        extract(year FROM obs_date)::int AS year,
        temp_mean, temp_min, temp_max, precip_mm
    FROM core.fact_weather_daily
    WHERE is_current
      AND station_id = current_setting('weather.station_id')
      AND extract(year FROM obs_date) BETWEEN current_setting('weather.norm_from_year')::int
                                          AND current_setting('weather.norm_to_year')::int
)
SELECT
    d.doy,
    round(avg(b.temp_mean), 1),
    round(avg(b.temp_min), 1),
    round(avg(b.temp_max), 1),
    round(stddev(b.temp_mean), 1),
    round(avg(b.precip_mm), 1),
    count(DISTINCT b.year),
    current_setting('weather.norm_from_year') || '–' || current_setting('weather.norm_to_year')
FROM generate_series(1, 366) AS d (doy)
JOIN base b ON least(abs(b.doy - d.doy), 366 - abs(b.doy - d.doy)) <= 7
GROUP BY d.doy;
