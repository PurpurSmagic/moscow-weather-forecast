"""Проверка качества моделей (команда evaluate).

Схема проверки — по времени, данные не перемешиваются:
* обучение — примеры, у которых день прогноза раньше validation_from;
* проверка (validation) — дни прогноза с validation_from до test_from: на ней выбираются
  параметры моделей (alpha у Ridge, число листьев и деревьев у LightGBM);
* тест (test) — дни прогноза с test_from до последнего известного дня: модели с выбранными
  параметрами заново обучаются на всех примерах до test_from и один раз оцениваются здесь.

Признаки строятся только по дням ≤ t, а климатическая норма — только по дням обучения.
Поэтому при оценке модель не видит ни одного значения из проверяемого периода.
Метрики сохраняются в meta.model_metric, выбранные параметры — в meta.model_run.params:
их потом использует команда forecast.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from weather.config import Settings
from weather.forecast.features import climate_norm, horizon_data, load_history, make_features
from weather.forecast.journal import ForecastError, finish_run, last_date, start_run
from weather.forecast.models import PARAM_GRID, make_model

log = logging.getLogger(__name__)

TOP_FEATURES = 10


@dataclass(frozen=True)
class Metric:
    model_code: str
    split: str  # validation | test
    horizon: int
    n: int
    mae: float
    rmse: float
    bias: float
    threshold_accuracy: float


@dataclass
class EvaluationResult:
    model_run_id: int
    data_until: date
    metrics: list[Metric]
    params: dict[str, dict[str, Any]]
    importance: dict[str, dict[str, float]] = field(default_factory=dict)

    def mae_table(self, split: str = "test") -> dict[str, dict[int, float]]:
        """{модель: {горизонт: MAE}}"""
        table: dict[str, dict[int, float]] = {}
        for m in self.metrics:
            if m.split == split:
                table.setdefault(m.model_code, {})[m.horizon] = m.mae
        return table


def score(code: str, split: str, h: int, y_true: pd.Series, y_pred: np.ndarray, threshold: float) -> Metric:
    """Метрики одной модели на одном горизонте."""
    error = np.asarray(y_pred, dtype=float) - y_true.to_numpy(dtype=float)
    same_side = (np.asarray(y_pred) < threshold) == (y_true.to_numpy() < threshold)
    return Metric(
        model_code=code,
        split=split,
        horizon=h,
        n=len(error),
        mae=round(float(np.mean(np.abs(error))), 3),
        rmse=round(float(np.sqrt(np.mean(error**2))), 3),
        bias=round(float(np.mean(error)), 3),
        threshold_accuracy=round(float(np.mean(same_side)), 3),
    )


def evaluate_history(
    history: pd.DataFrame,
    codes: tuple[str, ...],
    validation_from: date,
    test_from: date,
    threshold: float,
    horizon_days: int,
) -> tuple[list[Metric], dict[str, dict[str, Any]], dict[str, dict[str, float]]]:
    """Подбор параметров на проверочном периоде и итоговая оценка на тестовом.

    Возвращает метрики, выбранные параметры и вклад признаков в моделях, обученных до test_from."""
    val_from, test_start = pd.Timestamp(validation_from), pd.Timestamp(test_from)
    horizons = range(1, horizon_days + 1)
    metrics: list[Metric] = []

    # 1. Подбор параметров: обучение на днях до validation_from, проверка — до test_from
    norm = climate_norm(history["temp_mean"][history.index < val_from])
    features = make_features(history, norm)
    data = {h: horizon_data(history, features, norm, h) for h in horizons}
    _check_split_sizes(data, val_from, test_start)

    chosen: dict[str, dict[str, Any]] = {}
    for code in codes:
        best: tuple[float, dict[str, Any], list[Metric]] | None = None
        for params in PARAM_GRID[code]:
            found, trees = [], {}
            for h in horizons:
                x, y, target = data[h]
                train = y.notna() & (target < val_from)
                valid = y.notna() & (target >= val_from) & (target < test_start)
                model = make_model(code, params, h)
                if code == "lightgbm":
                    model.fit(x[train], y[train], valid=(x[valid], y[valid]))
                    trees[str(h)] = model.best_iteration
                else:
                    model.fit(x[train], y[train])
                found.append(score(code, "validation", h, y[valid], model.predict(x[valid]), threshold))
            mean_mae = float(np.mean([m.mae for m in found]))
            if best is None or mean_mae < best[0]:
                best = (mean_mae, {**params, **({"n_estimators": trees} if trees else {})}, found)
        assert best is not None
        chosen[code] = best[1]
        metrics.extend(best[2])
        log.info("%s: параметры %s, MAE на проверке %.2f °C", code, _short(best[1]), best[0])

    # 2. Итоговая оценка: обучение на всех днях до test_from с выбранными параметрами
    norm = climate_norm(history["temp_mean"][history.index < test_start])
    features = make_features(history, norm)
    importance: dict[str, dict[str, float]] = {}
    for h in horizons:
        x, y, target = horizon_data(history, features, norm, h)
        train = y.notna() & (target < test_start)
        test = y.notna() & (target >= test_start)
        for code in codes:
            model = make_model(code, chosen[code], h).fit(x[train], y[train])
            metrics.append(score(code, "test", h, y[test], model.predict(x[test]), threshold))
            if hasattr(model, "importance"):
                total = importance.setdefault(code, {})
                for name, value in model.importance(list(x.columns)).items():
                    total[name] = total.get(name, 0.0) + value / horizon_days
    top = {
        code: {k: round(v, 4) for k, v in sorted(values.items(), key=lambda kv: -abs(kv[1]))[:TOP_FEATURES]}
        for code, values in importance.items()
    }
    return metrics, chosen, top


def run_evaluate(settings: Settings, conn: Any) -> EvaluationResult:
    """Оценка качества всех моделей из настроек; результаты пишутся в meta."""
    history = load_history(conn, settings.station.wmo_id)
    if history.empty:
        raise ForecastError("нет фактических данных — сначала выполните: python -m weather run")
    m = settings.models
    run_id = start_run(conn, "evaluate")
    log.info("Оценка качества моделей №%d: %s", run_id, ", ".join(m.codes))
    try:
        metrics, chosen, importance = evaluate_history(
            history, m.codes, m.validation_from, m.test_from, settings.heating.threshold_c,
            settings.forecast_horizon_days,
        )  # fmt: skip
    except Exception as exc:
        finish_run(conn, run_id, "failed", error=str(exc))
        raise ForecastError(f"оценка качества: {exc}") from exc

    for x in metrics:
        conn.execute(
            "INSERT INTO meta.model_metric (model_run_id, model_code, split, horizon, n, mae, rmse, bias, "
            "threshold_accuracy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (run_id, x.model_code, x.split, x.horizon, x.n, x.mae, x.rmse, x.bias, x.threshold_accuracy),
        )
    until = last_date(history)
    params = {
        "validation_from": m.validation_from.isoformat(),
        "test_from": m.test_from.isoformat(),
        "models": chosen,
        "importance": importance,
    }
    finish_run(conn, run_id, "success", data_until=until, params=params)
    return EvaluationResult(run_id, until, metrics, chosen, importance)


def _check_split_sizes(
    data: dict[int, tuple[Any, pd.Series, pd.Series]], val_from: Any, test_from: Any
) -> None:
    x, y, target = data[max(data)]
    known = y.notna()
    sizes = {
        "обучение": int((known & (target < val_from)).sum()),
        "проверка": int((known & (target >= val_from) & (target < test_from)).sum()),
        "тест": int((known & (target >= test_from)).sum()),
    }
    empty = [name for name, n in sizes.items() if n < 30]
    if empty:
        raise ForecastError(
            "мало данных для периода: "
            + ", ".join(empty)
            + f" ({sizes}); проверьте validation_from и test_from"
        )


def _short(params: dict[str, Any]) -> str:
    shown = {k: v for k, v in params.items() if k != "n_estimators"}
    if "n_estimators" in params:
        trees = params["n_estimators"]
        shown["деревьев"] = (
            f"{min(trees.values())}–{max(trees.values())}" if isinstance(trees, dict) else trees
        )
    return ", ".join(f"{k}={v}" for k, v in shown.items()) or "без параметров"
