-- V003. Слой RAW: ответы источников без изменений.
-- Общие правила для всех таблиц слоя:
--   * данные только дописываются, существующие строки не меняются;
--   * повторное получение тех же данных не создаёт дубль — это обеспечивает
--     уникальный ключ на содержимое (контрольная сумма SHA-256);
--   * каждая строка ссылается на запись журнала загрузок (load_id) — по ней
--     восстанавливается происхождение данных;
--   * ответ с нарушенной структурой тоже сохраняется (is_valid = false) —
--     для разбора, но в следующие слои не попадает.

CREATE TABLE raw.openmeteo_archive (
    response_id       bigserial   PRIMARY KEY,
    load_id           bigint      NOT NULL REFERENCES meta.load_log (load_id),
    period_from       date        NOT NULL,
    period_to         date        NOT NULL,
    request_url       text        NOT NULL,
    request_params    jsonb       NOT NULL,
    payload           jsonb       NOT NULL,
    payload_sha256    char(64)    NOT NULL,
    fetched_at        timestamptz NOT NULL DEFAULT now(),
    is_valid          boolean     NOT NULL,
    validation_error  text,
    CHECK (period_from <= period_to),
    CHECK (is_valid OR validation_error IS NOT NULL),
    UNIQUE (period_from, period_to, payload_sha256)
);
COMMENT ON TABLE raw.openmeteo_archive IS
    'Ответы Open-Meteo Historical Weather API (JSON) за период period_from..period_to';
COMMENT ON COLUMN raw.openmeteo_archive.payload_sha256 IS
    'SHA-256 данных ответа без служебных полей (generationtime_ms) — для отсева повторов';

CREATE TABLE raw.openmeteo_forecast (
    response_id       bigserial   PRIMARY KEY,
    load_id           bigint      NOT NULL REFERENCES meta.load_log (load_id),
    issue_date        date        NOT NULL,
    request_url       text        NOT NULL,
    request_params    jsonb       NOT NULL,
    payload           jsonb       NOT NULL,
    payload_sha256    char(64)    NOT NULL,
    fetched_at        timestamptz NOT NULL DEFAULT now(),
    is_valid          boolean     NOT NULL,
    validation_error  text,
    CHECK (is_valid OR validation_error IS NOT NULL)
);
-- Один корректный прогноз на дату выпуска: повторный запуск в тот же день его не дублирует.
CREATE UNIQUE INDEX ux_openmeteo_forecast_issue ON raw.openmeteo_forecast (issue_date) WHERE is_valid;
COMMENT ON TABLE raw.openmeteo_forecast IS
    'Ежедневные снимки прогноза Open-Meteo Forecast API (JSON): дата выпуска + 7 суток вперёд';

CREATE TABLE raw.meteostat_file (
    file_id           bigserial   PRIMARY KEY,
    load_id           bigint      NOT NULL REFERENCES meta.load_log (load_id),
    station_id        text        NOT NULL,
    year              integer     NOT NULL CHECK (year BETWEEN 1900 AND 2100),
    url               text        NOT NULL,
    etag              text,
    last_modified     text,
    content           text        NOT NULL,
    content_sha256    char(64)    NOT NULL,
    columns           text[]      NOT NULL,
    row_count         integer     NOT NULL CHECK (row_count >= 0),
    fetched_at        timestamptz NOT NULL DEFAULT now(),
    is_valid          boolean     NOT NULL,
    validation_error  text,
    CHECK (is_valid OR validation_error IS NOT NULL),
    UNIQUE (station_id, year, content_sha256)
);
CREATE INDEX ix_meteostat_file_year ON raw.meteostat_file (station_id, year, fetched_at DESC);
COMMENT ON TABLE raw.meteostat_file IS
    'Годовые CSV-файлы Meteostat (распакованный текст). Новая версия файла сохраняется отдельной строкой.';
COMMENT ON COLUMN raw.meteostat_file.etag IS
    'ETag ответа сервера: при следующем запуске файл запрашивается условно и не скачивается, если не изменился';
COMMENT ON COLUMN raw.meteostat_file.columns IS
    'Заголовок CSV. Состав колонок меняется от года к году — фиксируем для контроля схемы';
