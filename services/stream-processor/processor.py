"""Parse OTLP JSON ExportTraceServiceRequest messages into flat span dicts."""

import logging

logger = logging.getLogger(__name__)

# Attributes the chat app must emit on every LLM span (CLAUDE.md contract).
_PROMPT_ATTR = "llm.request.prompt"
_RESPONSE_ATTR = "llm.response.content"


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


def extract_spans(message: dict) -> list[dict]:
    """Parse one OTLP JSON message into a list of span dicts ready for classification.

    Each LLM span produces up to two entries — one for the prompt, one for the
    response — both carrying the full span metadata so the writer can store them
    with a unique (span_id, text_type) key.
    """
    spans = []
    for resource_span in message.get("resourceSpans", []):
        for scope_span in resource_span.get("scopeSpans", []):
            for span in scope_span.get("spans", []):
                attrs = span.get("attributes", [])
                prompt = _attr(attrs, _PROMPT_ATTR)
                response = _attr(attrs, _RESPONSE_ATTR)

                if not prompt and not response:
                    continue  # not an LLM span

                meta = {
                    "trace_id": span.get("traceId", ""),
                    "span_id": span.get("spanId", ""),
                    "session_id": _attr(attrs, "session.id") or "",
                    "llm_model": _attr(attrs, "llm.request.model") or "",
                }

                if prompt:
                    spans.append({"text": prompt, "text_type": "prompt", **meta})
                if response:
                    spans.append({"text": response, "text_type": "response", **meta})

    return spans
