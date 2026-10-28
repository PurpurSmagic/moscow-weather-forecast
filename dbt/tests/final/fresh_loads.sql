select source_code, max(finished_at) filter (where status in ('success', 'skipped')) as last_success
from {{ source('meta', 'load_log') }}
group by source_code
having coalesce(max(finished_at) filter (where status in ('success', 'skipped')), '-infinity')
       < now() - interval '2 days'
