-- target: core.dim_date
-- Календарь от начала истории до месяца вперёд. Уже существующие даты не трогаем.

INSERT INTO core.dim_date (
    date_value, year, quarter, month, month_name, day, day_of_year, iso_week, day_of_week, is_weekend, season
)
SELECT
    d::date,
    extract(year FROM d)::smallint,
    extract(quarter FROM d)::smallint,
    extract(month FROM d)::smallint,
    (ARRAY['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август',
           'сентябрь', 'октябрь', 'ноябрь', 'декабрь'])[extract(month FROM d)::int],
    extract(day FROM d)::smallint,
    extract(doy FROM d)::smallint,
    extract(week FROM d)::smallint,
    extract(isodow FROM d)::smallint,
    extract(isodow FROM d) IN (6, 7),
    CASE
        WHEN extract(month FROM d) IN (12, 1, 2) THEN 'зима'
        WHEN extract(month FROM d) IN (3, 4, 5) THEN 'весна'
        WHEN extract(month FROM d) IN (6, 7, 8) THEN 'лето'
        ELSE 'осень'
    END
FROM generate_series(
    least(
        current_setting('weather.history_start')::date,
        (SELECT min(obs_date) FROM staging.openmeteo_daily),
        (SELECT min(obs_date) FROM staging.meteostat_daily)
    ),
    greatest(current_date + 31, (SELECT max(target_date) FROM staging.openmeteo_forecast)),
    interval '1 day'
) AS d
ON CONFLICT (date_value) DO NOTHING;
