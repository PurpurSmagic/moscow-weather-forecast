-- Климатическая норма: среднее за базовый период (по умолчанию 1991–2020, как у ВМО)
-- для каждого дня года. Чтобы норма не «прыгала» от дня к дню, берём окно ±7 дней.
{{ config(indexes=[{'columns': ['day_of_year'], 'unique': true}]) }}

with base as (
    select
        extract(doy from obs_date)::int as doy,
        extract(year from obs_date)::int as year,
        temp_mean, temp_min, temp_max, precip_mm
    from {{ ref('fact_weather_daily') }}
    where valid_to is null  -- текущая версия записи
      and station_id = '{{ var("station_id") }}'
      and extract(year from obs_date) between {{ var("norm_from_year") }} and {{ var("norm_to_year") }}
)

select
    d.doy::smallint                 as day_of_year,
    round(avg(b.temp_mean), 1)      as temp_mean_norm,
    round(avg(b.temp_min), 1)       as temp_min_norm,
    round(avg(b.temp_max), 1)       as temp_max_norm,
    round(stddev(b.temp_mean), 1)   as temp_mean_std,
    round(avg(b.precip_mm), 1)      as precip_norm_mm,
    count(distinct b.year)::smallint as years,
    '{{ var("norm_from_year") }}–{{ var("norm_to_year") }}'::text as period
from generate_series(1, 366) as d (doy)
join base b on least(abs(b.doy - d.doy), 366 - abs(b.doy - d.doy)) <= 7
group by d.doy
