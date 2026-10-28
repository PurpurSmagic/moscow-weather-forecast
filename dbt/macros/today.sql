{#- Сегодняшняя дата в часовом поясе проекта (а не сервера базы) -#}
{% macro today() -%}
    (now() at time zone '{{ var("timezone") }}')::date
{%- endmacro %}
