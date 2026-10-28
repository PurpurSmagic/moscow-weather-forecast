{#- Схема берётся ровно такой, как указана в настройках модели (staging, core, mart),
    без приставки схемы по умолчанию, как сделал бы dbt -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
