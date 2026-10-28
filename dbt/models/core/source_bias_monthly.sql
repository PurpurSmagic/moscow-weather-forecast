-- Станция ВДНХ в среднем теплее точки сетки Open-Meteo (городской остров тепла, разная
-- высота). Считаем это смещение по месяцам на днях, где есть оба источника, за
-- фиксированный опорный период — чтобы поправка не менялась при каждой загрузке.

select
    extract(month from s.obs_date)::smallint            as month,
    round(avg(s.temp_mean - o.temp_mean), 2)            as temp_mean_bias,
    round(coalesce(avg(s.temp_min - o.temp_min), 0), 2) as temp_min_bias,
    round(coalesce(avg(s.temp_max - o.temp_max), 0), 2) as temp_max_bias,
    count(*)                                            as days,
    '{{ var("bias_from") }}'::date                      as period_from,
    '{{ var("bias_to") }}'::date                        as period_to
from {{ ref('meteostat_daily') }} s
join {{ ref('openmeteo_daily') }} o using (obs_date)
where s.temp_mean is not null
  and o.temp_mean is not null
  and not s.is_model_temp
  and s.obs_date between '{{ var("bias_from") }}'::date and '{{ var("bias_to") }}'::date
group by 1
