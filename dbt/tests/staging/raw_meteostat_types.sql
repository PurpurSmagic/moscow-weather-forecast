with files as (
    select distinct on (year) year, columns, content
    from {{ source('raw', 'meteostat_file') }}
    where is_valid
    order by year, fetched_at desc, file_id desc
)

select f.year, u.name as column_name, u.value
from files f
cross join lateral string_to_table(f.content, E'\n') with ordinality as l (line, n)
cross join lateral unnest(f.columns, string_to_array(l.line, ',')) as u (name, value)
where l.n > 1
  and l.line <> ''
  and right(u.name, 7) <> '_source'
  and u.value <> ''
  and meta.try_numeric(u.value) is null
