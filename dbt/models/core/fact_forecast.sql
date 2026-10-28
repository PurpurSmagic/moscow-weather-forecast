-- Все прогнозы в одной таблице: Open-Meteo и собственные модели (команда forecast).
-- Выпущенный прогноз не меняется, поэтому таблица только дополняется новыми выпусками.
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    indexes=[{'columns': ['model_code', 'issue_date', 'target_date'], 'unique': true}]
) }}

with forecasts as (
    select
        'openmeteo'::text as model_code,
        f.issue_date,
        f.target_date,
        f.lead_days,
        f.temp_mean,
        f.temp_min,
        f.temp_max,
        f.precip_mm,
        f.precip_prob_pct,
        (select c.code from {{ ref('dim_weather_code') }} c where c.code = f.weather_code) as weather_code,
        f.response_id as source_response_id
    from {{ ref('openmeteo_forecast') }} f

    union all

    -- собственные модели прогнозируют только среднесуточную температуру
    select
        m.model_code,
        m.issue_date,
        m.target_date,
        m.lead_days,
        m.temp_mean,
        null::numeric,
        null::numeric,
        null::numeric,
        null::smallint,
        null::smallint,
        null::bigint
    from {{ source('forecast', 'model_forecast') }} m
)

select
    f.*,
    now() as created_at,
    nullif({{ var('transform_run_id') }}, 0)::bigint as transform_run_id
from forecasts f
{% if is_incremental() %}
where not exists (
    select 1 from {{ this }} t
    where t.model_code = f.model_code and t.issue_date = f.issue_date and t.target_date = f.target_date
)
{% endif %}
