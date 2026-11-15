"""SandboxAgent actors export their spans before kagent suspends them (AGI-12)."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from ai_platform.llm_agent import byo_runtime


class Recorder(SpanExporter):
    def __init__(self):
        self.names = []

    def export(self, spans):
        self.names += [span.name for span in spans]
        return SpanExportResult.SUCCESS

    def shutdown(self):
        return None


def test_flag_flushes_a_batch_that_would_otherwise_wait(monkeypatch):
    recorder = Recorder()
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(recorder, schedule_delay_millis=60_000))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    with provider.get_tracer("t").start_as_current_span("g3.feature_rag_specialist.execute"):
        pass
    assert recorder.names == []                     # still buffered: what a suspended actor kept
    assert byo_runtime.flush_before_response({"KAGENT_PRE_RESPONSE_TRACE_FLUSH": "true"}) is True
    assert recorder.names == ["g3.feature_rag_specialist.execute"]


def test_without_the_flag_nothing_is_forced(monkeypatch):
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: (_ for _ in ()).throw(AssertionError("read")))
    assert byo_runtime.flush_before_response({}) is False
    assert byo_runtime.flush_before_response({"KAGENT_PRE_RESPONSE_TRACE_FLUSH": "false"}) is False
