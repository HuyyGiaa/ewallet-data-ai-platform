"""KServe V2 transport with an artifact-reviewed wire contract boundary."""

from __future__ import annotations

import asyncio
import json
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ai_platform.inference.assembler import ModelInputVector
from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS
from ai_platform.inference.errors import (
    ModelBackendError,
    ModelResponseError,
    ModelSignatureError,
    ModelTimeoutError,
)
from ai_platform.inference.schemas import ModelPrediction


class JsonTransport(Protocol):
    async def get_json(
        self, url: str, *, timeout_seconds: float
    ) -> dict[str, object]: ...

    async def post_json(
        self,
        url: str,
        payload: dict[str, object],
        *,
        timeout_seconds: float,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, object]: ...


class KServeWireContract(Protocol):
    """Adapter proven against one registered MLflow/KServe artifact.

    No default implementation is provided because the source tree establishes
    the 41-column DataFrame schema, but not its KServe tensor serialization.
    """

    input_columns: tuple[str, ...]
    nullable_input_columns: frozenset[str]

    def validate_metadata(self, metadata: dict[str, object]) -> None: ...

    def encode_request(
        self, vector: ModelInputVector, *, correlation_id: str
    ) -> dict[str, object]: ...

    def decode_response(
        self,
        response: dict[str, object],
        *,
        model_name: str,
        model_version: str,
        correlation_id: str,
    ) -> ModelPrediction: ...

class UrllibJsonTransport:
    """Dependency-free bounded JSON transport for the production adapter."""

    async def get_json(
        self, url: str, *, timeout_seconds: float
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._request, "GET", url, None, timeout_seconds, None
        )

    async def post_json(
        self,
        url: str,
        payload: dict[str, object],
        *,
        timeout_seconds: float,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._request,
            "POST",
            url,
            payload,
            timeout_seconds,
            headers,
        )

    @staticmethod
    def _request(
        method: str,
        url: str,
        payload: dict[str, object] | None,
        timeout_seconds: float,
        headers: Mapping[str, str] | None,
    ) -> dict[str, object]:
        body = None if payload is None else json.dumps(
            payload, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        request_headers = {"accept": "application/json"}
        if body is not None:
            request_headers["content-type"] = "application/json"
        request_headers.update(headers or {})
        request = Request(
            url, data=body, headers=request_headers, method=method
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
        except TimeoutError:
            raise
        except HTTPError as exc:
            raise RuntimeError("KServe HTTP request failed") from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("KServe HTTP request timed out") from exc
            raise RuntimeError("KServe HTTP request failed") from exc
        except OSError as exc:
            raise RuntimeError("KServe HTTP request failed") from exc
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("KServe returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("KServe JSON response must be an object")
        return decoded


class KServeV2Client:
    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        model_version: str,
        timeout_seconds: float,
        transport: JsonTransport | None = None,
        wire_contract: KServeWireContract | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must use http or https")
        if not model_name.strip() or not model_version.strip():
            raise ValueError("model_name and model_version are required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.model_name = model_name
        self.model_version = model_version
        self.timeout_seconds = float(timeout_seconds)
        self._transport = transport or UrllibJsonTransport()
        self._wire_contract = wire_contract
        model = quote(model_name, safe="")
        version = quote(model_version, safe="")
        self._model_url = (
            f"{base_url.rstrip('/')}/v2/models/{model}/versions/{version}"
        )
        self._signature_validated = False

    def _require_wire_contract(self) -> KServeWireContract:
        contract = self._wire_contract
        if contract is None:
            raise ModelSignatureError()
        try:
            input_columns = contract.input_columns
            nullable_columns = contract.nullable_input_columns
        except AttributeError as exc:
            raise ModelSignatureError() from exc
        if input_columns != MODEL_INPUT_COLUMNS:
            raise ModelSignatureError()
        if not isinstance(nullable_columns, frozenset):
            raise ModelSignatureError()
        if not nullable_columns.issubset(MODEL_INPUT_COLUMNS):
            raise ModelSignatureError()
        return contract

    async def _backend_call(self, awaitable):
        try:
            return await asyncio.wait_for(
                awaitable, timeout=self.timeout_seconds
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ModelTimeoutError() from exc
        except (ModelTimeoutError, ModelBackendError):
            raise
        except Exception as exc:
            raise ModelBackendError() from exc

    async def ready(self) -> bool:
        contract = self._require_wire_contract()
        metadata = await self._backend_call(
            self._transport.get_json(
                self._model_url, timeout_seconds=self.timeout_seconds
            )
        )
        self._validate_metadata(metadata, contract)
        await self._backend_call(
            self._transport.get_json(
                self._model_url + "/ready",
                timeout_seconds=self.timeout_seconds,
            )
        )
        self._signature_validated = True
        return True

    def _validate_metadata(
        self, metadata: object, contract: KServeWireContract
    ) -> None:
        if not isinstance(metadata, dict):
            raise ModelSignatureError()
        if metadata.get("name") != self.model_name:
            raise ModelSignatureError()
        if "versions" in metadata:
            versions = metadata["versions"]
            if (
                not isinstance(versions, list)
                or self.model_version not in versions
            ):
                raise ModelSignatureError()
        try:
            contract.validate_metadata(metadata)
        except ModelSignatureError:
            raise
        except Exception as exc:
            raise ModelSignatureError() from exc

    @staticmethod
    def _validate_payload(payload: object, correlation_id: str) -> None:
        if not isinstance(payload, dict) or payload.get("id") != correlation_id:
            raise ModelSignatureError()

    async def predict(
        self,
        vector: ModelInputVector,
        *,
        correlation_id: str,
    ) -> ModelPrediction:
        contract = self._require_wire_contract()
        if not self._signature_validated:
            await self.ready()
        null_columns = frozenset(
            column
            for column, value in zip(
                vector.columns, vector.values, strict=True
            )
            if value is None
        )
        if not null_columns.issubset(contract.nullable_input_columns):
            raise ModelSignatureError()
        try:
            payload = contract.encode_request(
                vector, correlation_id=correlation_id
            )
        except ModelSignatureError:
            raise
        except Exception as exc:
            raise ModelSignatureError() from exc
        self._validate_payload(payload, correlation_id)
        headers = {"x-correlation-id": correlation_id}
        try:
            from opentelemetry import propagate

            propagate.inject(headers)
        except ImportError:
            pass
        response = await self._backend_call(
            self._transport.post_json(
                self._model_url + "/infer",
                payload,
                timeout_seconds=self.timeout_seconds,
                headers=headers,
            )
        )
        return self._decode_response(response, correlation_id, contract)

    def _decode_response(
        self,
        response: object,
        correlation_id: str,
        contract: KServeWireContract,
    ) -> ModelPrediction:
        if not isinstance(response, dict):
            raise ModelResponseError()
        if response.get("model_name") != self.model_name:
            raise ModelResponseError()
        if (
            "model_version" in response
            and response["model_version"] != self.model_version
        ):
            raise ModelResponseError()
        if response.get("id") != correlation_id:
            raise ModelResponseError()
        try:
            prediction = contract.decode_response(
                response,
                model_name=self.model_name,
                model_version=self.model_version,
                correlation_id=correlation_id,
            )
        except ModelResponseError:
            raise
        except Exception as exc:
            raise ModelResponseError() from exc
        if not isinstance(prediction, ModelPrediction):
            raise ModelResponseError()
        if (
            prediction.model_name != self.model_name
            or prediction.model_version != self.model_version
        ):
            raise ModelResponseError()
        return prediction
