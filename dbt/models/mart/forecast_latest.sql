-- Последний выпуск прогноза каждой модели с теми же признаками, что и у факта.
-- Серия холодных дней продолжает фактическую: если вчера была 3-я холодная
-- подряд, а сегодня по прогнозу тоже холодно — это уже 4-й день.
-- Прогноз Open-Meteo считается для точки сетки, а факт — по станции, которая теплее.
-- Поэтому к температуре прогноза Open-Meteo добавляем ту же поправку по месяцам,
-- что и при заполнении пропусков станции.
{{ config(indexes=[{'columns': ['model_code', 'target_date'], 'unique': true}]) }}

with last_actual as (
    select obs_date, cold_streak_days from {{ ref('weather_daily') }} order by obs_date desc limit 1
),

latest as (
    -- последний выпуск каждой модели, только даты после последнего факта
    select
        f.model_code, f.issue_date, f.target_date, f.lead_days, f.precip_mm, f.precip_prob_pct, f.weather_code,
        round(f.temp_mean + c.bias_mean, 1) as temp_mean,
        round(f.temp_min + c.bias_min, 1) as temp_min,
        round(f.temp_max + c.bias_max, 1) as temp_max,
        c.bias_mean as bias_correction
    from {{ ref('fact_forecast') }} f
    left join {{ ref('source_bias_monthly') }} b on b.month = extract(month from f.target_date)
    cross join lateral (
        select
            case when f.model_code = 'openmeteo' then coalesce(b.temp_mean_bias, 0) else 0 end as bias_mean,
            case when f.model_code = 'openmeteo' then coalesce(b.temp_min_bias, 0) else 0 end as bias_min,
            case when f.model_code = 'openmeteo' then coalesce(b.temp_max_bias, 0) else 0 end as bias_max
    ) c
    where f.issue_date = (select max(x.issue_date) from {{ ref('fact_forecast') }} x where x.model_code = f.model_code)
      and f.target_date > coalesce((select obs_date from last_actual), '-infinity'::date)
),

days as (
    select
        l.*,
        d.day_of_year,
        wc.description as weather_description,
        coalesce(wc.is_freezing, false) as is_freezing,
        l.temp_mean < {{ var("heating_threshold_c") }} as below,
        -- продолжает ли прогноз фактический ряд без разрыва
        min(l.target_date) over (partition by l.model_code) = (select obs_date from last_actual) + 1 as continues
    from latest l
    join {{ ref('dim_date') }} d on d.date_value = l.target_date
    left join {{ ref('dim_weather_code') }} wc on wc.code = l.weather_code
),

groups as (
    select days.*, sum(case when below then 0 else 1 end) over (partition by model_code order by target_date) as grp
    from days
),

streaks as (
    select
        groups.*,
        case when below
             then count(*) filter (where below) over (partition by model_code, grp order by target_date)
                  + case when grp = 0 and continues
                         then coalesce((select cold_streak_days from last_actual), 0) else 0 end
             else 0 end as cold_streak
    from groups
)

select
    s.model_code, s.issue_date, s.target_date, s.lead_days, s.temp_mean, s.temp_min, s.temp_max,
    s.bias_correction,
    n.temp_mean_norm as temp_norm,
    round(s.temp_mean - n.temp_mean_norm, 1) as temp_anomaly,
    s.precip_mm, s.precip_prob_pct, s.weather_description,
    s.below as below_heating_threshold,
    s.cold_streak::integer as cold_streak_days,
    s.cold_streak >= {{ var("heating_days") }} as heating_condition_met,
    s.temp_min < {{ var("ice_threshold_c") }} and s.temp_max > {{ var("ice_threshold_c") }} as zero_crossing,
    (s.temp_min < {{ var("ice_threshold_c") }} and s.temp_max > {{ var("ice_threshold_c") }}
     and coalesce(s.precip_mm, 0) > 0) or s.is_freezing as ice_risk
from streaks s
left join {{ ref('climate_norm') }} n on n.day_of_year = s.day_of_year
