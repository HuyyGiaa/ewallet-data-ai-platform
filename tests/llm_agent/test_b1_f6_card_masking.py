"""B1-F6 end to end: a card typed into kagent Chat never leaves the Coordinator.

Drives the real A2A application with a recording LLM transport and inspects
every outbound channel the task touches: the LLM request bytes, the A2A event
stream, all log records and all span attributes, including the spans created by
the FastAPI and HTTPX instrumentation.
"""

from __future__ import annotations

import json
import logging
import re
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ai_platform.llm_agent.nl_intake import IntakeFailure, plan
from tests.llm_agent.test_n11_a2a_streaming import coordinator_app, stream, terminal_states
from tests.llm_agent.test_n11_nl_intake import PROMPT, RecordingLLM, completion, run


CARD = "4111111111111111"
CARD_PROMPTS = {
    "next_number": f"{PROMPT} Thẻ {CARD} 5 items.",
    "previous_number": f"{PROMPT} 12 lần thẻ {CARD}.",
    "spaces": f"{PROMPT} Thẻ 4111 1111 1111 1111 5 items.",
    "hyphens": f"{PROMPT} Thẻ 4111-1111-1111-1111-5.",
    "dots": f"{PROMPT} Thẻ 4111.1111.1111.1111.",
    "glued": f"{PROMPT} Thẻ{CARD}.",
}


class RecordsEverything(logging.Handler):
    """Keep each formatted log record together with its structured fields."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Store the message and every record attribute."""
        self.lines.append(f"{record.getMessage()} {record.__dict__!r}")


def digits_of(text: str) -> str:
    """Drop separators, so a grouped card is still recognised."""
    return re.sub(r"\D", "", text)


def run_coordinator_task(text: str):
    """Run one chat task and return what each channel carried."""
    llm = RecordingLLM()
    app, _ = coordinator_app(planner=llm.planner())
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    records = RecordsEverything()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(records)
    root.setLevel(logging.INFO)
    instrumentation = HTTPXClientInstrumentor()
    instrumentation.instrument(tracer_provider=provider)
    try:
        with patch.object(trace, "_TRACER_PROVIDER", provider), TestClient(app) as client:
            events = stream(client, text)
    finally:
        instrumentation.uninstrument()
        root.removeHandler(records)
        root.setLevel(previous_level)
    spans = [
        json.dumps({"name": span.name, "attributes": dict(span.attributes or {}),
                    "events": [event.name for event in span.events]}, default=str)
        for span in exporter.get_finished_spans()
    ]
    return llm, events, records.lines, spans


class CardNeverLeavesTheCoordinatorTests(unittest.TestCase):
    """The screened value is the only value any outbound channel carries."""

    def test_every_channel_is_free_of_the_card(self):
        for name, text in CARD_PROMPTS.items():
            with self.subTest(name):
                llm, events, logs, spans = run_coordinator_task(text)
                self.assertEqual(terminal_states(events), ["completed"])
                self.assertEqual(len(llm.requests), 1)
                content = json.loads(llm.requests[0].content)["messages"][1]["content"]
                self.assertIn("[REDACTED_PAYMENT_CARD]", content)
                self.assertTrue(spans, "the span channel was not captured")
                self.assertTrue(logs, "the log channel was not captured")
                channels = {
                    "llm": llm.requests[0].content.decode(),
                    "events": json.dumps(events, ensure_ascii=False),
                    "logs": "\n".join(logs),
                    "spans": "\n".join(spans),
                }
                for channel, body in channels.items():
                    with self.subTest(channel=channel):
                        self.assertNotIn(CARD, digits_of(body))

    def test_the_intake_still_grounds_every_transaction_value(self):
        llm = RecordingLLM()
        result = run(plan(CARD_PROMPTS["next_number"], planner=llm.planner(), context_id="c", message_id="m"))
        self.assertEqual(str(result.request.transaction.transaction_id), "dc58ff4e-b5f7-4486-b1f8-4c31383741a9")
        self.assertEqual(float(result.request.transaction.amount), 275465.53)

    def test_the_screen_runs_before_the_llm_and_reports_no_value(self):
        screens = []
        llm = RecordingLLM()
        run(plan(CARD_PROMPTS["spaces"], planner=llm.planner(), context_id="c", message_id="m",
                 on_screen=screens.append))
        self.assertEqual(screens[0].counts["payment_card"], 1)
        self.assertNotIn(CARD, digits_of(repr(screens[0])))

    def test_a_card_argument_echoed_by_the_llm_is_not_grounded(self):
        arguments = {"transaction_id": CARD}
        llm = RecordingLLM(completion(arguments))
        with self.assertRaises(IntakeFailure) as caught:
            run(plan(CARD_PROMPTS["next_number"], planner=llm.planner(), context_id="c", message_id="m"))
        self.assertNotIn(CARD, str(caught.exception))

    def test_a_rejected_prompt_never_reaches_the_llm_or_echoes_the_card(self):
        llm = RecordingLLM()
        with self.assertRaises(IntakeFailure) as caught:
            run(plan(f"thẻ {CARD} 5 items", planner=llm.planner(), context_id="c", message_id="m"))
        self.assertEqual(llm.requests, [])
        self.assertNotIn(CARD, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
