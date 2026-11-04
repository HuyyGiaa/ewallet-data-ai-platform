"""Request security boundary for the Phase D MCP tool servers.

Which mechanism verifies the caller (mesh identity forwarded by the policy-only
MCP Gateway, OAuth 2.1/JWT validation, ...) is an open architecture decision
(DECISION_REQUIRED, see docs/llm_agent/phase_d_tool_services.md). This module
only fixes the boundary every mechanism must plug into:

- an ``Authenticator`` turns request headers into a verified ``Principal`` or
  fails; the production wiring uses ``DenyAllAuthenticator`` until a mechanism
  is approved, so every tool request fails closed;
- ``ToolGrants`` authorizes tools per verified principal, deny by default;
- ``RequestSecurityMiddleware`` applies the in-flight limit, Origin check, body
  size limit, an overall body-read deadline and authentication before any
  route, body parsing or validation;
  only ``/health/`` probes bypass it;
- ``accepts_json``/``is_json_content_type`` parse media types per RFC 9110.

Client-supplied identity headers are never a source of identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import hmac
import json
import logging
import os
import re
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_platform.inference.errors import ConfigurationError
from ai_platform.llm_agent.contracts import TOOL_NAMES, ErrorResponse

SUBJECT_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,128}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
UNPROTECTED_PREFIX = "/health/"
SERVICE_TOKEN_HEADER = "x-ewallet-mcp-service-token"
SERVICE_TOKEN_ENV = "EWALLET_MCP_SERVICE_TOKENS"
_TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_MEDIA_TYPE_RE = re.compile(rf"^\s*({_TOKEN})/({_TOKEN})\s*$")
_PARAM_RE = re.compile(rf'^\s*({_TOKEN})\s*=\s*({_TOKEN}|"(?:[^"\\]|\\.)*")\s*$')
_QVALUE_RE = re.compile(r"^(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)$")

# Fixed public messages: nothing from the request or the verifier is echoed.
_PUBLIC_MESSAGES = {
    "authentication_required": "Authentication is required",
    "authentication_failed": "Credentials were not accepted",
    "authentication_not_configured": "Authentication is not configured for this service",
    "authentication_unavailable": "Authentication is temporarily unavailable",
    "origin_not_allowed": "Origin is not allowed",
    "request_too_large": "Request body is too large",
    "invalid_content_length": "Content-Length is invalid",
    "server_busy": "Too many requests in flight",
    "request_timeout": "Request body was not received in time",
}


def _media_type(value: str) -> tuple[str, str, dict[str, str]] | None:
    """``type/subtype`` lower-cased plus parameters, or None when malformed."""

    head, *raw_params = value.split(";")
    match = _MEDIA_TYPE_RE.match(head)
    if match is None:
        return None
    params: dict[str, str] = {}
    for raw in raw_params:
        param = _PARAM_RE.match(raw)
        if param is None:
            return None
        name, val = param.group(1).lower(), param.group(2)
        if val.startswith('"'):
            val = re.sub(r"\\(.)", r"\1", val[1:-1])
        if name in params:
            return None
        params[name] = val
    return match.group(1).lower(), match.group(2).lower(), params


def is_json_content_type(value: str | None) -> bool:
    """Exactly ``application/json``; the only tolerated parameter is charset=utf-8."""

    parsed = _media_type(value or "")
    if parsed is None or parsed[:2] != ("application", "json"):
        return False
    return all(name == "charset" and val.lower() == "utf-8" for name, val in parsed[2].items())


def accepts_json(accept: str | None) -> bool:
    """Whether ``application/json`` is acceptable: the most specific matching range wins, q=0 refuses.

    A missing Accept header is refused: MCP clients must send one (as in the SDK's JSON mode).
    """

    best: tuple[int, float] | None = None
    for item in (accept or "").split(","):
        parsed = _media_type(item)
        if parsed is None:
            continue
        kind, subtype, params = parsed
        q = params.get("q", "1")
        if not _QVALUE_RE.match(q):
            continue
        specificity = {("application", "json"): 2, ("application", "*"): 1, ("*", "*"): 0}.get((kind, subtype))
        if specificity is None:
            continue
        if best is None or specificity > best[0] or (specificity == best[0] and float(q) > best[1]):
            best = (specificity, float(q))
    return best is not None and best[1] > 0


@dataclass(frozen=True)
class Principal:
    """A caller identity established by an ``Authenticator``."""

    subject: str
    mechanism: str

    def __post_init__(self) -> None:
        if not SUBJECT_RE.fullmatch(self.subject):
            raise ValueError("subject must be a bounded identifier")
        if not self.mechanism:
            raise ValueError("mechanism is required")


class AuthenticationError(Exception):
    """Missing, invalid or unverifiable credentials; always fails closed."""

    def __init__(self, code: str) -> None:
        if code not in _PUBLIC_MESSAGES or not code.startswith("authentication_"):
            raise ValueError(f"unknown authentication error code {code!r}")
        super().__init__(code)
        self.code = code
        self.status_code = 503 if code == "authentication_unavailable" else 401


class Authenticator(Protocol):
    """Verifies credentials carried by the request headers.

    An implementation may also define ``async readiness() -> str | None``:
    None when it can verify credentials now, otherwise one of the
    ``authentication_*`` codes. Transport readiness reports that code.
    """

    async def authenticate(self, headers: Mapping[str, str]) -> Principal:
        """Return the verified principal or raise ``AuthenticationError``."""


class DenyAllAuthenticator:
    """Production default until the authentication mechanism is approved."""

    async def authenticate(self, headers: Mapping[str, str]) -> Principal:
        raise AuthenticationError("authentication_not_configured")

    async def readiness(self) -> str | None:
        return "authentication_not_configured"


@dataclass(frozen=True)
class _ServiceCredential:
    credential_id: str
    subject: str
    token: str
    status: str


class ServiceTokenAuthenticator:
    """Verify an internal caller against a server-controlled credential table.

    The table is loaded from a service-specific Kubernetes Secret through
    ``EWALLET_MCP_SERVICE_TOKENS``. It maps opaque credentials to subjects; a
    caller-provided actor or service name is never trusted as identity.
    """

    _TOKEN_RE = re.compile(r"^[!-~]{32,512}$")
    _CREDENTIAL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
    _STATUSES = frozenset({"active", "revoked"})

    def __init__(self, credentials: tuple[_ServiceCredential, ...]) -> None:
        self._credentials = credentials

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str],
        *,
        known_subjects: frozenset[str],
    ) -> "ServiceTokenAuthenticator":
        supplied = environment.get(SERVICE_TOKEN_ENV, "")
        try:
            if not isinstance(supplied, str):
                raise ValueError("authentication configuration must be text")
            parsed = json.loads(supplied.strip())
            if not isinstance(parsed, dict) or set(parsed) != {"version", "credentials"}:
                raise ValueError("invalid authentication configuration shape")
            if parsed["version"] != 1 or not isinstance(parsed["credentials"], list):
                raise ValueError("unsupported authentication configuration version")
            if not parsed["credentials"] or not known_subjects:
                raise ValueError("authentication configuration is empty")

            credentials: list[_ServiceCredential] = []
            credential_ids: set[str] = set()
            tokens: set[str] = set()
            for item in parsed["credentials"]:
                if not isinstance(item, dict) or set(item) != {"id", "subject", "token", "status"}:
                    raise ValueError("invalid credential shape")
                credential_id, subject, token, status = (
                    item["id"], item["subject"], item["token"], item["status"]
                )
                if not all(isinstance(value, str) for value in (credential_id, subject, token, status)):
                    raise ValueError("credential fields must be strings")
                if not cls._CREDENTIAL_ID_RE.fullmatch(credential_id) or credential_id in credential_ids:
                    raise ValueError("credential ids must be unique bounded identifiers")
                if subject not in known_subjects or not SUBJECT_RE.fullmatch(subject):
                    raise ValueError("credential subject has no configured tool grants")
                if not cls._TOKEN_RE.fullmatch(token) or token in tokens or any(ch.isspace() for ch in token):
                    raise ValueError("tokens must be unique bounded strings")
                if status not in cls._STATUSES:
                    raise ValueError("credential status must be active or revoked")
                credential_ids.add(credential_id)
                tokens.add(token)
                credentials.append(_ServiceCredential(credential_id, subject, token, status))
            if not any(item.status == "active" for item in credentials):
                raise ValueError("at least one active credential is required")
            return cls(tuple(credentials))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ConfigurationError() from exc

    async def authenticate(self, headers: Mapping[str, str]) -> Principal:
        token = headers.get(SERVICE_TOKEN_HEADER, "")
        if not token:
            raise AuthenticationError("authentication_required")
        if not self._TOKEN_RE.fullmatch(token) or any(ch.isspace() for ch in token):
            raise AuthenticationError("authentication_failed")

        matched: _ServiceCredential | None = None
        # Inspect every credential so a match position does not create a useful
        # timing distinction. The number of configured credentials is not secret.
        for credential in self._credentials:
            if hmac.compare_digest(token, credential.token):
                matched = credential
        if matched is None or matched.status != "active":
            raise AuthenticationError("authentication_failed")
        return Principal(subject=matched.subject, mechanism="service-token")

    async def readiness(self) -> str | None:
        return None


def configured_authenticator(
    environment: Mapping[str, str],
    *,
    known_subjects: frozenset[str],
    logger: logging.Logger | None = None,
) -> Authenticator:
    """Select the configured verifier, retaining deny-all on every error."""

    supplied = environment.get(SERVICE_TOKEN_ENV, "")
    if isinstance(supplied, str) and not supplied.strip():
        return DenyAllAuthenticator()
    try:
        return ServiceTokenAuthenticator.from_env(environment, known_subjects=known_subjects)
    except ConfigurationError:
        # Never log the rejected value or exception; either can contain a token.
        (logger or logging.getLogger("ewallet.llm_agent.tools")).error(json.dumps({
            "code": "authentication_not_configured",
            "event": "authentication_configuration_rejected",
        }, sort_keys=True))
        return DenyAllAuthenticator()


async def authenticator_readiness(authenticator: Authenticator) -> str | None:
    """None when protected requests can be authenticated, else a public code."""

    check = getattr(authenticator, "readiness", None)
    if check is None:
        return None
    try:
        code = await check()
    except Exception:
        return "authentication_unavailable"
    if code is None:
        return None
    return code if code in _PUBLIC_MESSAGES and code.startswith("authentication_") else "authentication_unavailable"


@dataclass(frozen=True)
class ToolGrants:
    """Tools each verified subject may list and call; anything else is denied.

    ``discovery`` subjects (e.g. the kagent controller reconciling a
    RemoteMCPServer) may *list* every tool of the server but never call one.
    """

    grants: Mapping[str, frozenset[str]] = field(default_factory=dict)
    discovery: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        checked: dict[str, frozenset[str]] = {}
        for subject, tools in self.grants.items():
            if not SUBJECT_RE.fullmatch(subject):
                raise ValueError("grant subject must be a bounded identifier")
            tools = frozenset(tools)
            if not tools or not tools <= TOOL_NAMES:
                raise ValueError(f"grants must name tools from {sorted(TOOL_NAMES)}")
            checked[subject] = tools
        discovery = frozenset(self.discovery)
        if not all(SUBJECT_RE.fullmatch(subject) for subject in discovery):
            raise ValueError("discovery subject must be a bounded identifier")
        if discovery & set(checked):
            raise ValueError("a subject is either granted tools or discovery-only, not both")
        object.__setattr__(self, "grants", MappingProxyType(checked))
        object.__setattr__(self, "discovery", discovery)

    @property
    def subjects(self) -> frozenset[str]:
        """Every subject a credential may name (granted or discovery-only)."""
        return frozenset(self.grants) | self.discovery

    def allows(self, principal: Principal, tool_name: str) -> bool:
        """May call the tool."""
        return tool_name in self.grants.get(principal.subject, frozenset())

    def lists(self, principal: Principal, tool_name: str) -> bool:
        """May see the tool in tools/list and /v1/capabilities."""
        return principal.subject in self.discovery or self.allows(principal, tool_name)

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "ToolGrants":
        """``EWALLET_MCP_TOOL_GRANTS``: JSON object of subject -> tool names;
        ``EWALLET_MCP_DISCOVERY_SUBJECTS``: JSON list of list-only subjects."""

        values = os.environ if environment is None else environment
        raw = values.get("EWALLET_MCP_TOOL_GRANTS", "").strip()
        raw_discovery = values.get("EWALLET_MCP_DISCOVERY_SUBJECTS", "").strip()
        try:
            parsed = json.loads(raw) if raw else {}
            if not isinstance(parsed, dict) or not all(
                isinstance(tools, list) and all(isinstance(t, str) for t in tools) for tools in parsed.values()
            ):
                raise ValueError("grants must map subjects to lists of tool names")
            discovery = json.loads(raw_discovery) if raw_discovery else []
            if not isinstance(discovery, list) or not all(isinstance(s, str) for s in discovery):
                raise ValueError("discovery subjects must be a list of names")
            return cls({subject: frozenset(tools) for subject, tools in parsed.items()}, frozenset(discovery))
        except ValueError as exc:
            raise ConfigurationError() from exc


@dataclass(frozen=True)
class HttpSecuritySettings:
    """Transport limits; an absent Origin is allowed, a present one must be listed."""

    allowed_origins: tuple[str, ...] = ()
    max_body_bytes: int = 64 * 1024
    max_inflight_requests: int = 32
    # One deadline for the whole body, not per chunk: a client trickling bytes
    # must not hold an in-flight slot indefinitely.
    body_read_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not all(_ORIGIN_RE.fullmatch(origin) for origin in self.allowed_origins):
            raise ValueError("allowed origins must be exact scheme://host[:port] values")
        if isinstance(self.max_body_bytes, bool) or not 1024 <= self.max_body_bytes <= 4 * 1024 * 1024:
            raise ValueError("max_body_bytes must be within 1 KiB..4 MiB")
        if isinstance(self.max_inflight_requests, bool) or not 1 <= self.max_inflight_requests <= 1024:
            raise ValueError("max_inflight_requests must be within 1..1024")
        if isinstance(self.body_read_timeout_seconds, bool) or not 0.05 <= self.body_read_timeout_seconds <= 60:
            raise ValueError("body_read_timeout_seconds must be within 0.05..60")

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "HttpSecuritySettings":
        values = os.environ if environment is None else environment
        try:
            origins = tuple(item.strip() for item in values.get("EWALLET_MCP_ALLOWED_ORIGINS", "").split(",") if item.strip())
            return cls(
                allowed_origins=origins,
                max_body_bytes=int(values.get("EWALLET_MCP_MAX_BODY_BYTES", str(cls.max_body_bytes))),
                max_inflight_requests=int(values.get("EWALLET_MCP_MAX_INFLIGHT", str(cls.max_inflight_requests))),
                body_read_timeout_seconds=float(values.get("EWALLET_MCP_BODY_TIMEOUT_SECONDS",
                                                           str(cls.body_read_timeout_seconds))),
            )
        except ValueError as exc:
            raise ConfigurationError() from exc


def error_response(status_code: int, headers: Mapping[str, str], code: str, message: str, *,
                   retryable: bool = False, extra_headers: Mapping[str, str] | None = None) -> JSONResponse:
    request_id = headers.get("x-request-id", "")
    body = ErrorResponse(
        request_id=request_id if REQUEST_ID_RE.fullmatch(request_id) else "unknown",
        code=code, message=message, retryable=retryable,
    )
    return JSONResponse(status_code=status_code, content=body.__dict__, headers=dict(extra_headers or {}))


class _BodyTooLarge(Exception):
    pass


class _ClientDisconnected(Exception):
    pass


class RequestSecurityMiddleware:
    """Checks every protected request before routing, body parsing or validation."""

    def __init__(self, app: ASGIApp, *, settings: HttpSecuritySettings, authenticator: Authenticator,
                 logger: logging.Logger | None = None) -> None:
        self.app = app
        self.settings = settings
        self.authenticator = authenticator
        self.logger = logger or logging.getLogger("ewallet.llm_agent.tools")
        self.inflight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(UNPROTECTED_PREFIX):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if self.inflight >= self.settings.max_inflight_requests:
            await self._reject(scope, receive, send, headers, 503, "server_busy", retryable=True,
                               extra_headers={"retry-after": "1"})
            return
        self.inflight += 1
        try:
            await self._guarded(scope, receive, send, headers)
        finally:
            self.inflight -= 1

    async def _guarded(self, scope: Scope, receive: Receive, send: Send, headers: Headers) -> None:
        origin = headers.get("origin")
        if origin is not None and origin not in self.settings.allowed_origins:
            await self._reject(scope, receive, send, headers, 403, "origin_not_allowed")
            return
        declared = headers.get("content-length")
        if declared is not None:
            if not declared.isdigit():
                await self._reject(scope, receive, send, headers, 400, "invalid_content_length")
                return
            if int(declared) > self.settings.max_body_bytes:
                await self._reject(scope, receive, send, headers, 413, "request_too_large")
                return
        try:
            principal = await self.authenticator.authenticate(headers)
        except AuthenticationError as exc:
            await self._reject(scope, receive, send, headers, exc.status_code, exc.code,
                               retryable=exc.status_code == 503)
            return
        except Exception:
            # A verifier fault (e.g. key fetch) must deny, never fall through.
            await self._reject(scope, receive, send, headers, 503, "authentication_unavailable", retryable=True)
            return
        try:
            body = await asyncio.wait_for(self._read_body(receive), self.settings.body_read_timeout_seconds)
        except asyncio.TimeoutError:
            # Stalled or incomplete body: the tool never sees a partial request.
            await self._reject(scope, receive, send, headers, 408, "request_timeout",
                               extra_headers={"connection": "close"})
            return
        except _BodyTooLarge:
            await self._reject(scope, receive, send, headers, 413, "request_too_large")
            return
        except _ClientDisconnected:
            # The client is gone: never run the tool on a truncated body.
            self.logger.info(json.dumps({"event": "request_aborted", "path": scope["path"],
                                         "code": "client_disconnected"}, sort_keys=True))
            return
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        state = {**scope.get("state", {}), "principal": principal}
        await self.app({**scope, "state": state}, replay, send)

    async def _read_body(self, receive: Receive) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise _ClientDisconnected()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.settings.max_body_bytes:
                raise _BodyTooLarge()
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    async def _reject(self, scope: Scope, receive: Receive, send: Send, headers: Headers, status_code: int,
                      code: str, *, retryable: bool = False, extra_headers: Mapping[str, str] | None = None) -> None:
        # Metadata only: no header values, credentials or body content.
        self.logger.info(json.dumps({"event": "request_rejected", "path": scope["path"],
                                     "status": status_code, "code": code}, sort_keys=True))
        response = error_response(status_code, headers, code, _PUBLIC_MESSAGES[code],
                                  retryable=retryable, extra_headers=extra_headers)
        await response(scope, receive, send)


def verified_principal(state: Any) -> Principal:
    """The principal the middleware attached; its absence is a wiring fault."""

    principal = getattr(state, "principal", None)
    if not isinstance(principal, Principal):
        raise AuthenticationError("authentication_required")
    return principal
