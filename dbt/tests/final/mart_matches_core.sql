select c.obs_date, 'нет в витрине' as problem
from {{ ref('fact_weather_daily') }} c
where c.valid_to is null and c.station_id = '{{ var("station_id") }}'
  and not exists (select 1 from {{ ref('weather_daily') }} m where m.obs_date = c.obs_date)

union all

select m.obs_date, 'нет в core'
from {{ ref('weather_daily') }} m
where not exists (
    select 1 from {{ ref('fact_weather_daily') }} c
    where c.valid_to is null and c.obs_date = m.obs_date and c.station_id = '{{ var("station_id") }}'
)
