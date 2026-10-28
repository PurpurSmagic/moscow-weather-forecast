-- Качество моделей по последней оценке (команда evaluate): ошибки по горизонтам прогноза
-- на проверочном (validation) и тестовом (test) периодах.

with last_run as (
    select model_run_id, finished_at, data_until, params
    from {{ source('meta', 'model_run') }}
    where kind = 'evaluate' and status = 'success'
    order by model_run_id desc
    limit 1
)

select
    m.model_code,
    m.split,
    m.horizon,
    m.n,
    m.mae,
    m.rmse,
    m.bias,
    m.threshold_accuracy,
    r.model_run_id,
    r.finished_at as evaluated_at,
    (r.params ->> 'validation_from')::date as validation_from,
    (r.params ->> 'test_from')::date as test_from,
    r.data_until
from {{ source('meta', 'model_metric') }} m
join last_run r using (model_run_id)
