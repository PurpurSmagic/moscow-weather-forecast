select model_code, issue_date, target_date, temp_mean
from {{ source('forecast', 'model_forecast') }}
where temp_mean not between -50 and 45
