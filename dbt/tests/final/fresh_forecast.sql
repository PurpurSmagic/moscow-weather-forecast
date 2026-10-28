select max(issue_date) as last_issue
from {{ ref('fact_forecast') }}
where model_code = 'openmeteo'
having max(issue_date) is null or max(issue_date) < {{ today() }}
