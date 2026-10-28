-- Сведение двух источников в одну запись на дату («золотая запись»).
-- История изменений этих записей хранится в снапшоте core.fact_weather_daily.
--
-- Правила сведения:
--   * температура — со станции (Meteostat), если там есть наблюдение;
--     если нет (пропуск или только модель DWD MOSMIX) — из Open-Meteo с поправкой
--     на среднее смещение станции за этот месяц (core.source_bias_monthly);
--   * осадки — со станции, если есть наблюдение, иначе из Open-Meteo;
--   * остальное (ветер, давление, облачность, снег и т. д.) — из Open-Meteo:
--     у станции эти данные есть не за все годы.
{{ config(materialized='view') }}

with joined as (
    select
        coalesce(o.obs_date, s.obs_date) as obs_date,
        o.temp_mean, o.temp_min, o.temp_max, o.precip_mm, o.rain_mm, o.snowfall_cm, o.snow_depth_cm,
        o.wind_speed_ms, o.wind_gust_ms, o.wind_dir_deg, o.pressure_hpa, o.humidity_pct,
        o.cloud_cover_pct, o.dew_point_c, o.radiation_mj_m2, o.weather_code, o.response_id,
        s.temp_mean as s_temp_mean,
        s.temp_min as s_temp_min,
        s.temp_max as s_temp_max,
        s.precip_mm as s_precip_mm,
        s.precip_source as s_precip_source,
        s.file_id as s_file_id,
        (s.temp_mean is not null and not s.is_model_temp) as station_ok,
        b.temp_mean_bias,
        b.temp_min_bias,
        b.temp_max_bias
    from {{ ref('openmeteo_daily') }} o
    full join {{ ref('meteostat_daily') }} s on s.obs_date = o.obs_date
    left join {{ ref('source_bias_monthly') }} b on b.month = extract(month from coalesce(o.obs_date, s.obs_date))
),

golden as (
    select
        '{{ var("station_id") }}'::text as station_id,
        (select st.station_key from {{ ref('dim_station') }} st
         where st.station_id = '{{ var("station_id") }}' and st.valid_to is null) as station_key,
        j.obs_date,
        case when station_ok then s_temp_mean
             else round(j.temp_mean + coalesce(temp_mean_bias, 0), 1) end as temp_mean,
        case when station_ok and s_temp_min is not null then s_temp_min
             else round(j.temp_min + coalesce(temp_min_bias, 0), 1) end as temp_min,
        case when station_ok and s_temp_max is not null then s_temp_max
             else round(j.temp_max + coalesce(temp_max_bias, 0), 1) end as temp_max,
        case when station_ok then 'station' else 'openmeteo_adjusted' end as temp_source,
        s_temp_mean as temp_mean_station,
        j.temp_mean as temp_mean_openmeteo,
        case when s_precip_mm is not null and coalesce(s_precip_source, '') <> 'dwd_mosmix' then s_precip_mm
             else j.precip_mm end as precip_mm,
        case when s_precip_mm is not null and coalesce(s_precip_source, '') <> 'dwd_mosmix' then 'station'
             when j.precip_mm is not null then 'openmeteo' end as precip_source,
        j.rain_mm,
        j.snowfall_cm,
        j.snow_depth_cm,
        j.wind_speed_ms,
        j.wind_gust_ms,
        j.wind_dir_deg,
        j.pressure_hpa,
        j.humidity_pct,
        j.cloud_cover_pct,
        j.dew_point_c,
        j.radiation_mj_m2,
        (select c.code from {{ ref('dim_weather_code') }} c where c.code = j.weather_code) as weather_code,
        j.response_id as openmeteo_response_id,
        s_file_id as meteostat_file_id
    from joined j
)

select
    g.*,
    -- по хешу значимых полей снапшот понимает, изменилась ли запись
    md5(concat_ws('|', g.temp_mean, g.temp_min, g.temp_max, g.temp_source, g.temp_mean_station,
                  g.temp_mean_openmeteo, g.precip_mm, g.precip_source, g.rain_mm, g.snowfall_cm,
                  g.snow_depth_cm, g.wind_speed_ms, g.wind_gust_ms, g.wind_dir_deg, g.pressure_hpa,
                  g.humidity_pct, g.cloud_cover_pct, g.dew_point_c, g.radiation_mj_m2, g.weather_code)) as row_hash,
    nullif({{ var('transform_run_id') }}, 0)::bigint as transform_run_id
from golden g
where g.temp_mean is not null
  and g.obs_date >= '{{ var("history_start") }}'::date
