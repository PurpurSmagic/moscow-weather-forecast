-- Фактическая погода по дням для дашборда: норма, отклонение от нормы и признаки
-- для решений ЖКХ:
--   * cold_streak_days — сколько дней подряд среднесуточная ниже порога отопления;
--   * heating_condition_met — серия достигла нужной длины (по умолчанию 5 дней ниже +8 °C);
--   * ice_risk — переход через 0 °C при осадках или переохлаждённые осадки.
{{ config(indexes=[{'columns': ['obs_date'], 'unique': true}]) }}

with days as (
    select
        f.*,
        d.year, d.month, d.month_name, d.season, d.day_of_year,
        wc.description as weather_description,
        coalesce(wc.is_freezing, false) as is_freezing,
        f.temp_mean < {{ var("heating_threshold_c") }} as below
    from {{ ref('fact_weather_daily') }} f
    join {{ ref('dim_date') }} d on d.date_value = f.obs_date
    left join {{ ref('dim_weather_code') }} wc on wc.code = f.weather_code
    where f.valid_to is null  -- текущая версия записи
      and f.station_id = '{{ var("station_id") }}'
),

groups as (
    -- номер группы растёт на каждом «тёплом» дне: холодные дни подряд попадают в одну группу
    select days.*, sum(case when below then 0 else 1 end) over (order by obs_date) as grp
    from days
),

streaks as (
    select
        groups.*,
        case when below
             then count(*) filter (where below) over (partition by grp order by obs_date)
             else 0 end as cold_streak
    from groups
)

select
    s.obs_date, s.year, s.month, s.month_name, s.season, s.day_of_year,
    s.temp_mean, s.temp_min, s.temp_max, s.temp_source,
    n.temp_mean_norm as temp_norm,
    round(s.temp_mean - n.temp_mean_norm, 1) as temp_anomaly,
    s.precip_mm, s.snowfall_cm, s.snow_depth_cm, s.wind_speed_ms, s.wind_gust_ms,
    s.pressure_hpa, s.humidity_pct, s.cloud_cover_pct, s.weather_code, s.weather_description,
    s.below as below_heating_threshold,
    s.cold_streak::integer as cold_streak_days,
    s.cold_streak >= {{ var("heating_days") }} as heating_condition_met,
    s.temp_min < {{ var("ice_threshold_c") }} and s.temp_max > {{ var("ice_threshold_c") }} as zero_crossing,
    (s.temp_min < {{ var("ice_threshold_c") }} and s.temp_max > {{ var("ice_threshold_c") }}
     and coalesce(s.precip_mm, 0) > 0) or s.is_freezing as ice_risk
from streaks s
left join {{ ref('climate_norm') }} n on n.day_of_year = s.day_of_year
