"""Две панели Streamlit. Данные читаются из PostgreSQL при открытии и раз в минуту."""

from datetime import timedelta
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import psycopg
import streamlit as st

from weather import db
from weather.config import load_settings, today_in

MODELS = {
    "lightgbm": "LightGBM",
    "ridge": "Ridge",
    "openmeteo": "Open-Meteo",
    "persistence": "Как вчера",
    "climatology": "Климатическая норма",
}
SOURCES = {
    "meteostat_daily": "Meteostat · станция",
    "openmeteo_archive": "Open-Meteo · архив",
    "openmeteo_forecast": "Open-Meteo · прогноз",
}
STATUS = {
    "success": "Успешно",
    "skipped": "Пропущено",
    "failed": "Ошибка",
    "partial": "Частично",
    "error": "Ошибка",
    "running": "В работе",
    "passed": "Пройдена",
}


def read(conn, sql, args=()):
    with conn.cursor() as cursor:
        cursor.execute(sql, args)
        return pd.DataFrame.from_records(
            cursor.fetchall(),
            columns=[c.name for c in cursor.description],
            coerce_float=True,
        )


def display(frame, names):
    st.dataframe(frame.rename(columns=names), hide_index=True, width="stretch")


def temperature_chart(history, forecast):
    past = history.rename(
        columns={"obs_date": "date", "temp_mean": "Наблюдение", "temp_norm": "Норма"}
    )
    past = past.melt(
        id_vars="date",
        value_vars=["Наблюдение", "Норма"],
        var_name="Ряд",
        value_name="Температура",
    )
    future = forecast.rename(
        columns={"target_date": "date", "temp_mean": "Температура", "model_code": "Ряд"}
    )
    chart_data = pd.concat(
        [past, future[["date", "Температура", "Ряд"]]], ignore_index=True
    )
    chart = (
        alt.Chart(chart_data)
        .mark_line(point=True)
        .encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%d.%m")),
            y=alt.Y("Температура:Q", title="°C", scale=alt.Scale(zero=False)),
            color=alt.Color(
                "Ряд:N",
                scale=alt.Scale(
                    domain=["Наблюдение", "Норма", *MODELS.values()],
                    range=["#186469", "#757575", "#c04c27", "#514493", "#217bb4", "#78572f", "#a74472"],
                ),
                legend=alt.Legend(orient="bottom", title=None, values=chart_data["Ряд"].unique().tolist()),
            ),
            tooltip=[
                alt.Tooltip("date:T", title="Дата", format="%d.%m.%Y"),
                "Ряд:N",
                alt.Tooltip("Температура:Q", title="°C", format=".1f"),
            ],
        )
    )
    st.altair_chart(chart.properties(height=260), width="stretch")


def weather(conn, settings):
    st.title("Погода в Москве")
    last = conn.execute("select max(obs_date) from mart.weather_daily").fetchone()[0]
    if last is None:
        st.info("Ожидаем первую загрузку наблюдений.")
        return
    st.caption(
        f"{settings.station.name} · станция {settings.station.wmo_id} · наблюдения по {last:%d.%m.%Y}"
    )
    if (today_in(settings.timezone) - last).days > 2:
        st.warning(
            "Наблюдения старше двух суток. Проверьте загрузки на панели «Работа системы»."
        )
    latest = read(
        conn, "select * from mart.weather_daily where obs_date = %s", (last,)
    ).iloc[0]
    threshold = settings.heating.threshold_c
    left, middle, right = st.columns(3)
    left.metric("Средняя за сутки", f"{latest.temp_mean:+.1f} °C" if pd.notna(latest.temp_mean) else "—")
    middle.metric(
        "Отклонение от нормы", f"{latest.temp_anomaly:+.1f} °C" if pd.notna(latest.temp_anomaly) else "—"
    )
    right.metric(f"Дней подряд ниже {threshold:g} °C", int(latest.cold_streak_days))

    st.subheader("Наблюдения и прогноз")
    forecast = read(
        conn,
        "select * from mart.forecast_latest where target_date between %s and %s "
        "order by target_date, model_code",
        (last + timedelta(days=1), last + timedelta(days=settings.forecast_horizon_days)),
    )
    models = [key for key in MODELS if key in forecast.model_code.unique()]
    span, choice = st.columns([1, 2])
    days = span.selectbox("История, суток", [7, 14, 30, 90], index=1)
    selected = choice.multiselect(
        "Модели на графике",
        models,
        default=[key for key in ("lightgbm", "openmeteo") if key in models] or models[:1],
        format_func=MODELS.get,
    )
    history = read(
        conn,
        "select obs_date, temp_mean, temp_norm from mart.weather_daily "
        "where obs_date between %s and %s order by obs_date",
        (last - timedelta(days=days - 1), last),
    )
    chosen = forecast[forecast.model_code.isin(selected)].copy()
    chosen["model_code"] = chosen.model_code.map(MODELS)
    temperature_chart(history, chosen)

    st.subheader("Прогноз для ЖКХ")
    if models:
        model = st.selectbox(
            "Модель для условия отопления", models, format_func=MODELS.get
        )
        group = forecast[forecast.model_code == model]
        st.caption(
            f"Выпуск {group.issue_date.iloc[0]:%d.%m.%Y} · прогноз с "
            f"{group.target_date.min():%d.%m} по {group.target_date.max():%d.%m.%Y}"
        )
        heating = group.loc[group.heating_condition_met, "target_date"]
        ice = forecast[forecast.model_code == "openmeteo"]
        ice_dates = ice.loc[ice.ice_risk.eq(True), "target_date"]
        one, two = st.columns(2)
        one.markdown("**Условие отопления**")
        one.write(
            f"Первый день в прогнозе с серией от {settings.heating.consecutive_days} суток "
            f"ниже {threshold:g} °C — {heating.iloc[0]:%d.%m}."
            if len(heating)
            else "За горизонт выбранной модели порог серии не достигнут."
        )
        two.markdown("**Риск гололёда · Open-Meteo**")
        two.write(
            ", ".join(d.strftime("%d.%m") for d in ice_dates)
            if len(ice_dates)
            else (
                "В выпуске нет дней с признаком риска."
                if len(ice)
                else "Для этих дат нет прогноза Open-Meteo."
            )
        )
        st.caption(
            "Для гололёда учитываются переход через 0 °C с осадками и переохлаждённые осадки."
        )
        display(
            group[["issue_date", "target_date", "temp_mean", "cold_streak_days"]],
            {
                "issue_date": "Выпуск",
                "target_date": "Дата",
                "temp_mean": "Средняя, °C",
                "cold_streak_days": "Холодных дней подряд",
            },
        )
    else:
        st.info("Для следующих дат прогноз пока отсутствует.")

    with st.expander("Точность прогноза"):
        metrics = read(
            conn,
            "select horizon, model_code, mae from mart.model_quality "
            "where split='test' order by horizon, model_code",
        )
        if len(metrics):
            st.caption(
                "Средняя абсолютная ошибка на тестовом периоде, °C. "
                "Меньшее значение означает более точный прогноз."
            )
            st.dataframe(
                metrics.pivot(index="horizon", columns="model_code", values="mae")
                .rename(columns=MODELS)
                .rename_axis("Горизонт, суток"),
                width="stretch",
            )
        live = read(
            conn,
            "select model_code, count(*) as n, round(avg(abs_error),2) as mae "
            "from mart.forecast_error group by model_code order by model_code",
        )
        st.caption(
            "Сохранённые выпуски, для которых уже появился факт. Модели могут иметь разное число наблюдений."
        )
        live["model_code"] = live.model_code.map(MODELS)
        display(
            live,
            {
                "model_code": "Источник прогноза",
                "n": "Оценено прогнозов",
                "mae": "MAE, °C",
            },
        )


def operations(conn, settings):
    st.title("Работа системы")
    st.caption(f"Загрузки, обработка и проверки качества · {settings.timezone}")
    health = read(
        conn,
        """select distinct on (s.source_code) s.source_code, l.status, w.loaded_until, l.finished_at
           from meta.source s
           left join meta.load_log l using (source_code)
           left join meta.watermark w using (source_code)
           order by s.source_code, l.load_id desc""",
    )
    checks = read(
        conn,
        """select distinct on (rule_code) rule_code, target_table, severity, status, failed_rows
           from meta.dq_result order by rule_code, result_id desc""",
    ).sort_values(["failed_rows", "rule_code"], ascending=[False, True])
    last = conn.execute("select max(obs_date) from mart.weather_daily").fetchone()[0]
    a, b, c = st.columns(3)
    a.metric(
        "Источники без ошибки",
        f"{health.status.isin(['success', 'skipped']).sum()} из {len(health)}",
    )
    b.metric(
        "Правила с нарушениями", int(checks.status.isin(["failed", "error"]).sum())
    )
    c.metric("Последний факт", last.strftime("%d.%m.%Y") if last else "—")
    st.subheader("Актуальность источников")
    health["source_code"] = health.source_code.map(SOURCES)
    health["status"] = health.status.map(STATUS).fillna("Загрузок пока нет")
    health["finished_at"] = pd.to_datetime(health.finished_at, utc=True).dt.tz_convert(
        ZoneInfo(settings.timezone)
    )
    display(
        health,
        {
            "source_code": "Источник",
            "status": "Результат",
            "loaded_until": "Отметка загрузки",
            "finished_at": "Завершение",
        },
    )
    st.subheader("Журнал загрузок")
    source = st.selectbox(
        "Источник", ["Все", *SOURCES], format_func=lambda k: SOURCES.get(k, k)
    )
    logs = read(
        conn,
        "select load_id, source_code, started_at at time zone %s as started_at, status, "
        "requests, retries, payloads_new, error_message from meta.load_log "
        "where (%s = 'Все' or source_code = %s) order by load_id desc limit 30",
        (settings.timezone, source, source),
    )
    logs["source_code"] = logs.source_code.map(SOURCES)
    logs["status"] = logs.status.map(STATUS)
    display(
        logs,
        {
            "load_id": "№",
            "source_code": "Источник",
            "started_at": "Начало",
            "status": "Результат",
            "requests": "Запросы",
            "retries": "Повторы",
            "payloads_new": "Новые ответы",
            "error_message": "Ошибка",
        },
    )
    st.subheader("Качество данных")
    violations_only = st.checkbox("Только нарушения", value=True)
    if checks.empty:
        st.info("Проверки качества пока не выполнялись.")
    elif violations_only and not checks.status.isin(["failed", "error"]).any():
        st.success("Последние проверки пройдены.")
    if violations_only:
        checks = checks[checks.status.isin(["failed", "error"])]
    checks = checks.replace(
        {
            "status": {**STATUS, "failed": "Нарушение"},
            "severity": {
                "critical": "Критичная",
                "warning": "Предупреждение",
                "quarantine": "Карантин",
            },
        }
    )
    display(
        checks,
        {
            "rule_code": "Правило",
            "target_table": "Таблица",
            "severity": "Уровень",
            "status": "Результат",
            "failed_rows": "Строк",
        },
    )
    with st.expander("Последние преобразования"):
        transforms = read(
            conn,
            "select transform_run_id,started_at at time zone %s as started_at,status,error_message "
            "from meta.transform_run order by transform_run_id desc limit 10",
            (settings.timezone,),
        )
        display(
            transforms,
            {
                "transform_run_id": "№",
                "started_at": "Начало",
                "status": "Результат",
                "error_message": "Ошибка",
            },
        )


st.set_page_config(page_title="Погода Москвы", page_icon="🌡️", layout="wide")
page = st.sidebar.radio("Панели", ["Погода в Москве", "Работа системы"])
st.sidebar.caption("Данные обновляются на экране каждую минуту.")


@st.fragment(run_every="60s")
def panel():
    settings = load_settings()
    try:
        with db.connect(settings.db) as conn:
            if page == "Погода в Москве":
                weather(conn, settings)
            else:
                operations(conn, settings)
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName):
        st.info("Идёт подготовка витрин. Страница обновится автоматически.")
    except psycopg.OperationalError:
        st.warning("Нет соединения с PostgreSQL. Повтор через минуту.")


panel()
