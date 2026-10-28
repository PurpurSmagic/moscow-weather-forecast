-- Описание станции из настроек (config/settings.yaml). История его изменений
-- хранится в снапшоте core.dim_station.
{{ config(materialized='view') }}

select
    '{{ var("station_id") }}'::text                                        as station_id,
    '{{ var("station_id") }}'::text                                        as wmo_id,
    '{{ var("station_name") | replace("'", "''") }}'::text                 as name,
    round({{ var("station_latitude") }}::numeric, 4)                       as latitude,
    round({{ var("station_longitude") }}::numeric, 4)                      as longitude,
    round({{ var("station_elevation_m") }}::numeric, 1)                    as elevation_m
