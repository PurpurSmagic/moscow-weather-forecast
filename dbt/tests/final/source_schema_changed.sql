select s.source_code, s.first_seen_at, s.fields
from {{ source('meta', 'source_schema') }} s
where s.first_seen_at > now() - interval '7 days'
  and s.first_seen_at > (
      select min(o.first_seen_at) + interval '1 day'
      from {{ source('meta', 'source_schema') }} o
      where o.source_code = s.source_code
  )
