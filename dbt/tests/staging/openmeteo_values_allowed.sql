select obs_date, humidity_pct, cloud_cover_pct, wind_dir_deg, precip_mm, snowfall_cm,
       wind_speed_ms, pressure_hpa
from {{ ref('openmeteo_daily') }}
where humidity_pct not between 0 and 100
   or cloud_cover_pct not between 0 and 100
   or wind_dir_deg not between 0 and 360
   or precip_mm < 0 or snowfall_cm < 0 or wind_speed_ms < 0
   or pressure_hpa not between 940 and 1070
