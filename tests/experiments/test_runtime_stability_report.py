"""runtime_stability_report: facts from driver rows and fleet recordings."""

from __future__ import annotations

import json

from ops.experiments import runtime_stability_report as rep


def test_rows_summary_counts_states_arrivals_errors_and_actor_identity(tmp_path):
    rows = [
        {"phase": "new", "state": "failed", "context": "c1", "actor": "asr-a", "e2e_ms": 20000},
        {"phase": "resume", "state": "failed", "context": "c1", "actor": "asr-a", "status_at_send": "Suspending", "e2e_ms": 3000},
        {"phase": "resume", "state": "rpc_error", "context": "c2", "actor": "asr-b", "rpc_error_message": "no free workers"},
        {"phase": "resume", "state": "failed", "context": "c2", "actor": "asr-c"},
    ]
    path = tmp_path / "r.txt"
    path.write_text("\n".join("LOAD_ROW " + json.dumps(r) for r in rows))
    out = rep.summarize_rows(rep.rows([str(path)]))
    assert out["terminal_without_rpc_error"] == 3
    assert out["rpc_errors"] == {"no free workers": 1}
    assert out["arrival_status"] == {"resume:Suspending": 1}
    assert out["contexts_with_more_than_one_actor"] == ["c2"]


def test_status_summary_measures_transitional_stretches_and_sharing(tmp_path):
    samples = [
        {"t": 0, "actors": {"drift": [{"actor": "asr-a", "pod": "w1", "status": "Resuming"}],
                            "rag": [{"actor": "golden-1", "pod": "w2", "status": "Running"}]}},
        {"t": 30, "actors": {"drift": [{"actor": "asr-a", "pod": "w1", "status": "Resuming"}]}},
        {"t": 31, "actors": {"drift": [{"actor": "asr-a", "pod": "w1", "status": "Running"}],
                             "rag": [{"actor": "asr-b", "pod": "w1", "status": "Resuming"}]}},
    ]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(s) for s in samples))
    out = rep.summarize_status([str(path)])
    assert out["longest_transitional_s"]["asr-a:Resuming"] == 30
    assert out["still_transitional_at_end"] == {"asr-b": "Resuming"}
    assert out["workers_serving_both_agents"] == ["w1"]
