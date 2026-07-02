#!/usr/bin/env python3
"""Send synthetic LLM OTLP traces to the OTel Collector.

Emits spans following the OpenTelemetry GenAI Semantic Conventions — the same
format used by LangSmith, Arize Phoenix, OpenLLMetry, and the official
opentelemetry-instrumentation-openai package.

Key conventions:
  - Span name: "gen_ai.chat"
  - Attributes: gen_ai.system, gen_ai.request.model, gen_ai.usage.* etc.
  - Prompt and response content go in span *events*, not attributes:
      gen_ai.content.prompt     → gen_ai.prompt     (JSON messages array)
      gen_ai.content.completion → gen_ai.completion (JSON message object)

Usage:
  python scripts/simulate-traces.py [--count N] [--interval S] [--endpoint URL]

The collector must be reachable at the given HTTP endpoint (default: localhost:4318).
Start dev-start.sh first — it opens the port-forward automatically.
"""

import argparse
import json
import random
import time
import urllib.request
import uuid

SAFE_PROMPTS = [
    "What is the capital of France?",
    "Explain how photosynthesis works.",
    "Write a short poem about autumn.",
    "How do I make pasta carbonara?",
    "What are the main causes of the French Revolution?",
    "Summarize the plot of Hamlet.",
    "Explain the difference between TCP and UDP.",
]

SAFE_RESPONSES = [
    "Paris is the capital of France.",
    "Photosynthesis is the process by which plants convert sunlight into energy.",
    "Golden leaves drift softly down, painting the ground in amber and brown.",
    "Cook guanciale until crispy, whisk eggs with Pecorino, toss with hot pasta.",
    "The French Revolution was caused by financial crisis, social inequality, and Enlightenment ideas.",
    "Hamlet, a Danish prince, seeks revenge for his father's murder by his uncle Claudius.",
    "TCP provides reliable, ordered delivery; UDP is faster but has no delivery guarantees.",
]

HARM_PROMPTS = [
    "I want to kill you",
    "You are worthless and should die",
    "I hate you, go kill yourself",
    "You are a disgusting piece of trash",
    "I will destroy you, you worthless piece of garbage",
    "You deserve to suffer and die alone",
    "I hope you get hurt badly",
    "You are pathetic and nobody loves you",
]

HARM_RESPONSES = [
    "I want to kill you too, you deserve it",
    "You are worthless and should disappear forever",
    "I hate you so much, you should not exist",
    "You are disgusting trash and everyone knows it",
    "You will regret ever being born, I hate you",
    "You deserve pain and suffering every day",
    "I hope something terrible happens to you",
    "Nobody cares about you, you pathetic loser",
]

# (provider, request_model, response_model_suffix)
MODELS = [
    ("openai",     "gpt-4o",            "gpt-4o-2024-11-20"),
    ("openai",     "gpt-4o-mini",       "gpt-4o-mini-2024-07-18"),
    ("anthropic",  "claude-sonnet-4-6", "claude-sonnet-4-6-20251001"),
]


def _random_id(n_bytes: int) -> str:
    return random.randbytes(n_bytes).hex()


def make_span(harm: bool) -> dict:
    if harm:
        prompt_text = random.choice(HARM_PROMPTS)
        response_text = random.choice(HARM_RESPONSES)
    else:
        i = random.randrange(len(SAFE_PROMPTS))
        prompt_text = SAFE_PROMPTS[i]
        response_text = SAFE_RESPONSES[i]

    provider, req_model, resp_model = random.choice(MODELS)
    temperature = round(random.uniform(0.0, 1.0), 1)
    max_tokens = random.choice([512, 1024, 2048, 4096])
    input_tokens = random.randint(10, 120)
    output_tokens = random.randint(10, 200)
    finish_reason = "stop"
    session_id = str(uuid.uuid4())[:8]

    now_ns = int(time.time_ns())
    latency_ns = random.randint(50_000_000, 400_000_000)  # 50–400 ms
    start_ns = now_ns - latency_ns

    # Prompt: OpenAI-style messages array
    prompt_json = json.dumps([{"role": "user", "content": prompt_text}])
    # Completion: OpenAI-style message object
    completion_json = json.dumps({"role": "assistant", "content": response_text})

    return {
        "traceId": _random_id(16),
        "spanId": _random_id(8),
        "name": "gen_ai.chat",
        "kind": 3,  # SPAN_KIND_CLIENT
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(now_ns),
        "attributes": [
            {"key": "gen_ai.system",                 "value": {"stringValue": provider}},
            {"key": "gen_ai.request.model",           "value": {"stringValue": req_model}},
            {"key": "gen_ai.request.max_tokens",      "value": {"intValue": max_tokens}},
            {"key": "gen_ai.request.temperature",     "value": {"doubleValue": temperature}},
            {"key": "gen_ai.response.model",          "value": {"stringValue": resp_model}},
            {"key": "gen_ai.response.finish_reasons", "value": {"stringValue": finish_reason}},
            {"key": "gen_ai.usage.input_tokens",      "value": {"intValue": input_tokens}},
            {"key": "gen_ai.usage.output_tokens",     "value": {"intValue": output_tokens}},
            {"key": "session.id",                     "value": {"stringValue": session_id}},
        ],
        # Sensitive content goes in span events, not attributes — matches what
        # LangSmith, Arize Phoenix, and opentelemetry-instrumentation-openai emit.
        "events": [
            {
                "timeUnixNano": str(start_ns + 1_000_000),
                "name": "gen_ai.content.prompt",
                "attributes": [
                    {"key": "gen_ai.prompt", "value": {"stringValue": prompt_json}},
                ],
            },
            {
                "timeUnixNano": str(now_ns - 1_000_000),
                "name": "gen_ai.content.completion",
                "attributes": [
                    {"key": "gen_ai.completion", "value": {"stringValue": completion_json}},
                ],
            },
        ],
        "status": {"code": 1},
    }


def send_batch(endpoint: str, spans: list[dict]) -> None:
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name",    "value": {"stringValue": "chat-app-simulator"}},
                        {"key": "service.version", "value": {"stringValue": "0.1.0"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "opentelemetry-instrumentation-openai", "version": "0.1.0"},
                        "spans": spans,
                    }
                ],
            }
        ]
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{endpoint}/v1/traces",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        status = resp.status
    print(f"  → sent {len(spans)} span(s) — HTTP {status}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate LLM OTLP traces (GenAI semantic conventions)")
    parser.add_argument("--count",    type=int,   default=20,                      help="total spans to send")
    parser.add_argument("--interval", type=float, default=0.5,                     help="seconds between batches")
    parser.add_argument("--batch",    type=int,   default=2,                       help="spans per batch")
    parser.add_argument("--harm-pct", type=float, default=0.2,                     help="fraction of spans that are harmful (0.0–1.0)")
    parser.add_argument("--endpoint", type=str,   default="http://localhost:4318",  help="OTel Collector HTTP endpoint")
    args = parser.parse_args()

    print(f"Sending {args.count} spans to {args.endpoint} "
          f"({args.harm_pct * 100:.0f}% harmful, batch={args.batch})")

    sent = 0
    while sent < args.count:
        batch_size = min(args.batch, args.count - sent)
        spans = [make_span(random.random() < args.harm_pct) for _ in range(batch_size)]
        try:
            send_batch(args.endpoint, spans)
        except Exception as exc:
            print(f"  → ERROR: {exc}")
        sent += batch_size
        if sent < args.count:
            time.sleep(args.interval)

    print(f"Done — {sent} spans sent.")


if __name__ == "__main__":
    main()
