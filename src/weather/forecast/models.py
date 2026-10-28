"""Модели прогноза среднесуточной температуры.

Для каждого горизонта h = 1..7 обучается своя модель (прямая стратегия):
модель на 3 дня вперёд учится только на парах «признаки дня t → температура дня t + 3».

* persistence — «как вчера»: завтра и всю неделю будет так же, как в последний известный день;
* climatology — климатическая норма для дня прогноза;
* ridge — линейная регрессия с регуляризацией по признакам из features.py;
* lightgbm — градиентный бустинг деревьев решений.

Первые две модели — базовые: остальные имеет смысл использовать, только если они точнее их.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

MODEL_NAMES = {
    "persistence": "как вчера",
    "climatology": "норма",
    "ridge": "Ridge",
    "lightgbm": "LightGBM",
}

# Варианты параметров, из которых команда evaluate выбирает лучший на проверочном периоде
PARAM_GRID: dict[str, list[dict[str, Any]]] = {
    "persistence": [{}],
    "climatology": [{}],
    "ridge": [{"alpha": a} for a in (1.0, 10.0, 100.0, 1000.0)],
    "lightgbm": [{"num_leaves": n, "learning_rate": 0.03} for n in (7, 15, 31)],
}

# Параметры по умолчанию — если оценка качества ещё ни разу не запускалась
DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "persistence": {},
    "climatology": {},
    "ridge": {"alpha": 10.0},
    "lightgbm": {"num_leaves": 15, "learning_rate": 0.03, "n_estimators": 400},
}

LIGHTGBM_MAX_TREES = 3000
LIGHTGBM_EARLY_STOPPING = 100
SEED = 42


class Persistence:
    """Прогноз «как вчера»: температура последнего известного дня."""

    def fit(self, x: pd.DataFrame, y: pd.Series) -> Persistence:
        return self

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return x["temp_0"].to_numpy()


class Climatology:
    """Прогноз «как обычно»: климатическая норма для дня прогноза."""

    def fit(self, x: pd.DataFrame, y: pd.Series) -> Climatology:
        return self

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return x["norm_target"].to_numpy()


class RidgeModel:
    """Линейная регрессия: пропуски заполняются медианой, признаки приводятся к одному масштабу."""

    def __init__(self, alpha: float) -> None:
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        self.pipeline = make_pipeline(
            SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(), Ridge(alpha=alpha)
        )

    def fit(self, x: pd.DataFrame, y: pd.Series) -> RidgeModel:
        self.pipeline.fit(x, y)
        return self

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return self.pipeline.predict(x)

    def importance(self, columns: list[str]) -> dict[str, float]:
        """Коэффициенты при признаках в одном масштабе: насколько °C меняется прогноз
        при изменении признака на одно стандартное отклонение."""
        coef = self.pipeline[-1].coef_
        return dict(zip(columns, (float(c) for c in coef), strict=True))


class LightGBMModel:
    """Градиентный бустинг. Пропуски в признаках LightGBM обрабатывает сам."""

    def __init__(self, num_leaves: int, learning_rate: float, n_estimators: int = LIGHTGBM_MAX_TREES) -> None:
        import lightgbm

        self.model = lightgbm.LGBMRegressor(
            n_estimators=n_estimators,
            learning_rate=learning_rate,
            num_leaves=num_leaves,
            min_child_samples=20,
            subsample=0.8,
            subsample_freq=1,
            colsample_bytree=0.8,
            random_state=SEED,
            verbose=-1,
        )

    def fit(
        self, x: pd.DataFrame, y: pd.Series, valid: tuple[pd.DataFrame, pd.Series] | None = None
    ) -> LightGBMModel:
        """valid — проверочные данные: обучение останавливается, когда ошибка на них перестаёт падать."""
        if valid is None:
            self.model.fit(x, y)
        else:
            import lightgbm

            self.model.fit(
                x,
                y,
                eval_X=valid[0],
                eval_y=valid[1],
                eval_metric="l1",
                callbacks=[lightgbm.early_stopping(LIGHTGBM_EARLY_STOPPING, verbose=False)],
            )
        return self

    @property
    def best_iteration(self) -> int:
        return int(self.model.best_iteration_ or self.model.n_estimators)

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return self.model.predict(x)

    def importance(self, columns: list[str]) -> dict[str, float]:
        """Вклад признаков: суммарное уменьшение ошибки на разбиениях по признаку, в долях."""
        gain = self.model.booster_.feature_importance(importance_type="gain")
        total = float(gain.sum()) or 1.0
        return dict(zip(columns, (float(g) / total for g in gain), strict=True))


def make_model(code: str, params: dict[str, Any], h: int | None = None) -> Any:
    """Модель по коду. Для LightGBM число деревьев может быть задано своё для каждого горизонта."""
    if code == "persistence":
        return Persistence()
    if code == "climatology":
        return Climatology()
    if code == "ridge":
        return RidgeModel(alpha=float(params["alpha"]))
    if code == "lightgbm":
        trees = params.get("n_estimators", LIGHTGBM_MAX_TREES)
        if isinstance(trees, dict):
            trees = trees.get(str(h), DEFAULT_PARAMS["lightgbm"]["n_estimators"])
        return LightGBMModel(int(params["num_leaves"]), float(params["learning_rate"]), int(trees))
    raise ValueError(f"неизвестная модель {code}")
