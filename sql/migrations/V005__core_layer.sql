-- V005. Слой CORE: справочники и факты.

-- Справочник станций. Заполняется из config/settings.yaml при каждом преобразовании
CREATE TABLE core.dim_station (
    station_id   text        PRIMARY KEY,
    wmo_id       text        NOT NULL,
    name         text        NOT NULL,
    latitude     numeric     NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude    numeric     NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    elevation_m  numeric,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE core.dim_station IS 'Справочник метеостанций';

-- Календарь
CREATE TABLE core.dim_date (
    date_value   date     PRIMARY KEY,
    year         smallint NOT NULL,
    quarter      smallint NOT NULL,
    month        smallint NOT NULL,
    month_name   text     NOT NULL,
    day          smallint NOT NULL,
    day_of_year  smallint NOT NULL,
    iso_week     smallint NOT NULL,
    day_of_week  smallint NOT NULL,  -- 1 = понедельник
    is_weekend   boolean  NOT NULL,
    season       text     NOT NULL CHECK (season IN ('зима', 'весна', 'лето', 'осень'))
);
COMMENT ON TABLE core.dim_date IS 'Календарь: атрибуты даты для разрезов в витринах и дашбордах';

-- Коды погоды ВМО, которые использует Open-Meteo
CREATE TABLE core.dim_weather_code (
    code              smallint PRIMARY KEY,
    description       text     NOT NULL,
    category          text     NOT NULL,
    is_precipitation  boolean  NOT NULL,
    is_freezing       boolean  NOT NULL
);
COMMENT ON TABLE core.dim_weather_code IS 'Коды погодных явлений ВМО (WMO 4677, в варианте Open-Meteo)';
COMMENT ON COLUMN core.dim_weather_code.is_freezing IS 'Переохлаждённые осадки — прямой признак гололёда';

INSERT INTO core.dim_weather_code (code, description, category, is_precipitation, is_freezing) VALUES
    (0,  'Ясно',                                  'ясно',     false, false),
    (1,  'Преимущественно ясно',                  'облачно',  false, false),
    (2,  'Переменная облачность',                 'облачно',  false, false),
    (3,  'Пасмурно',                              'облачно',  false, false),
    (45, 'Туман',                                 'туман',    false, false),
    (48, 'Туман с изморозью',                     'туман',    false, true),
    (51, 'Слабая морось',                         'морось',   true,  false),
    (53, 'Умеренная морось',                      'морось',   true,  false),
    (55, 'Сильная морось',                        'морось',   true,  false),
    (56, 'Слабая переохлаждённая морось',         'морось',   true,  true),
    (57, 'Сильная переохлаждённая морось',        'морось',   true,  true),
    (61, 'Слабый дождь',                          'дождь',    true,  false),
    (63, 'Умеренный дождь',                       'дождь',    true,  false),
    (65, 'Сильный дождь',                         'дождь',    true,  false),
    (66, 'Слабый переохлаждённый дождь',          'дождь',    true,  true),
    (67, 'Сильный переохлаждённый дождь',         'дождь',    true,  true),
    (71, 'Слабый снег',                           'снег',     true,  false),
    (73, 'Умеренный снег',                        'снег',     true,  false),
    (75, 'Сильный снег',                          'снег',     true,  false),
    (77, 'Снежные зёрна',                         'снег',     true,  false),
    (80, 'Слабый ливень',                         'ливень',   true,  false),
    (81, 'Умеренный ливень',                      'ливень',   true,  false),
    (82, 'Сильный ливень',                        'ливень',   true,  false),
    (85, 'Слабый снегопад',                       'снег',     true,  false),
    (86, 'Сильный снегопад',                      'снег',     true,  false),
    (95, 'Гроза',                                 'гроза',    true,  false),
    (96, 'Гроза со слабым градом',                'гроза',    true,  false),
    (99, 'Гроза с сильным градом',                'гроза',    true,  false);

-- Систематическое смещение: насколько станция в среднем теплее точки сетки Open-Meteo.
-- Используется, когда у станции нет значения и пропуск заполняется из Open-Meteo
CREATE TABLE core.source_bias_monthly (
    month           smallint PRIMARY KEY CHECK (month BETWEEN 1 AND 12),
    temp_mean_bias  numeric  NOT NULL,
    temp_min_bias   numeric  NOT NULL,
    temp_max_bias   numeric  NOT NULL,
    days            integer  NOT NULL,
    period_from     date     NOT NULL,
    period_to       date     NOT NULL
);
COMMENT ON TABLE core.source_bias_monthly IS
    'Среднее (станция Meteostat - Open-Meteo) по месяцам за опорный период: поправка при заполнении пропусков';

-- Погода по дням: сведённая запись из двух источников с историей изменений (SCD2)
CREATE TABLE core.fact_weather_daily (
    version_id             bigserial   PRIMARY KEY,
    station_id             text        NOT NULL REFERENCES core.dim_station (station_id),
    obs_date               date        NOT NULL REFERENCES core.dim_date (date_value),
    temp_mean              numeric,
    temp_min               numeric,
    temp_max               numeric,
    temp_source            text        NOT NULL CHECK (temp_source IN ('station', 'openmeteo_adjusted')),
    temp_mean_station      numeric,
    temp_mean_openmeteo    numeric,
    precip_mm              numeric,
    precip_source          text        CHECK (precip_source IN ('station', 'openmeteo')),
    rain_mm                numeric,
    snowfall_cm            numeric,
    snow_depth_cm          numeric,
    wind_speed_ms          numeric,
    wind_gust_ms           numeric,
    wind_dir_deg           smallint,
    pressure_hpa           numeric,
    humidity_pct           smallint,
    cloud_cover_pct        smallint,
    dew_point_c            numeric,
    radiation_mj_m2        numeric,
    weather_code           smallint    REFERENCES core.dim_weather_code (code),
    openmeteo_response_id  bigint      REFERENCES raw.openmeteo_archive (response_id),
    meteostat_file_id      bigint      REFERENCES raw.meteostat_file (file_id),
    row_hash               char(32)    NOT NULL,
    valid_from             timestamptz NOT NULL,
    valid_to               timestamptz,
    is_current             boolean     NOT NULL,
    transform_run_id       bigint      REFERENCES meta.transform_run (transform_run_id),
    CHECK (is_current = (valid_to IS NULL)),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
-- Текущая версия на дату может быть только одна
CREATE UNIQUE INDEX ux_fact_weather_daily_current ON core.fact_weather_daily (station_id, obs_date) WHERE is_current;
CREATE INDEX ix_fact_weather_daily_date ON core.fact_weather_daily (obs_date);

COMMENT ON TABLE core.fact_weather_daily IS
    'Погода по дням для станции. Хранятся все версии: при изменении данных старая версия закрывается '
    '(valid_to, is_current = false), добавляется новая';
COMMENT ON COLUMN core.fact_weather_daily.temp_source IS
    'station — значение станции (Meteostat); openmeteo_adjusted — Open-Meteo с поправкой на смещение, '
    'если у станции нет наблюдения';
COMMENT ON COLUMN core.fact_weather_daily.row_hash IS 'MD5 значимых полей: по нему определяется, изменилась ли запись';

-- Прогнозы: Open-Meteo и (на следующих этапах) собственные модели
CREATE TABLE core.fact_forecast (
    model_code          text        NOT NULL,
    issue_date          date        NOT NULL REFERENCES core.dim_date (date_value),
    target_date         date        NOT NULL REFERENCES core.dim_date (date_value),
    lead_days           smallint    NOT NULL CHECK (lead_days >= 0),
    temp_mean           numeric,
    temp_min            numeric,
    temp_max            numeric,
    precip_mm           numeric,
    precip_prob_pct     smallint,
    weather_code        smallint    REFERENCES core.dim_weather_code (code),
    source_response_id  bigint      REFERENCES raw.openmeteo_forecast (response_id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    transform_run_id    bigint      REFERENCES meta.transform_run (transform_run_id),
    PRIMARY KEY (model_code, issue_date, target_date),
    CHECK (target_date >= issue_date)
);
COMMENT ON TABLE core.fact_forecast IS
    'Прогнозы по датам выпуска. Выпущенный прогноз не меняется: новые выпуски добавляются, старые остаются';
