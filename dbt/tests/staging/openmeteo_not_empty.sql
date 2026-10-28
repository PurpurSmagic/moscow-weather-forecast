select 'staging.openmeteo_daily пуста' as problem
where not exists (select 1 from {{ ref('openmeteo_daily') }})
