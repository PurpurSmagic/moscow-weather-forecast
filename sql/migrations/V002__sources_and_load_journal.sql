-- V002. Справочник источников и журнал загрузок.

-- Справочник источников данных: откуда, каким способом и в каком формате
-- поступают данные. На него ссылаются журнал загрузок и отметки загрузки.
CREATE TABLE meta.source (
    source_code  text PRIMARY KEY,
    name         text NOT NULL,
    provider     text NOT NULL,
    method       text NOT NULL CHECK (method IN ('rest_api', 'file_download', 'html')),
    data_format  text NOT NULL CHECK (data_format IN ('json', 'csv', 'html')),
    base_url     text NOT NULL,
    license      text NOT NULL,
    description  text NOT NULL
);
COMMENT ON TABLE meta.source IS 'Справочник источников данных: способ получения, формат, лицензия';

INSERT INTO meta.source (source_code, name, provider, method, data_format, base_url, license, description) VALUES
    ('openmeteo_archive', 'Historical Weather API', 'Open-Meteo', 'rest_api', 'json',
     'https://archive-api.open-meteo.com/v1/archive', 'CC BY 4.0',
     'Суточные значения реанализа ERA5 и модели ECMWF IFS в точке станции ВДНХ. '
     'Последние дни источник уточняет, поэтому они перезапрашиваются.'),
    ('openmeteo_forecast', 'Forecast API', 'Open-Meteo', 'rest_api', 'json',
     'https://api.open-meteo.com/v1/forecast', 'CC BY 4.0',
     'Суточный прогноз численных моделей погоды на 7 суток. Сохраняется раз в сутки — '
     'получается архив прогнозов для сравнения с собственными моделями.'),
    ('meteostat_daily', 'Bulk Data: daily', 'Meteostat', 'file_download', 'csv',
     'https://data.meteostat.net/daily/', 'CC BY 4.0',
     'Суточные данные станции 27612 (Москва, ВДНХ): один сжатый CSV-файл на год. '
     'Для каждого значения указан первоисточник (GHCN-D, DWD и др.).');

-- Запуск конвейера целиком: все источники за один вызов.
CREATE TABLE meta.pipeline_run (
    run_id         bigserial   PRIMARY KEY,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    status         text        NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'success', 'partial', 'failed')),
    trigger        text        NOT NULL DEFAULT 'manual'
                   CHECK (trigger IN ('manual', 'schedule', 'test')),
    params         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    error_message  text,
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);
COMMENT ON TABLE meta.pipeline_run IS 'Запуски конвейера: время, статус, кто запустил';

-- Журнал загрузок: одна строка на источник в каждом запуске.
CREATE TABLE meta.load_log (
    load_id             bigserial   PRIMARY KEY,
    run_id              bigint      NOT NULL REFERENCES meta.pipeline_run (run_id),
    source_code         text        NOT NULL REFERENCES meta.source (source_code),
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    status              text        NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'success', 'partial', 'failed', 'skipped')),
    period_from         date,
    period_to           date,
    requests            integer     NOT NULL DEFAULT 0 CHECK (requests >= 0),
    retries             integer     NOT NULL DEFAULT 0 CHECK (retries >= 0),
    payloads_received   integer     NOT NULL DEFAULT 0 CHECK (payloads_received >= 0),
    payloads_new        integer     NOT NULL DEFAULT 0 CHECK (payloads_new >= 0),
    payloads_unchanged  integer     NOT NULL DEFAULT 0 CHECK (payloads_unchanged >= 0),
    payloads_invalid    integer     NOT NULL DEFAULT 0 CHECK (payloads_invalid >= 0),
    records_received    integer     NOT NULL DEFAULT 0 CHECK (records_received >= 0),
    watermark_before    date,
    watermark_after     date,
    error_message       text,
    details             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    CHECK (finished_at IS NULL OR finished_at >= started_at),
    CHECK (period_from IS NULL OR period_to IS NULL OR period_from <= period_to)
);
CREATE INDEX ix_load_log_source_started ON meta.load_log (source_code, started_at DESC);
CREATE INDEX ix_load_log_run ON meta.load_log (run_id);

COMMENT ON TABLE meta.load_log IS 'Журнал загрузок: что запрошено, что получено, что сохранено, чем закончилось';
COMMENT ON COLUMN meta.load_log.status IS
    'success — всё загружено; partial — часть данных загружена, часть нет; failed — ничего не загружено; '
    'skipped — новых данных нет, источник не опрашивался';
COMMENT ON COLUMN meta.load_log.period_from IS 'Начало запрошенного периода данных';
COMMENT ON COLUMN meta.load_log.period_to IS 'Конец запрошенного периода данных';
COMMENT ON COLUMN meta.load_log.requests IS 'HTTP-запросов, включая повторные';
COMMENT ON COLUMN meta.load_log.retries IS 'Повторных запросов после сбоев (таймаут, 429, 5xx)';
COMMENT ON COLUMN meta.load_log.payloads_received IS 'Получено ответов/файлов с данными';
COMMENT ON COLUMN meta.load_log.payloads_new IS 'Сохранено в raw новых ответов/файлов';
COMMENT ON COLUMN meta.load_log.payloads_unchanged IS 'Ответов/файлов, уже сохранённых ранее (не дублируются)';
COMMENT ON COLUMN meta.load_log.payloads_invalid IS 'Ответов/файлов с нарушенной структурой';
COMMENT ON COLUMN meta.load_log.records_received IS 'Суточных записей в полученных данных';
COMMENT ON COLUMN meta.load_log.watermark_before IS 'Отметка загрузки до запуска';
COMMENT ON COLUMN meta.load_log.watermark_after IS 'Отметка загрузки после запуска';

-- Отметки инкрементальной загрузки: до какой даты данные источника уже загружены.
CREATE TABLE meta.watermark (
    source_code   text        PRIMARY KEY REFERENCES meta.source (source_code),
    loaded_until  date        NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    load_id       bigint      REFERENCES meta.load_log (load_id)
);
COMMENT ON TABLE meta.watermark IS
    'Отметки инкрементальной загрузки: последняя дата, по которую данные источника загружены. '
    'Следующий запуск запрашивает данные начиная с неё (с перекрытием).';
