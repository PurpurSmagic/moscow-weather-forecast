select obs_date, humidity_pct, cloud_cover_okta, precip_mm, snow_depth_cm, pressure_hpa
from {{ ref('meteostat_daily') }}
where humidity_pct not between 0 and 100
   or cloud_cover_okta not between 0 and 8
   or precip_mm not between 0 and 300
   or snow_depth_cm not between 0 and 150
   or pressure_hpa not between 940 and 1070
