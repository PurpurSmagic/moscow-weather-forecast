"""Данные и признаки для моделей прогноза.

Прогноз строится от дня t — последнего дня, за который известна фактическая погода.
Признаки дня t считаются только по дням ≤ t (температура за последнюю неделю, давление,
влажность и т. д.), а целевое значение — среднесуточная температура дня t + h,
где h = 1..7 — горизонт прогноза. Так в признаки не может попасть информация из будущего.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# Колонки core.fact_weather_daily, которые нужны моделям
HISTORY_COLUMNS = (
    "temp_mean",
    "temp_min",
    "temp_max",
    "precip_mm",
    "pressure_hpa",
    "humidity_pct",
    "cloud_cover_pct",
    "wind_speed_ms",
    "snow_depth_cm",
    "dew_point_c",
)
NORM_WINDOW_DAYS = 7


def load_history(conn: Any, station_id: str) -> pd.DataFrame:
    """Фактическая погода по дням (текущие версии записей) с непрерывным индексом дат."""
    rows = conn.execute(
        f"SELECT obs_date, {', '.join(HISTORY_COLUMNS)} FROM core.fact_weather_daily "
        "WHERE valid_to IS NULL AND station_id = %s ORDER BY obs_date",
        (station_id,),
    ).fetchall()
    df = pd.DataFrame(rows, columns=["obs_date", *HISTORY_COLUMNS])
    if df.empty:
        return df.set_index("obs_date")
    df["obs_date"] = pd.to_datetime(df["obs_date"])
    # пропущенные даты остаются в индексе с пустыми значениями — чтобы сдвиги считались в днях
    return df.set_index("obs_date").asfreq("D").astype(float)


def climate_norm(temps: pd.Series, window: int = NORM_WINDOW_DAYS) -> np.ndarray:
    """Средняя температура для каждого дня года (индексы 1..366) с окном ±window дней.

    Считается только по переданным дням — при проверке качества это дни обучающего периода.
    """
    temps = temps.dropna()
    doy = temps.index.day_of_year.to_numpy()
    values = temps.to_numpy()
    norm = np.full(367, np.nan)
    for day in range(1, 367):
        distance = np.minimum(np.abs(doy - day), 366 - np.abs(doy - day))
        near = values[distance <= window]
        if len(near):
            norm[day] = near.mean()
    return norm


def make_features(history: pd.DataFrame, norm: np.ndarray) -> pd.DataFrame:
    """Признаки дня t (строка таблицы = день t). Используются только значения за дни ≤ t."""
    t = history["temp_mean"]
    x = pd.DataFrame(index=history.index)
    for lag in range(7):
        x[f"temp_{lag}"] = t.shift(lag)  # температура t, t-1, ..., t-6
    x["tmin_0"] = history["temp_min"]
    x["tmax_0"] = history["temp_max"]
    x["mean_3"] = t.rolling(3, min_periods=1).mean()
    x["mean_7"] = t.rolling(7, min_periods=1).mean()
    x["mean_30"] = t.rolling(30, min_periods=1).mean()
    x["diff_1"] = t.diff()
    x["precip_0"] = history["precip_mm"]
    x["pressure_0"] = history["pressure_hpa"]
    x["pressure_diff_1"] = history["pressure_hpa"].diff()
    x["humidity_0"] = history["humidity_pct"]
    x["cloud_0"] = history["cloud_cover_pct"]
    x["wind_0"] = history["wind_speed_ms"]
    x["snow_0"] = history["snow_depth_cm"]
    x["dew_0"] = history["dew_point_c"]
    x["norm_0"] = norm[history.index.day_of_year.to_numpy()]
    x["anom_0"] = x["temp_0"] - x["norm_0"]  # насколько сегодня теплее нормы
    x["anom_7"] = x["mean_7"] - x["norm_0"]
    return x


def horizon_data(
    history: pd.DataFrame, features: pd.DataFrame, norm: np.ndarray, h: int
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Данные для прогноза на h суток вперёд: признаки дня t, целевое значение t + h и дата t + h.

    К признакам дня t добавляются признаки дня прогноза: сезон (синус и косинус дня года)
    и климатическая норма — они известны заранее.
    """
    target_dates = history.index + pd.Timedelta(days=h)
    doy = target_dates.day_of_year.to_numpy()
    x = features.copy()
    x["norm_target"] = norm[doy]
    x["doy_sin"] = np.sin(2 * np.pi * doy / 365.25)
    x["doy_cos"] = np.cos(2 * np.pi * doy / 365.25)
    y = history["temp_mean"].shift(-h)
    return x, y, pd.Series(target_dates, index=history.index)
