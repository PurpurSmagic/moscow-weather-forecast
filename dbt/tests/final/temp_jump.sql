select obs_date, prev_temp, temp_mean
from (
    select obs_date, temp_mean, lag(temp_mean) over (order by obs_date) as prev_temp
    from {{ ref('fact_weather_daily') }}
    where valid_to is null and station_id = '{{ var("station_id") }}'
) x
where abs(temp_mean - prev_temp) > 15
