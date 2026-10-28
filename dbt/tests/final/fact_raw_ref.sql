select f.obs_date, f.openmeteo_response_id, f.meteostat_file_id
from {{ ref('fact_weather_daily') }} f
left join {{ source('raw', 'openmeteo_archive') }} a on a.response_id = f.openmeteo_response_id
left join {{ source('raw', 'meteostat_file') }} m on m.file_id = f.meteostat_file_id
where f.valid_to is null
  and ((f.openmeteo_response_id is null and f.meteostat_file_id is null)
       or (f.openmeteo_response_id is not null and a.response_id is null)
       or (f.meteostat_file_id is not null and m.file_id is null)
       or a.is_valid = false or m.is_valid = false)
