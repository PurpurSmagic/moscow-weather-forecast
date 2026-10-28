-- Open-Meteo, история по дням: разобранные данные без строк из карантина.
{{ config(indexes=[{'columns': ['obs_date'], 'unique': true}]) }}

select p.*
from {{ ref('parsed_openmeteo_daily') }} p
where not exists (
    select 1
    from {{ ref('quarantine') }} q
    where q.source_table = 'staging.openmeteo_daily' and q.row_key = p.obs_date::text
)
