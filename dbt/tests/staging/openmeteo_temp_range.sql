-- строки, которые это правило убрало в карантин (условие — в models/staging/quarantine.sql)
select
    row_key as obs_date,
    row_data ->> 'temp_min' as temp_min,
    row_data ->> 'temp_mean' as temp_mean,
    row_data ->> 'temp_max' as temp_max
from {{ ref('quarantine') }}
where rule_code = 'openmeteo_temp_range'
