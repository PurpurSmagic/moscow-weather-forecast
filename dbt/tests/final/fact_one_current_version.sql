select station_id, obs_date, count(*) as versions
from {{ ref('fact_weather_daily') }}
where valid_to is null
group by station_id, obs_date
having count(*) > 1
