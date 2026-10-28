-- Прогнозы в общую таблицу прогнозов (пока только Open-Meteo, потом добавятся свои модели).
-- Выпущенный прогноз не меняется, поэтому таблица только дополняется новыми выпусками.
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    indexes=[{'columns': ['model_code', 'issue_date', 'target_date'], 'unique': true}]
) }}

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
    f.response_id as source_response_id,
    now() as created_at,
    nullif({{ var('transform_run_id') }}, 0)::bigint as transform_run_id
from {{ ref('openmeteo_forecast') }} f
{% if is_incremental() %}
where not exists (
    select 1 from {{ this }} t
    where t.model_code = 'openmeteo' and t.issue_date = f.issue_date and t.target_date = f.target_date
)
{% endif %}
