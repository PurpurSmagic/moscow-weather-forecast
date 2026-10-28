select r.response_id, d.key as variable, e.value
from {{ source('raw', 'openmeteo_archive') }} r
cross join lateral jsonb_each(r.payload -> 'daily') as d (key, value)
cross join lateral jsonb_array_elements(d.value) as e (value)
where r.is_valid
  and d.key <> 'time'
  and jsonb_typeof(e.value) not in ('number', 'null')
