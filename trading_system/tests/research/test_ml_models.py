"""Tests for the ML model tournament interface.

``TestOptionalModelsSkipGracefully`` is the important property for a
research lab that must run on whatever machine it happens to be on:
XGBoost/LightGBM being absent must never break the mandatory baseline
models, and must never be silently substituted with something else.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.ml.models import (
    MANDATORY_MODELS,
    LogisticRegressionModel,
    ModelUnavailableError,
    RandomForestModel,
    available_models,
)


def toy_dataset(n: int = 200, seed: int = 7) -> tuple[pd.DataFrame, pd.Series]:
    """A trivially separable dataset: label follows the sign of feature 'a'."""
    rng = np.random.default_rng(seed)
    a = rng.normal(0, 1, n)
    b = rng.normal(0, 1, n)
    X = pd.DataFrame({"a": a, "b": b})
    y = pd.Series(np.where(a > 0, "CALL_FAVORABLE", "NEUTRAL"))
    return X, y


class TestLogisticRegressionModel:
    def test_fits_and_predicts(self) -> None:
        X, y = toy_dataset()
        model = LogisticRegressionModel()
        model.fit(X, y)
        assert model.is_fitted
        preds = model.predict(X)
        assert len(preds) == len(y)

    def test_learns_the_separable_signal(self) -> None:
        X, y = toy_dataset(n=400)
        model = LogisticRegressionModel().fit(X, y)
        preds = model.predict(X)
        accuracy = (preds == y.values).mean()
        assert accuracy > 0.85  # trivially separable data, should learn it well

    def test_predict_proba_returns_calibrated_shape(self) -> None:
        X, y = toy_dataset()
        model = LogisticRegressionModel().fit(X, y)
        proba = model.predict_proba(X)
        assert proba is not None
        assert proba.shape == (len(X), 2)
        assert np.allclose(proba.sum(axis=1), 1.0)


class TestRandomForestModel:
    def test_fits_and_predicts(self) -> None:
        X, y = toy_dataset()
        model = RandomForestModel()
        model.fit(X, y)
        preds = model.predict(X)
        assert len(preds) == len(y)

    def test_feature_importance_available_after_fit(self) -> None:
        X, y = toy_dataset()
        model = RandomForestModel().fit(X, y)
        importance = model.feature_importance(["a", "b"])
        assert set(importance) == {"a", "b"}
        # 'a' determines the label; it must dominate 'b'.
        assert importance["a"] > importance["b"]

    def test_feature_importance_before_fit_raises(self) -> None:
        model = RandomForestModel()
        with pytest.raises(RuntimeError):
            model.feature_importance(["a", "b"])


class TestOptionalModelsSkipGracefully:
    def test_mandatory_models_are_always_present(self) -> None:
        models = available_models()
        for name in MANDATORY_MODELS:
            assert name in models
            assert models[name].is_fitted is False

    def test_xgboost_or_lightgbm_absence_does_not_break_the_others(self) -> None:
        """Whatever is actually installed on this machine, the mandatory
        entrants must still come back, and nothing raises."""
        models = available_models()
        assert "logistic_regression" in models
        assert "random_forest" in models
        # xgboost/lightgbm may or may not be present; either way this must
        # not raise, and only real ModelAdapter instances are returned.
        for name, model in models.items():
            assert hasattr(model, "fit")
            assert hasattr(model, "predict")

    def test_unavailable_model_raises_a_specific_error_not_a_bare_importerror(self) -> None:
        """A model class constructed directly (bypassing available_models())
        must fail with its own clear error when the library is missing."""
        try:
            import xgboost  # noqa: F401

            pytest.skip("xgboost is installed in this environment")
        except ImportError:
            pass

        from research.ml.models import XGBoostModel

        with pytest.raises(ModelUnavailableError):
            XGBoostModel()
