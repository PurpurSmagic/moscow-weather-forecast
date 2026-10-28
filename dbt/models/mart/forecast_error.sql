-- Сравнение прогнозов с фактом: каждый прогноз, день которого уже наступил.
-- К прогнозу Open-Meteo добавляется та же поправка на станцию, что и в forecast_latest,
-- чтобы сравнение с собственными моделями было честным.
{{ config(indexes=[{'columns': ['model_code', 'issue_date', 'target_date'], 'unique': true}]) }}

with forecasts as (
    select
        f.model_code,
        f.issue_date,
        f.target_date,
        f.lead_days,
        round(f.temp_mean + case when f.model_code = 'openmeteo' then coalesce(b.temp_mean_bias, 0) else 0 end, 1)
            as forecast_temp
    from {{ ref('fact_forecast') }} f
    left join {{ ref('source_bias_monthly') }} b on b.month = extract(month from f.target_date)
    where f.temp_mean is not null
)

select
    f.model_code,
    f.issue_date,
    f.target_date,
    f.lead_days,
    f.forecast_temp,
    a.temp_mean as actual_temp,
    round(f.forecast_temp - a.temp_mean, 1) as error,
    abs(round(f.forecast_temp - a.temp_mean, 1)) as abs_error,
    (f.forecast_temp < {{ var("heating_threshold_c") }}) = (a.temp_mean < {{ var("heating_threshold_c") }})
        as threshold_correct
from forecasts f
join {{ ref('fact_weather_daily') }} a
  on a.obs_date = f.target_date
 and a.valid_to is null
 and a.station_id = '{{ var("station_id") }}'
