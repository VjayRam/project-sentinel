"""Parse OTLP JSON ExportTraceServiceRequest messages into flat span dicts.

Follows the OpenTelemetry GenAI Semantic Conventions (same format used by
LangSmith, Arize Phoenix, OpenLLMetry, and opentelemetry-instrumentation-openai):
  - Prompt/response content lives in span *events*, not span attributes.
  - Span attributes carry non-sensitive metadata: model name, token counts, etc.

Span event structure:
  gen_ai.content.prompt     → attribute gen_ai.prompt     (JSON messages array)
  gen_ai.content.completion → attribute gen_ai.completion (JSON completion object)

Prompt JSON format (OpenAI messages):
  [{"role": "user", "content": "..."}]

Completion JSON format:
  {"role": "assistant", "content": "..."}
"""

import json
import logging

logger = logging.getLogger(__name__)

_PROMPT_EVENT = "gen_ai.content.prompt"
_COMPLETION_EVENT = "gen_ai.content.completion"
_PROMPT_ATTR = "gen_ai.prompt"
_COMPLETION_ATTR = "gen_ai.completion"


def _attr(attributes: list[dict], key: str) -> str | None:
    """Return the string representation of an OTLP attribute value, or None."""
    for a in attributes:
        if a["key"] != key:
            continue
        v = a.get("value", {})
        if "stringValue" in v:
            return v["stringValue"]
        if "intValue" in v:
            return str(v["intValue"])
        if "doubleValue" in v:
            return str(v["doubleValue"])
    return None


def _event_text(events: list[dict], event_name: str, attr_key: str) -> str | None:
    """Extract plain text from a GenAI span event.

    The attribute value is a JSON-encoded messages array (for prompts) or a
    JSON-encoded message object (for completions). We parse the JSON and return
    the content string so the rest of the pipeline never sees the JSON envelope.

    Falls back to returning the raw string if JSON parsing fails.
    """
    for event in events:
        if event.get("name") != event_name:
            continue
        for a in event.get("attributes", []):
            if a["key"] != attr_key:
                continue
            raw = a.get("value", {}).get("stringValue")
            if not raw:
                return None
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    # Messages array — take the last user/human message content.
                    for msg in reversed(parsed):
                        content = msg.get("content") if isinstance(msg, dict) else None
                        if content:
                            return content
                elif isinstance(parsed, dict):
                    return parsed.get("content") or raw
            except (json.JSONDecodeError, AttributeError):
                return raw  # raw string is fine if it's not JSON-wrapped
    return None


def extract_spans(message: dict) -> list[dict]:
    """Parse one OTLP JSON message into a list of span dicts ready for classification.

    Each LLM span produces up to two entries — one for the prompt, one for the
    response — both carrying the full span metadata so the writer can store them
    with a unique (span_id, text_type) key.

    Non-LLM spans (no gen_ai events) are silently skipped.
    """
    spans = []
    for resource_span in message.get("resourceSpans", []):
        for scope_span in resource_span.get("scopeSpans", []):
            for span in scope_span.get("spans", []):
                attrs = span.get("attributes", [])
                events = span.get("events", [])

                prompt = _event_text(events, _PROMPT_EVENT, _PROMPT_ATTR)
                response = _event_text(events, _COMPLETION_EVENT, _COMPLETION_ATTR)

                if not prompt and not response:
                    continue  # not a GenAI span

                meta = {
                    "trace_id": span.get("traceId", ""),
                    "span_id": span.get("spanId", ""),
                    "session_id": _attr(attrs, "session.id") or "",
                    "llm_model": _attr(attrs, "gen_ai.request.model") or "",
                }

                if prompt:
                    spans.append({"text": prompt, "text_type": "prompt", **meta})
                if response:
                    spans.append({"text": response, "text_type": "response", **meta})

    return spans
