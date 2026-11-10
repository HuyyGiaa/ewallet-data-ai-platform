# The 4 × HTTP 503 in the EXP-V2 smoke policy probe (llm-d arm)

**Result:** the 503s are most likely vLLM's own admission control (`--max-num-queued-reqs 8`), reached because the EPP's prefix-cache scorer sent a burst of identical prompts to one pod. They come from the policy probe's burst, which runs outside every measured window. They cannot occur in the formal EXP-V2 workload, so no configuration change is needed. The GKE counters that would prove it were not captured in the smoke run; the runner now captures them (see "What changed").

## What the GKE evidence shows

From `../../exp-v2-smoke/gateway-policy/optimized/gateway.log` (23:32:19Z):

- The probe sent a 16-request burst with the same body (`"Reply with OK."`, `max_tokens 1`).
- agentgateway answered 4 requests with 429 itself (`listener=llm`, no endpoint, 0 ms). That is the 12/s `localRateLimit`.
- It forwarded 12 requests to `llmd-router-epp…:8081`, the router pod's Envoy sidecar. 8 returned 200. 4 returned **503 within 25–77 ms**, logged as `endpoint=llmd-router-epp…:8081`.
- So the 503s came from behind agentgateway: Envoy, the EPP, or a vLLM pod.

The smoke run did **not** capture EPP or Envoy container logs, or any counter around the probe. The gateway log alone cannot tell those three components apart.

Two facts point at vLLM:

- The Service arm (B2) sent the same burst and got 12×200 + 4×429, with no 503.
- The live metric names of the bench vLLM pods include `vllm:admission_rejections_total` (`exp-v2-smoke-attempt2/observability/metric-readback/live-metric-names-baseline.json`).

## What rules Envoy and EPP saturation out

- **Envoy limits.** The rendered `llm-d-router-standalone` v1.0.0 Envoy config, with the bench values, sets these circuit breakers on the upstream and ext_proc clusters: `max_connections`, `max_pending_requests` and `max_requests` of 40 000. It also sets ext_proc `message_timeout: 1000s`, a route `timeout: 86400s` and `failure_mode_allow: false`. None of these can return 503 for 12 requests in under 100 ms.
- **EPP saturation.** The EPP (same image digest `024188e9…`, same values, local kind cluster, two fake model servers) runs the `utilization-detector` filter. Its settings are queue depth 5, KV cache 0.8, staleness 200 ms, policy `saturated`, and the log says "Flow Control layer is disabled … using legacy admission control" (`epp-startup-config.log`). Both endpoints were forced to report 10 waiting requests, and the EPP read that value (`llm_d_epp_per_endpoint_queue_size` = 10). It still logged `remainingEndpoints: 2` and answered 200 for every request at `priority: 0`. So saturation does not turn default-priority requests into 503s.

## Why vLLM is the most likely source

The pinned image (digest `f695b23e…`, vLLM 0.32.0) contains this code (`vllm-0.32.0-admission-source.txt`):

- `max_num_queued_reqs` is the "maximum number of requests that can be in-flight (waiting or running) … new requests are rejected with HTTP 503".
- The rejection is counted in `vllm:admission_rejections_total{reason="max_num_queued_reqs"}`.

The bench pods run `--max-num-seqs 4 --max-num-queued-reqs 8`. If more than 8 of the 12 burst requests land on one pod, the extra ones are answered with 503.

The EPP's default profile weights `prefix-cache-scorer` at 3, against 2 each for the queue and KV scorers. The probe's routing step had just sent the same prompt three times, so identical prompts steer to the same pod.

## Local reproduction

Same EPP and Envoy images, same chart values. The fake model servers (`fake_vllm.py`) emulate `max-num-seqs 4`, a 503 above 8 in-flight requests, and 4 s per request. Commands: `burst.py`, `local-values.yaml`. Results: `burst-results.txt`.

| Burst of 12 (after 3 single requests with the same prompt) | Result | Where it went |
|---|---|---|
| Identical prompt `"Reply with OK."` (as in the probe) | **8 × 200, 4 × 503**, the GKE pattern, reproduced twice | most requests to one pod; its 9th to 12th requests rejected |
| Distinct prompts | 12 × 200 | spread over both pods |

This is an emulation of the model server, not the real one. It shows that this EPP, in this configuration, can concentrate the probe's burst enough to cross an 8-request cap.

## Effect on the formal EXP-V2 workload

None, by construction. The formal client is closed-loop, with at most 8 requests in flight in total (levels 1, 2, 4, 8). So no pod can hold more than 8, and the cap (`current + 1 > 8`) cannot trigger. The historical G2 run (same router values, levels up to 8) recorded 0 errors at every level.

The policy probe runs after the measured window, and every run starts new pods. The new dashboard panel "vLLM admission rejections per pod" makes this visible for every measured window.

## What changed

- `vllm_bench.sh`:
  - snapshots `vllm:admission_rejections_total` and `vllm:request_success_total` from every vLLM pod immediately before and after the policy probe;
  - snapshots `llm_d_epp_request_error_total` and `llm_d_epp_request_total` from the EPP the same way;
  - saves the EPP and Envoy container logs for the probe's three minutes.
- `ops/experiments/probe_attribution.py` writes `gateway-policy/<arm>/attribution.json`. It names a component only when that component's counter delta equals the 503 count; otherwise the verdict is `UNATTRIBUTED`.
- Nothing in llm-d scheduling, vLLM limits, or the gateway policy was changed.
