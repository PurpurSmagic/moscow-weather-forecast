select model_code, count(distinct issue_date) as issues, count(*) as rows, count(distinct target_date) as dates
from {{ ref('forecast_latest') }}
group by model_code
having count(distinct issue_date) > 1 or count(*) <> count(distinct target_date)
