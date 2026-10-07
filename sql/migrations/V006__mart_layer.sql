-- V006. Слой MART: витрины для дашбордов и моделей.

-- Климатическая норма по дню года (сглаживание ±7 дней)
CREATE TABLE mart.climate_norm (
    day_of_year     smallint PRIMARY KEY CHECK (day_of_year BETWEEN 1 AND 366),
    temp_mean_norm  numeric  NOT NULL,
    temp_min_norm   numeric  NOT NULL,
    temp_max_norm   numeric  NOT NULL,
    temp_mean_std   numeric,
    precip_norm_mm  numeric,
    years           smallint NOT NULL,
    period          text     NOT NULL
);
COMMENT ON TABLE mart.climate_norm IS 'Климатическая норма температуры и осадков по дню года';

-- Погода по дням с нормой и признаками для решений
CREATE TABLE mart.weather_daily (
    obs_date                 date     PRIMARY KEY,
    year                     smallint NOT NULL,
    month                    smallint NOT NULL,
    month_name               text     NOT NULL,
    season                   text     NOT NULL,
    day_of_year              smallint NOT NULL,
    temp_mean                numeric,
    temp_min                 numeric,
    temp_max                 numeric,
    temp_source              text     NOT NULL,
    temp_norm                numeric,
    temp_anomaly             numeric,
    precip_mm                numeric,
    snowfall_cm              numeric,
    snow_depth_cm            numeric,
    wind_speed_ms            numeric,
    wind_gust_ms             numeric,
    pressure_hpa             numeric,
    humidity_pct             smallint,
    cloud_cover_pct          smallint,
    weather_code             smallint,
    weather_description      text,
    below_heating_threshold  boolean,
    cold_streak_days         integer  NOT NULL,
    heating_condition_met    boolean  NOT NULL,
    zero_crossing            boolean,
    ice_risk                 boolean
);
COMMENT ON TABLE mart.weather_daily IS 'Фактическая погода по дням: норма, отклонение, признаки отопления и гололёда';
COMMENT ON COLUMN mart.weather_daily.cold_streak_days IS
    'Сколько дней подряд (включая этот) среднесуточная температура ниже порога отопления';
COMMENT ON COLUMN mart.weather_daily.heating_condition_met IS
    'Выполнено условие начала отопления: не меньше заданного числа дней подряд ниже порога';
COMMENT ON COLUMN mart.weather_daily.ice_risk IS
    'Риск гололёда: переход через 0 °C при осадках или переохлаждённые осадки';

-- Последний прогноз на неделю с теми же признаками
CREATE TABLE mart.forecast_latest (
    model_code               text     NOT NULL,
    issue_date               date     NOT NULL,
    target_date              date     NOT NULL,
    lead_days                smallint NOT NULL,
    temp_mean                numeric,
    temp_min                 numeric,
    temp_max                 numeric,
    bias_correction          numeric  NOT NULL,
    temp_norm                numeric,
    temp_anomaly             numeric,
    precip_mm                numeric,
    precip_prob_pct          smallint,
    weather_description      text,
    below_heating_threshold  boolean,
    cold_streak_days         integer  NOT NULL,
    heating_condition_met    boolean  NOT NULL,
    zero_crossing            boolean,
    ice_risk                 boolean,
    PRIMARY KEY (model_code, target_date)
);
COMMENT ON TABLE mart.forecast_latest IS
    'Последний выпуск прогноза каждой модели. Серия холодных дней продолжает фактическую серию';
COMMENT ON COLUMN mart.forecast_latest.bias_correction IS
    'Поправка, добавленная к температуре прогноза Open-Meteo, чтобы привести его к станции '
    '(как при заполнении пропусков в core.fact_weather_daily)';
