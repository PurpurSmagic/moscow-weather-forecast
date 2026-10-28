select f.obs_date, f.temp_mean_station, f.temp_mean_openmeteo, b.temp_mean_bias
from {{ ref('fact_weather_daily') }} f
join {{ ref('source_bias_monthly') }} b on b.month = extract(month from f.obs_date)
where f.valid_to is null
  and f.temp_source = 'station'
  and abs(f.temp_mean_station - f.temp_mean_openmeteo - b.temp_mean_bias) > 5
