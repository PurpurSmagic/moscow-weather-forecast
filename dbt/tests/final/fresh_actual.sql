select max(obs_date) as last_date
from {{ ref('fact_weather_daily') }}
where valid_to is null
having max(obs_date) is null or max(obs_date) < {{ today() }} - 2
