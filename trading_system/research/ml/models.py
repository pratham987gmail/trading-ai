"""A common fit/predict/evaluate interface over a small model tournament.

Deep learning is explicitly not mandatory (Phase 2 spec, section 21): the
tournament runs Logistic Regression and Random Forest, which need only
``scikit-learn``, unconditionally, and XGBoost/LightGBM only when those
optional, heavier libraries are actually installed. A model whose library is
missing is skipped with a clear, logged reason — never silently substituted
and never a hard failure that blocks the rest of the tournament from running.

Nothing here decides that a model is good. :mod:`research.ml.evaluation`
does that, using trading metrics as the primary criterion, exactly as the
spec requires ("classification accuracy alone is insufficient").
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class ModelUnavailableError(RuntimeError):
    """Raised when a model's backing library is not installed."""


class ModelAdapter(ABC):
    """Common interface every tournament entrant implements.

    Attributes:
        name: Display name, used in the leaderboard and experiment log.
        params: Hyperparameters, kept as a plain mapping so they serialize
            into an experiment record without reflection.
    """

    name: str = "ModelAdapter"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = dict(params or {})
        self._fitted = False

    @abstractmethod
    def fit(self, X: pd.DataFrame, y: pd.Series) -> "ModelAdapter":
        """Fit the model. Must set internal state so predict() can be called."""
        raise NotImplementedError

    @abstractmethod
    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """Return class predictions (or point regression predictions)."""
        raise NotImplementedError

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray | None:
        """Return class probabilities, or ``None`` for a model that has none
        (a regressor, or a classifier that does not expose calibrated
        probabilities)."""
        return None

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "params": self.params, "fitted": self._fitted}


class LogisticRegressionModel(ModelAdapter):
    """The mandatory, always-available baseline classifier."""

    name = "LogisticRegression"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        from sklearn.linear_model import LogisticRegression

        defaults = {"max_iter": 1000, "class_weight": "balanced"}
        defaults.update(self.params)
        self.params = defaults
        self._model = LogisticRegression(**defaults)

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "LogisticRegressionModel":
        self._model.fit(X, y)
        self._fitted = True
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return self._model.predict(X)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray | None:
        return self._model.predict_proba(X)


class RandomForestModel(ModelAdapter):
    """The mandatory, always-available tree-ensemble baseline."""

    name = "RandomForest"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        from sklearn.ensemble import RandomForestClassifier

        defaults = {
            "n_estimators": 200,
            "max_depth": 6,
            "min_samples_leaf": 5,
            "class_weight": "balanced",
            "random_state": 42,
        }
        defaults.update(self.params)
        self.params = defaults
        self._model = RandomForestClassifier(**defaults)

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "RandomForestModel":
        self._model.fit(X, y)
        self._fitted = True
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return self._model.predict(X)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray | None:
        return self._model.predict_proba(X)

    def feature_importance(self, feature_names: list[str]) -> dict[str, float]:
        """Gini importance per feature, for the ablation/importance study."""
        if not self._fitted:
            raise RuntimeError("Model must be fitted before feature_importance()")
        return dict(zip(feature_names, self._model.feature_importances_.tolist()))


class XGBoostModel(ModelAdapter):
    """Optional entrant. Requires the ``xgboost`` package."""

    name = "XGBoost"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        try:
            import xgboost as xgb
        except ImportError as exc:
            raise ModelUnavailableError(
                "xgboost is not installed; this entrant is skipped, not substituted."
            ) from exc
        defaults = {"n_estimators": 200, "max_depth": 4, "learning_rate": 0.1, "eval_metric": "logloss"}
        defaults.update(self.params)
        self.params = defaults
        self._xgb = xgb
        self._model = xgb.XGBClassifier(**defaults)
        self._label_map: dict[Any, int] = {}
        self._inverse_label_map: dict[int, Any] = {}

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "XGBoostModel":
        classes = sorted(y.unique())
        self._label_map = {c: i for i, c in enumerate(classes)}
        self._inverse_label_map = {i: c for c, i in self._label_map.items()}
        y_encoded = y.map(self._label_map)
        self._model.fit(X, y_encoded)
        self._fitted = True
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        encoded = self._model.predict(X)
        return np.array([self._inverse_label_map[int(v)] for v in encoded])

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray | None:
        return self._model.predict_proba(X)


class LightGBMModel(ModelAdapter):
    """Optional entrant. Requires the ``lightgbm`` package."""

    name = "LightGBM"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        try:
            import lightgbm as lgb
        except ImportError as exc:
            raise ModelUnavailableError(
                "lightgbm is not installed; this entrant is skipped, not substituted."
            ) from exc
        defaults = {"n_estimators": 200, "max_depth": 4, "learning_rate": 0.1, "verbose": -1}
        defaults.update(self.params)
        self.params = defaults
        self._model = lgb.LGBMClassifier(**defaults)

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "LightGBMModel":
        self._model.fit(X, y)
        self._fitted = True
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return self._model.predict(X)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray | None:
        return self._model.predict_proba(X)


#: Every entrant the tournament will try to include. Order is the order they
#: appear in a leaderboard when tied on every metric.
_REGISTRY: dict[str, type[ModelAdapter]] = {
    "logistic_regression": LogisticRegressionModel,
    "random_forest": RandomForestModel,
    "xgboost": XGBoostModel,
    "lightgbm": LightGBMModel,
}

#: The two entrants the spec requires unconditionally.
MANDATORY_MODELS: tuple[str, ...] = ("logistic_regression", "random_forest")


def available_models(params: dict[str, dict[str, Any]] | None = None) -> dict[str, ModelAdapter]:
    """Instantiate every tournament entrant whose backing library is installed.

    Args:
        params: Per-model hyperparameter overrides, keyed by registry name.

    Returns:
        Mapping of registry name to a constructed (not yet fitted) adapter.
        Entrants whose library is missing are silently absent here — check the
        log for which were skipped and why, rather than assuming the
        tournament ran every model the registry lists.
    """
    params = params or {}
    models: dict[str, ModelAdapter] = {}
    for key, cls in _REGISTRY.items():
        try:
            models[key] = cls(params.get(key))
        except ModelUnavailableError as exc:
            if key in MANDATORY_MODELS:
                raise RuntimeError(
                    f"Mandatory model '{key}' could not be constructed: {exc}. "
                    "scikit-learn must be installed for the tournament to run at all."
                ) from exc
            logger.info("Skipping optional model '%s': %s", key, exc)
    return models
