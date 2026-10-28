-- V009. Обработка данных переведена на dbt (каталог dbt/).
--
-- Таблицы слоёв staging, core и mart теперь создаёт и обновляет dbt, поэтому таблицы,
-- созданные миграциями V004–V008, удаляются. Это производные данные: при следующем запуске
-- `python -m weather transform` dbt соберёт их заново из raw. История версий в core
-- (core.fact_weather_daily, core.dim_station) начинается заново — её ведут снапшоты dbt.
-- Слой raw и служебные таблицы meta не меняются.

DROP TABLE IF EXISTS
    mart.forecast_latest,
    mart.weather_daily,
    mart.climate_norm,
    core.fact_forecast,
    core.fact_weather_daily,
    core.source_bias_monthly,
    core.dim_station,
    core.dim_weather_code,
    core.dim_date,
    staging.quarantine,
    staging.openmeteo_forecast,
    staging.meteostat_daily,
    staging.openmeteo_daily
    CASCADE;

-- Журнал преобразований: шаг — это модель, справочник (seed) или снапшот dbt.
-- Если шаг упал, зависящие от него шаги dbt не выполняет — они записываются как skipped.
ALTER TABLE meta.transform_step DROP CONSTRAINT transform_step_status_check;
ALTER TABLE meta.transform_step
    ADD CONSTRAINT transform_step_status_check CHECK (status IN ('success', 'failed', 'skipped'));
ALTER TABLE meta.transform_step ADD COLUMN node_type text;
COMMENT ON TABLE meta.transform_step IS
    'Шаги преобразования (узлы dbt): какую таблицу построил шаг, сколько в ней строк после шага';
COMMENT ON COLUMN meta.transform_step.node_type IS 'model — модель, seed — справочник из CSV, snapshot — история изменений';

-- Результаты проверок: проверки теперь — тесты dbt. Тест, который не выполнялся
-- из-за ошибки на предыдущем шаге, записывается как skipped.
ALTER TABLE meta.dq_result DROP CONSTRAINT dq_result_status_check;
ALTER TABLE meta.dq_result
    ADD CONSTRAINT dq_result_status_check CHECK (status IN ('passed', 'failed', 'error', 'skipped'));
COMMENT ON TABLE meta.dq_result IS
    'Результаты проверок качества данных (тесты dbt: dbt/tests и описания моделей dbt/models)';
COMMENT ON COLUMN meta.dq_result.status IS
    'passed — нарушений нет; failed — найдены нарушения; error — сама проверка не выполнилась; '
    'skipped — не выполнялась из-за ошибки на предыдущем шаге';
