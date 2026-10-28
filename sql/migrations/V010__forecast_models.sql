-- V010. Модели прогноза: запуски, метрики качества и прогнозы моделей.
--
-- Модели обучаются на Python (src/weather/forecast), а не в dbt, поэтому таблицы для них
-- создаются миграцией. dbt читает прогнозы моделей как источник и добавляет их в общую
-- таблицу прогнозов core.fact_forecast и в витрины.

-- Запуски: оценка качества (evaluate) и построение прогноза (forecast)
CREATE TABLE meta.model_run (
    model_run_id   bigserial   PRIMARY KEY,
    kind           text        NOT NULL CHECK (kind IN ('evaluate', 'forecast')),
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    status         text        NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'failed')),
    data_until     date,
    params         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    error_message  text
);
COMMENT ON TABLE meta.model_run IS 'Запуски моделей прогноза: оценка качества (evaluate) и прогноз (forecast)';
COMMENT ON COLUMN meta.model_run.data_until IS 'Последняя дата фактических данных, на которых обучались модели';
COMMENT ON COLUMN meta.model_run.params IS
    'Периоды проверки и параметры моделей; для evaluate — параметры, выбранные на проверочном периоде';

-- Метрики качества по каждой модели и горизонту прогноза
CREATE TABLE meta.model_metric (
    model_run_id        bigint   NOT NULL REFERENCES meta.model_run (model_run_id),
    model_code          text     NOT NULL,
    split               text     NOT NULL CHECK (split IN ('validation', 'test')),
    horizon             smallint NOT NULL CHECK (horizon BETWEEN 1 AND 14),
    n                   integer  NOT NULL,
    mae                 numeric  NOT NULL,
    rmse                numeric  NOT NULL,
    bias                numeric  NOT NULL,
    threshold_accuracy  numeric  NOT NULL,
    PRIMARY KEY (model_run_id, model_code, split, horizon)
);
COMMENT ON TABLE meta.model_metric IS 'Качество моделей на проверочном (validation) и тестовом (test) периодах';
COMMENT ON COLUMN meta.model_metric.horizon IS 'На сколько суток вперёд от последнего известного дня';
COMMENT ON COLUMN meta.model_metric.mae IS 'Средняя абсолютная ошибка, °C';
COMMENT ON COLUMN meta.model_metric.rmse IS 'Корень из средней квадратичной ошибки, °C';
COMMENT ON COLUMN meta.model_metric.bias IS 'Средняя ошибка (прогноз − факт), °C: больше нуля — прогноз завышает';
COMMENT ON COLUMN meta.model_metric.threshold_accuracy IS
    'Доля дней, где прогноз и факт одинаково попали выше или ниже порога отопления';

-- Прогнозы моделей. Выпущенный прогноз не меняется: повторный запуск за тот же день ничего не добавит
CREATE TABLE core.model_forecast (
    model_code    text        NOT NULL,
    issue_date    date        NOT NULL,
    target_date   date        NOT NULL,
    lead_days     smallint    NOT NULL CHECK (lead_days >= 0),
    temp_mean     numeric     NOT NULL,
    model_run_id  bigint      NOT NULL REFERENCES meta.model_run (model_run_id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model_code, issue_date, target_date)
);
COMMENT ON TABLE core.model_forecast IS
    'Прогнозы среднесуточной температуры собственными моделями (пишет команда forecast)';
COMMENT ON COLUMN core.model_forecast.issue_date IS 'Дата выпуска: следующий день после последнего известного факта';
