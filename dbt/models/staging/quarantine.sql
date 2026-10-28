-- Карантин: строки источников, не прошедшие проверки уровня quarantine.
-- В таблицы staging (и дальше в core) такие строки не попадают — день заполняется
-- из другого источника. Тесты с теми же названиями (tests/staging) показывают,
-- сколько строк попало в карантин.

with openmeteo as (
    select * from {{ ref('parsed_openmeteo_daily') }}
),

meteostat as (
    select * from {{ ref('parsed_meteostat_daily') }}
),

bad as (
    select 'openmeteo_temp_range' as rule_code, 'staging.openmeteo_daily' as source_table,
           obs_date, to_jsonb(openmeteo) as row_data
    from openmeteo
    where temp_mean not between -50 and 45 or temp_min < -55 or temp_max > 50

    union all

    select 'openmeteo_temp_order', 'staging.openmeteo_daily', obs_date, to_jsonb(openmeteo)
    from openmeteo
    where temp_min > temp_mean + 0.5 or temp_mean > temp_max + 0.5

    union all

    select 'meteostat_temp_range', 'staging.meteostat_daily', obs_date, to_jsonb(meteostat)
    from meteostat
    where temp_mean not between -50 and 45 or temp_min < -55 or temp_max > 50

    union all

    select 'meteostat_temp_order', 'staging.meteostat_daily', obs_date, to_jsonb(meteostat)
    from meteostat
    where temp_min > temp_mean + 0.5 or temp_mean > temp_max + 0.5
)

select
    row_number() over (order by source_table, obs_date, rule_code) as quarantine_id,
    nullif({{ var('transform_run_id') }}, 0)::bigint as transform_run_id,
    rule_code,
    source_table,
    obs_date::text as row_key,
    row_data,
    now() as detected_at
from bad
