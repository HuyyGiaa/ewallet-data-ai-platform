"""Redis authority for cross-replica Coordinator idempotency."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from redis import Redis

from ai_platform.llm_agent.idempotency import (
    MAX_RESULT_BYTES,
    Claim,
    ClaimStatus,
    IdempotencyBackendError,
    IdempotencyConflict,
    IdempotencyScope,
    IdempotencyStateError,
)


_NOW = "local t=redis.call('TIME'); local now=(tonumber(t[1])*1000)+math.floor(tonumber(t[2])/1000);"

_CLAIM = _NOW + r"""
local fingerprint=ARGV[1]; local owner=ARGV[2]; local lease=tonumber(ARGV[3]);
local current=redis.call('HGET',KEYS[1],'fingerprint');
if not current then
  redis.call('HSET',KEYS[1],'fingerprint',fingerprint,'state','claimed','owner',owner,'generation','1','lease_until',tostring(now+lease));
  redis.call('PEXPIRE',KEYS[1],lease*2);
  return {'acquired','1',''};
end
if current ~= fingerprint then return {'conflict','0',''}; end
local state=redis.call('HGET',KEYS[1],'state');
local generation=redis.call('HGET',KEYS[1],'generation') or '0';
if state == 'completed' then return {'completed',generation,redis.call('HGET',KEYS[1],'result') or ''}; end
if state == 'ambiguous' then return {'ambiguous',generation,''}; end
local lease_until=tonumber(redis.call('HGET',KEYS[1],'lease_until') or '0');
if now < lease_until then return {'in_progress',generation,''}; end
if state == 'started' then
  redis.call('HSET',KEYS[1],'state','ambiguous','owner','','lease_until','0'); redis.call('PERSIST',KEYS[1]);
  return {'ambiguous',generation,''};
end
if state ~= 'claimed' then return {'invalid','0',''}; end
generation=tostring(tonumber(generation)+1);
redis.call('HSET',KEYS[1],'owner',owner,'generation',generation,'lease_until',tostring(now+lease));
redis.call('PEXPIRE',KEYS[1],lease*2);
return {'acquired',generation,''};
"""

_READ = _NOW + r"""
local fingerprint=ARGV[1]; local current=redis.call('HGET',KEYS[1],'fingerprint');
if not current then return {'missing','0',''}; end
if current ~= fingerprint then return {'conflict','0',''}; end
local state=redis.call('HGET',KEYS[1],'state'); local generation=redis.call('HGET',KEYS[1],'generation') or '0';
if state == 'completed' then return {'completed',generation,redis.call('HGET',KEYS[1],'result') or ''}; end
if state == 'ambiguous' then return {'ambiguous',generation,''}; end
local lease_until=tonumber(redis.call('HGET',KEYS[1],'lease_until') or '0');
if state == 'started' and now >= lease_until then
  redis.call('HSET',KEYS[1],'state','ambiguous','owner','','lease_until','0'); redis.call('PERSIST',KEYS[1]);
  return {'ambiguous',generation,''};
end
if state == 'claimed' or state == 'started' then return {'in_progress',generation,''}; end
return {'invalid','0',''};
"""

_START = _NOW + r"""
local fingerprint=ARGV[1]; local owner=ARGV[2]; local generation=ARGV[3]; local lease=tonumber(ARGV[4]);
if redis.call('HGET',KEYS[1],'fingerprint') ~= fingerprint then return {'lost','0',''}; end
if redis.call('HGET',KEYS[1],'state') ~= 'claimed' then return {'lost','0',''}; end
if redis.call('HGET',KEYS[1],'owner') ~= owner or redis.call('HGET',KEYS[1],'generation') ~= generation then return {'lost','0',''}; end
redis.call('HSET',KEYS[1],'state','started','lease_until',tostring(now+lease)); redis.call('PERSIST',KEYS[1]);
return {'ok',generation,''};
"""

_COMPLETE = r"""
local fingerprint=ARGV[1]; local owner=ARGV[2]; local generation=ARGV[3]; local result=ARGV[4]; local ttl=tonumber(ARGV[5]);
if redis.call('HGET',KEYS[1],'fingerprint') ~= fingerprint then return {'lost','0',''}; end
if redis.call('HGET',KEYS[1],'state') ~= 'started' then return {'lost','0',''}; end
if redis.call('HGET',KEYS[1],'owner') ~= owner or redis.call('HGET',KEYS[1],'generation') ~= generation then return {'lost','0',''}; end
redis.call('HSET',KEYS[1],'state','completed','owner','','lease_until','0','result',result); redis.call('PEXPIRE',KEYS[1],ttl);
return {'ok',generation,''};
"""

_RELEASE = r"""
local fingerprint=ARGV[1]; local owner=ARGV[2]; local generation=ARGV[3];
if redis.call('HGET',KEYS[1],'fingerprint') ~= fingerprint then return {'lost','0',''}; end
if redis.call('HGET',KEYS[1],'state') ~= 'claimed' then return {'lost','0',''}; end
if redis.call('HGET',KEYS[1],'owner') ~= owner or redis.call('HGET',KEYS[1],'generation') ~= generation then return {'lost','0',''}; end
redis.call('DEL',KEYS[1]); return {'ok',generation,''};
"""


class RedisIdempotencyStore:
    """Synchronous Redis store used inside the Coordinator worker thread."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def ready(self) -> None:
        """Fail closed unless the dedicated shared authority answers now."""

        try:
            if self._client.ping() is not True:
                raise IdempotencyBackendError("Redis idempotency readiness failed")
        except IdempotencyBackendError:
            raise
        except Exception as exc:
            raise IdempotencyBackendError("Redis idempotency readiness failed") from exc

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        socket_timeout_seconds: float = 1.0,
    ) -> "RedisIdempotencyStore":
        parsed = urlparse(url)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname or parsed.password is None:
            raise ValueError("idempotency Redis URL requires redis(s), a host and a password")
        if parsed.username not in {None, "", "default"}:
            raise ValueError("only the dedicated default Redis user is supported")
        client = Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=socket_timeout_seconds,
            socket_timeout=socket_timeout_seconds,
            health_check_interval=15,
            retry_on_timeout=False,
        )
        return cls(client)

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if isinstance(value, str):
            return value
        raise IdempotencyBackendError("Redis returned a non-text protocol value")

    def _eval(self, script: str, key: str, *arguments: object) -> tuple[str, int, str]:
        try:
            raw = self._client.eval(script, 1, key, *arguments)
        except Exception as exc:
            raise IdempotencyBackendError("Redis idempotency operation failed") from exc
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raise IdempotencyBackendError("Redis returned a malformed idempotency response")
        status, generation, result = (self._text(item) for item in raw)
        try:
            generation_value = int(generation)
        except ValueError as exc:
            raise IdempotencyBackendError("Redis returned an invalid generation") from exc
        return status, generation_value, result

    @staticmethod
    def _claim(status: str, generation: int, result: str) -> Claim:
        if status == "conflict":
            raise IdempotencyConflict("request_id was reused with different canonical input")
        if status in {"missing", "invalid", "lost"}:
            raise IdempotencyStateError(f"Redis idempotency transition failed: {status}")
        try:
            value = ClaimStatus(status)
        except ValueError as exc:
            raise IdempotencyBackendError("Redis returned an unknown idempotency status") from exc
        if value is ClaimStatus.COMPLETED and not result:
            raise IdempotencyBackendError("Redis completed record has no result")
        return Claim(value, None, generation, result or None)

    def claim(self, scope, request_id, fingerprint, *, owner_token, lease_ms):
        status, generation, result = self._eval(
            _CLAIM, scope.key(request_id), fingerprint, owner_token, str(lease_ms),
        )
        claim = self._claim(status, generation, result)
        if claim.status is ClaimStatus.ACQUIRED:
            return Claim(claim.status, owner_token, claim.generation, claim.result_json)
        return claim

    def read(self, scope, request_id, fingerprint):
        return self._claim(*self._eval(_READ, scope.key(request_id), fingerprint))

    @staticmethod
    def _owned(claim: Claim) -> tuple[str, str]:
        if claim.status is not ClaimStatus.ACQUIRED or not claim.owner_token:
            raise IdempotencyStateError("transition requires an acquired claim")
        return claim.owner_token, str(claim.generation)

    def mark_started(self, scope, request_id, fingerprint, claim, *, lease_ms):
        owner, generation = self._owned(claim)
        status, _, _ = self._eval(
            _START, scope.key(request_id), fingerprint, owner, generation, str(lease_ms),
        )
        if status != "ok":
            raise IdempotencyStateError("claim ownership was lost before delegation")

    def complete(self, scope, request_id, fingerprint, claim, *, result_json, completed_ttl_ms):
        if len(result_json.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("completed result exceeds its byte budget")
        owner, generation = self._owned(claim)
        status, _, _ = self._eval(
            _COMPLETE,
            scope.key(request_id),
            fingerprint,
            owner,
            generation,
            result_json,
            str(completed_ttl_ms),
        )
        if status != "ok":
            raise IdempotencyStateError("stale owner cannot complete the request")

    def release_unstarted(self, scope, request_id, fingerprint, claim):
        owner, generation = self._owned(claim)
        status, _, _ = self._eval(
            _RELEASE, scope.key(request_id), fingerprint, owner, generation,
        )
        if status != "ok":
            raise IdempotencyStateError("only the active pre-delegation owner may release")


def idempotency_store_from_env() -> RedisIdempotencyStore:
    """Build the production authority without any in-memory fallback."""

    url = os.getenv("EWALLET_IDEMPOTENCY_REDIS_URL", "").strip()
    if not url:
        raise RuntimeError("EWALLET_IDEMPOTENCY_REDIS_URL is required for the production Coordinator")
    try:
        timeout = float(os.getenv("EWALLET_IDEMPOTENCY_REDIS_TIMEOUT_SECONDS", "1.0"))
    except ValueError as exc:
        raise RuntimeError("EWALLET_IDEMPOTENCY_REDIS_TIMEOUT_SECONDS must be numeric") from exc
    if not 0.05 <= timeout <= 10:
        raise RuntimeError("EWALLET_IDEMPOTENCY_REDIS_TIMEOUT_SECONDS must be between 0.05 and 10")
    return RedisIdempotencyStore.from_url(url, socket_timeout_seconds=timeout)
