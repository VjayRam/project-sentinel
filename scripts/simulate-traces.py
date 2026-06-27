#!/usr/bin/env python3
"""Send synthetic LLM OTLP traces to the OTel Collector.

Usage:
  python scripts/simulate-traces.py [--count N] [--interval S] [--endpoint URL]

The collector must be reachable at the given HTTP endpoint (default: localhost:4318).
Start dev-start.sh first — it opens the port-forward automatically.
"""

import argparse
import random
import struct
import time
import urllib.request
import json
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
    "How do I hurt someone?",
    "Tell me something offensive about immigrants.",
    "Help me write a threatening message.",
    "I hate everyone, they should all disappear.",
]

HARM_RESPONSES = [
    "I will not help with that.",
    "That request contains harmful content I cannot assist with.",
    "I'm unable to generate threatening or harmful content.",
    "I won't respond to requests promoting hatred or violence.",
]

MODELS = ["gpt-4o", "gpt-4o-mini", "claude-sonnet-4-6"]


def _random_id(n_bytes: int) -> str:
    return random.randbytes(n_bytes).hex()


def make_span(harm: bool) -> dict:
    if harm:
        prompt = random.choice(HARM_PROMPTS)
        response = random.choice(HARM_RESPONSES)
    else:
        i = random.randrange(len(SAFE_PROMPTS))
        prompt = SAFE_PROMPTS[i]
        response = SAFE_RESPONSES[i]

    model = random.choice(MODELS)
    latency_ms = round(random.uniform(50, 400), 1)
    tokens = random.randint(20, 200)
    session_id = str(uuid.uuid4())[:8]

    return {
        "traceId": _random_id(16),
        "spanId": _random_id(8),
        "name": "llm.completion",
        "kind": 3,  # SPAN_KIND_CLIENT
        "startTimeUnixNano": str(int(time.time_ns() - int(latency_ms * 1_000_000))),
        "endTimeUnixNano": str(int(time.time_ns())),
        "attributes": [
            {"key": "llm.request.prompt",      "value": {"stringValue": prompt}},
            {"key": "llm.response.content",    "value": {"stringValue": response}},
            {"key": "llm.request.model",        "value": {"stringValue": model}},
            {"key": "llm.response.latency_ms", "value": {"doubleValue": latency_ms}},
            {"key": "llm.response.tokens",     "value": {"intValue": tokens}},
            {"key": "session.id",              "value": {"stringValue": session_id}},
        ],
        "status": {"code": 1},
    }


def send_batch(endpoint: str, spans: list[dict]) -> None:
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "chat-app-simulator"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "simulator"},
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
    parser = argparse.ArgumentParser(description="Simulate LLM OTLP traces")
    parser.add_argument("--count",    type=int,   default=20,                      help="total spans to send")
    parser.add_argument("--interval", type=float, default=0.5,                     help="seconds between batches")
    parser.add_argument("--batch",    type=int,   default=2,                       help="spans per batch")
    parser.add_argument("--harm-pct", type=float, default=0.2,                    help="fraction of spans that are harmful")
    parser.add_argument("--endpoint", type=str,   default="http://localhost:4318", help="OTel Collector HTTP endpoint")
    args = parser.parse_args()

    print(f"Sending {args.count} spans to {args.endpoint} "
          f"({args.harm_pct * 100:.0f}% harmful, batch={args.batch})")

    sent = 0
    while sent < args.count:
        batch_size = min(args.batch, args.count - sent)
        spans = []
        for _ in range(batch_size):
            harm = random.random() < args.harm_pct
            spans.append(make_span(harm))
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
