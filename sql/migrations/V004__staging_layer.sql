-- V004. Журнал преобразований и слой STAGING.

-- Журнал запусков преобразований (raw -> staging -> core -> mart)
CREATE TABLE meta.transform_run (
    transform_run_id  bigserial   PRIMARY KEY,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    status            text        NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'success', 'failed')),
    error_message     text
);
COMMENT ON TABLE meta.transform_run IS 'Запуски преобразований данных между слоями';

CREATE TABLE meta.transform_step (
    transform_run_id  bigint  NOT NULL REFERENCES meta.transform_run (transform_run_id),
    step              text    NOT NULL,
    target_table      text    NOT NULL,
    status            text    NOT NULL CHECK (status IN ('success', 'failed')),
    rows_after        bigint,
    duration_ms       integer NOT NULL,
    PRIMARY KEY (transform_run_id, step)
);
COMMENT ON TABLE meta.transform_step IS 'Шаги преобразования: какой SQL-файл, какую таблицу заполнил, сколько в ней строк после шага';

-- Безопасное приведение текста к числу: некорректное значение превращается в NULL,
-- а не роняет всё преобразование. Такие значения потом ловят проверки качества.
CREATE FUNCTION meta.try_numeric(value text) RETURNS numeric
    LANGUAGE sql IMMUTABLE
    AS $$
        SELECT CASE WHEN value ~ '^\s*-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?\s*$' THEN value::numeric END
    $$;

-- Open-Meteo, история: одна строка на дату (последняя полученная версия)
CREATE TABLE staging.openmeteo_daily (
    obs_date            date        PRIMARY KEY,
    weather_code        smallint,
    temp_mean           numeric,
    temp_min            numeric,
    temp_max            numeric,
    apparent_temp_mean  numeric,
    precip_mm           numeric,
    rain_mm             numeric,
    snowfall_cm         numeric,
    precip_hours        numeric,
    wind_speed_ms       numeric,
    wind_gust_ms        numeric,
    wind_dir_deg        smallint,
    radiation_mj_m2     numeric,
    humidity_pct        smallint,
    pressure_hpa        numeric,
    cloud_cover_pct     smallint,
    dew_point_c         numeric,
    snow_depth_cm       numeric,
    response_id         bigint      NOT NULL,
    fetched_at          timestamptz NOT NULL
);
COMMENT ON TABLE staging.openmeteo_daily IS
    'Open-Meteo, история по дням. Ветер переведён из км/ч в м/с, высота снега из м в см';

-- Meteostat: одна строка на дату, только прошедшие дни
CREATE TABLE staging.meteostat_daily (
    obs_date          date        PRIMARY KEY,
    temp_mean         numeric,
    temp_min          numeric,
    temp_max          numeric,
    precip_mm         numeric,
    snow_depth_cm     numeric,
    wind_speed_ms     numeric,
    pressure_hpa      numeric,
    humidity_pct      smallint,
    cloud_cover_okta  smallint,
    temp_source       text,
    precip_source     text,
    is_model_temp     boolean     NOT NULL,
    file_id           bigint      NOT NULL,
    fetched_at        timestamptz NOT NULL
);
COMMENT ON TABLE staging.meteostat_daily IS
    'Meteostat, станция 27612 по дням. Будущие даты из файла (прогноз DWD MOSMIX) отброшены';
COMMENT ON COLUMN staging.meteostat_daily.is_model_temp IS
    'Температура взята только из модели DWD MOSMIX, а не из наблюдений';

-- Open-Meteo, прогноз: дата выпуска x дата, на которую прогноз
CREATE TABLE staging.openmeteo_forecast (
    issue_date       date     NOT NULL,
    target_date      date     NOT NULL,
    lead_days        smallint NOT NULL,
    weather_code     smallint,
    temp_mean        numeric,
    temp_min         numeric,
    temp_max         numeric,
    precip_mm        numeric,
    precip_prob_pct  smallint,
    wind_speed_ms    numeric,
    wind_gust_ms     numeric,
    humidity_pct     smallint,
    pressure_hpa     numeric,
    cloud_cover_pct  smallint,
    response_id      bigint   NOT NULL,
    PRIMARY KEY (issue_date, target_date)
);
COMMENT ON TABLE staging.openmeteo_forecast IS 'Прогнозы Open-Meteo по датам выпуска';
