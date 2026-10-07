-- V008. Контроль качества данных: результаты проверок и карантин.

-- Результаты проверок. Правила описаны в config/dq_rules.yaml; здесь — что показал
-- каждый запуск каждого правила.
CREATE TABLE meta.dq_result (
    result_id         bigserial   PRIMARY KEY,
    transform_run_id  bigint      REFERENCES meta.transform_run (transform_run_id),
    checked_at        timestamptz NOT NULL DEFAULT now(),
    rule_code         text        NOT NULL,
    check_type        text        NOT NULL,
    phase             text        NOT NULL CHECK (phase IN ('staging', 'final')),
    target_table      text        NOT NULL,
    severity          text        NOT NULL CHECK (severity IN ('warning', 'quarantine', 'critical')),
    status            text        NOT NULL CHECK (status IN ('passed', 'failed', 'error')),
    failed_rows       integer     NOT NULL DEFAULT 0 CHECK (failed_rows >= 0),
    sample            jsonb       NOT NULL DEFAULT '[]'::jsonb,
    message           text
);
CREATE INDEX ix_dq_result_rule ON meta.dq_result (rule_code, checked_at DESC);
CREATE INDEX ix_dq_result_run ON meta.dq_result (transform_run_id);

COMMENT ON TABLE meta.dq_result IS 'Результаты проверок качества данных (правила — в config/dq_rules.yaml)';
COMMENT ON COLUMN meta.dq_result.transform_run_id IS 'Запуск преобразования; пусто — проверка запускалась отдельно (команда dq)';
COMMENT ON COLUMN meta.dq_result.status IS
    'passed — нарушений нет; failed — найдены нарушения; error — сама проверка не выполнилась';
COMMENT ON COLUMN meta.dq_result.sample IS 'До 5 примеров строк-нарушений';

-- Карантин: строки staging, не прошедшие проверки с уровнем quarantine.
-- В core они не попадают; хранится состояние последнего запуска преобразования.
CREATE TABLE staging.quarantine (
    quarantine_id     bigserial   PRIMARY KEY,
    transform_run_id  bigint      NOT NULL REFERENCES meta.transform_run (transform_run_id),
    rule_code         text        NOT NULL,
    source_table      text        NOT NULL,
    row_key           text        NOT NULL,
    row_data          jsonb       NOT NULL,
    detected_at       timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE staging.quarantine IS
    'Строки, убранные проверками качества: какое правило сработало и сама строка целиком';
