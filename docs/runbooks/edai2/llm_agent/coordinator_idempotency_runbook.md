# Coordinator durable idempotency runbook

## Runtime contract

The BYO Coordinator uses a dedicated Redis authority named
`coordinator-idempotency-redis` in `ewallet-agents`. It must not share data,
credentials, PVCs or lifecycle with Feast or RAG Redis.

- Redis uses AOF with `appendfsync everysec` and `maxmemory-policy noeviction`.
- `COMPLETED` results are replay-protected only for the configured TTL
  (`EWALLET_IDEMPOTENCY_COMPLETED_TTL_MS`, currently 24 hours). After expiry,
  the request may execute again; protection is not indefinite.
- `STARTED` records that outlive their lease become persistent `AMBIGUOUS`
  records. They are not evicted and are never automatically retried.
- AOF `everysec` bounds normal persistence overhead but can lose roughly the
  latest second during an abrupt host failure. The platform therefore does
  not claim exactly-once downstream effects.
- The key scope is the deployment-configured tenant, the exact authenticated
  kagent-controller SPIFFE principal, and a hash of validated business
  `user_id` plus `request_id`. Forwarded identity headers are not authoritative.

## Secret contract

The immutable Secret `coordinator-idempotency-redis-auth` is created outside
Helm and contains only:

- `password`: a randomly generated Redis password;
- `connection_url`: the authenticated in-cluster URL used by the Coordinator;
- `users.acl`: a Redis ACL for the default user, restricted to the
  `ewallet:coordinator:idempotency:v1:*` key prefix and the commands required by
  the atomic Lua state machine. Besides the direct Lua commands, the ACL must
  include `SELECT`, `MULTI`, `EXEC` and `PEXPIREAT`: Redis writes a database
  selection before the transaction, persists Lua effects to AOF as
  transactions, and rewrites relative expiry as an absolute expiry. Omitting
  those commands can make AOF restart emit `NOPERM` and is a durability gate
  failure.

Never print, log, copy into evidence, or place these values in Helm values.
Rotate the Secret only in a separately reviewed maintenance window because the
Redis server and every Coordinator replica must switch coherently.

## Health and persistence checks

1. Confirm the StatefulSet, PVC and single Ready pod before checking Agents.
2. Confirm the live Redis configuration reports AOF enabled, fsync `everysec`
   and eviction policy `noeviction`; redact authentication material.
3. Confirm `/ready` on every Coordinator replica returns 503 when Redis is
   unreachable and returns 200 only when Redis answers an authenticated ping.
4. Complete one bounded request, record its Specialist/MCP invocation counts,
   restart Redis gracefully, and replay from another Coordinator context. The
   result must be identical and invocation counts must not increase.
5. Restart one Coordinator replica and repeat the replay check through the
   restarted replica.

## NetworkPolicy verification

Positive and negative checks are runtime gates, not render-only claims:

- `fraud-coordinator` ServiceAccount/pods can connect and authenticate.
- A temporary pod using another ServiceAccount in `ewallet-agents` cannot
  connect to port 6379.
- A pod in another namespace cannot connect.
- Unauthenticated Redis access is denied even from an allowed source.
- Redis has no application egress requirement; the namespace default-deny
  remains active.

Delete only the explicitly named temporary test pods after evidence capture.

## AMBIGUOUS reconciliation

An `idempotency_ambiguous` response means downstream work may have happened.
Do not delete the Redis record or replay the business request immediately.

1. Freeze retries for that tenant, business user and request ID.
2. Capture the Coordinator trace ID, A2A task, Gateway request, Specialist task,
   MCP audit events, Feast lookup and KServe request evidence. Do not capture
   credentials or raw sensitive feature values.
3. Compute the hashed Redis key locally from the exact deployment tenant,
   principal, validated business `user_id` and `request_id`; never use a
   forwarded identity header. Inspect only `state`, `fingerprint`, `generation`
   and `lease_until` first.
4. Classify one of these outcomes:
   - **PROVEN_NOT_STARTED:** all downstream audit sources prove no invocation.
     A separately approved operator action may delete only that exact ambiguous
     key, after which a new request can claim it.
   - **PROVEN_COMPLETED:** downstream evidence and the validated canonical
     result agree. A separately approved reconciliation action may atomically
     replace only that exact ambiguous record with `COMPLETED` and a fresh,
     explicit bounded TTL.
   - **UNRESOLVED:** evidence is missing or inconsistent. Preserve the
     `AMBIGUOUS` record indefinitely and return a fail-closed response.
5. Record the key hash, prior/new state, evidence checksums, operator, approval,
   timestamp and TTL. Never record the Redis password or result payload.

Direct `HSET`, broad key scans, wildcard deletion and automatic ambiguous
replay are prohibited. If a purpose-built reconciliation command is not
available, keep the record `UNRESOLVED` rather than editing Redis manually.

## Rollback

Rollback the `llm-agent` Helm release to its prior revision if readiness,
authorization, replay or G3 business behavior regresses. Preserve the Redis PVC
and Secret for investigation; do not revoke or delete them as part of an
application rollback. Restore the prior release first, verify G3 health, then
decide the Redis lifecycle in a separate approved cleanup.
