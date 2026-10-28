select d::date as missing_date
from generate_series(
    '{{ var("history_start") }}'::date,
    (select max(obs_date) from {{ ref('fact_weather_daily') }} where valid_to is null),
    interval '1 day'
) as d

except

select obs_date
from {{ ref('fact_weather_daily') }}
where valid_to is null and station_id = '{{ var("station_id") }}'
