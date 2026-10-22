"""Threshold-aware MLflow model contract used by serving runtimes."""

from __future__ import annotations

import mlflow.pyfunc
import pandas as pd


class FraudPyFuncModel(mlflow.pyfunc.PythonModel):
    """Return both fraud probability and the validation-selected decision."""

    def __init__(self, pipeline: object, threshold: float) -> None:
        self.pipeline = pipeline
        self.threshold = float(threshold)

    def predict(self, context, model_input, params=None):
        frame = model_input if isinstance(model_input, pd.DataFrame) else pd.DataFrame(model_input)
        probabilities = self.pipeline.predict_proba(frame)[:, 1]
        return pd.DataFrame(
            {
                "fraud_probability": probabilities,
                "fraud_prediction": (probabilities >= self.threshold).astype(int),
            }
        )
