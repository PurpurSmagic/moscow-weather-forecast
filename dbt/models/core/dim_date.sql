-- Календарь от начала истории до месяца вперёд.
{{ config(indexes=[{'columns': ['date_value'], 'unique': true}]) }}

select
    d::date                                   as date_value,
    extract(year from d)::smallint            as year,
    extract(quarter from d)::smallint         as quarter,
    extract(month from d)::smallint           as month,
    (array['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август',
           'сентябрь', 'октябрь', 'ноябрь', 'декабрь'])[extract(month from d)::int] as month_name,
    extract(day from d)::smallint             as day,
    extract(doy from d)::smallint             as day_of_year,
    extract(week from d)::smallint            as iso_week,
    extract(isodow from d)::smallint          as day_of_week,  -- 1 = понедельник
    extract(isodow from d) in (6, 7)          as is_weekend,
    case
        when extract(month from d) in (12, 1, 2) then 'зима'
        when extract(month from d) in (3, 4, 5) then 'весна'
        when extract(month from d) in (6, 7, 8) then 'лето'
        else 'осень'
    end                                       as season
from generate_series(
    least(
        '{{ var("history_start") }}'::date,
        (select min(obs_date) from {{ ref('openmeteo_daily') }}),
        (select min(obs_date) from {{ ref('meteostat_daily') }})
    ),
    greatest(current_date + 31, (select max(target_date) from {{ ref('openmeteo_forecast') }})),
    interval '1 day'
) as d
