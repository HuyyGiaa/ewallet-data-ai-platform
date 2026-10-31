"""Sanitized, structured failure classes for Block B boundaries."""

from __future__ import annotations


class InferenceError(Exception):
    code = "inference_error"
    status_code = 500
    retryable = False
    public_message = "Inference failed"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.public_message)


class MissingFeatureError(InferenceError):
    code = "missing_feature"
    status_code = 424
    public_message = "Required online features are unavailable"

    def __init__(self, feature_names: list[str] | tuple[str, ...]) -> None:
        self.feature_names = tuple(feature_names)
        super().__init__(self.public_message)


class StaleFeatureError(InferenceError):
    code = "stale_feature"
    status_code = 424
    public_message = "Online features exceed the freshness policy"

    def __init__(self, feature_names: list[str] | tuple[str, ...]) -> None:
        self.feature_names = tuple(feature_names)
        super().__init__(self.public_message)


class FeatureTimestampError(InferenceError):
    code = "invalid_feature_timestamp"
    status_code = 424
    public_message = "Online feature timestamps are incompatible with the request"


class FeatureContractError(InferenceError):
    code = "feature_contract_error"
    status_code = 502
    public_message = "Online feature response violates the frozen contract"


class FeatureBackendError(InferenceError):
    code = "feature_backend_error"
    status_code = 503
    retryable = True
    public_message = "Online feature service is unavailable"


class FeatureTimeoutError(InferenceError):
    code = "feature_timeout"
    status_code = 504
    retryable = True
    public_message = "Online feature retrieval timed out"


class ModelTimeoutError(InferenceError):
    code = "model_timeout"
    status_code = 504
    retryable = True
    public_message = "Model inference timed out"


class ModelBackendError(InferenceError):
    code = "model_backend_error"
    status_code = 502
    retryable = True
    public_message = "Model service is unavailable"


class ModelSignatureError(InferenceError):
    code = "model_signature_error"
    status_code = 502
    public_message = "Model signature is incompatible with the frozen contract"


class ModelResponseError(InferenceError):
    code = "model_response_error"
    status_code = 502
    public_message = "Model response violates the prediction contract"


class ConfigurationError(InferenceError):
    code = "configuration_error"
    status_code = 500
    public_message = "Inference service configuration is invalid"


class DependencyNotReadyError(InferenceError):
    code = "dependency_not_ready"
    status_code = 503
    retryable = True
    public_message = "Inference dependency is not ready"
