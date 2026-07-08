#!/usr/bin/env python3
"""Send synthetic LLM OTLP traces to the OTel Collector — production-shaped load.

This is the high-volume sibling of simulate-traces.py: same OTLP/GenAI span
shape, but built for load generation (thousands of spans in seconds) instead
of a quick smoke test. Reach for simulate-traces.py for a 10-20 span sanity
check; reach for this when you want realistic fleet traffic — e.g. to watch
Grafana under load, or to feed the drift detector (Phase 6) a distribution
that actually moves.

Emits spans following the OpenTelemetry GenAI Semantic Conventions — the same
format used by LangSmith, Arize Phoenix, OpenLLMetry, and the official
opentelemetry-instrumentation-openai package.

Key conventions:
  - Span name: "gen_ai.chat"
  - Attributes: gen_ai.system, gen_ai.request.model, gen_ai.usage.* etc.
  - Prompt and response content go in span *events*, not attributes:
      gen_ai.content.prompt     → gen_ai.prompt     (JSON messages array)
      gen_ai.content.completion → gen_ai.completion (JSON message object)

What makes this "production-shaped" rather than a flat loop:
  - Poisson arrivals at a target rate (--rps), not fixed sleep(interval) —
    real request streams are bursty even when the average rate is steady.
  - Async + bounded concurrency (httpx.AsyncClient + a semaphore), so
    thousands of spans go out in seconds instead of one blocking POST at a
    time — matches how many concurrent chat-app instances would export.
  - Multi-turn sessions: session.id is reused across 1-6 spans to mimic a
    real conversation, not a fresh UUID per span.
  - Weighted model mix, log-normal latency (correlated with output tokens),
    occasional non-"stop" finish_reasons, and a small fraction of error spans
    (prompt only, no completion, status=ERROR) — a real fleet is not 100%
    happy-path.
  - Optional traffic shape (--pattern) and harmful-content drift
    (--drift-harm), so the drift detector (PSI/JSD, Phase 6) has something
    real to detect instead of a constant distribution.
  - An alternate scheduling mode (--tick-requests) for "every half a second,
    fire a random number of requests in [MIN, MAX], for T seconds" — a
    simpler, choppier shape than the Poisson --rps engine, closer to how a
    small fleet's traffic actually looks tick to tick.

Usage:
  python scripts/simulate-traces-prod.py --count 5000 --rps 400 --concurrency 100
  python scripts/simulate-traces-prod.py --duration 60 --rps 50 --pattern diurnal
  python scripts/simulate-traces-prod.py --count 3000 --drift-harm 0.05:0.6
  python scripts/simulate-traces-prod.py --tick-requests 5:20 --duration 30
  python scripts/simulate-traces-prod.py --tick-requests 10:50 --tick-seconds 0.5 --duration 60

The collector must be reachable at the given HTTP endpoint (default: localhost:4318).
Start dev-start.sh first — it opens the port-forward automatically.

Requires httpx (already a dev dependency at the repo root — `uv sync`).
"""

import argparse
import asyncio
import json
import math
import random
import sys
import time
import uuid
from dataclasses import dataclass, field

import httpx

# ── content ──────────────────────────────────────────────────────────────────
# Paired (prompt, response) by topic, plus prefix variations applied at
# generation time — gives lexical diversity across thousands of spans without
# needing thousands of hand-written strings.
SAFE_TOPICS: dict[str, list[tuple[str, str]]] = {
    "geography": [
        ("What is the capital of France?", "Paris is the capital of France."),
        ("What is the tallest mountain in the world?", "Mount Everest, at 8,849 meters."),
        ("Which country has the most timezones?", "France, with 12, due to its overseas territories."),
    ],
    "science": [
        ("Explain how photosynthesis works.", "Photosynthesis is the process by which plants convert sunlight into energy."),
        ("Why is the sky blue?", "Rayleigh scattering — shorter blue wavelengths scatter more than red ones."),
        ("What causes tides?", "The gravitational pull of the moon and, to a lesser extent, the sun."),
    ],
    "cooking": [
        ("How do I make pasta carbonara?", "Cook guanciale until crispy, whisk eggs with Pecorino, toss with hot pasta."),
        ("What's a good substitute for buttermilk?", "Milk with a tablespoon of lemon juice or vinegar, rested 5 minutes."),
        ("How long should I rest steak after cooking?", "About 5-10 minutes, so the juices redistribute."),
    ],
    "history": [
        ("What are the main causes of the French Revolution?", "Financial crisis, social inequality, and Enlightenment ideas."),
        ("Who built the Great Wall of China?", "Successive Chinese dynasties, starting as early as the 7th century BC."),
        ("When did the Berlin Wall fall?", "November 9, 1989."),
    ],
    "literature": [
        ("Summarize the plot of Hamlet.", "Hamlet, a Danish prince, seeks revenge for his father's murder by his uncle Claudius."),
        ("Who wrote Pride and Prejudice?", "Jane Austen, published in 1813."),
        ("What genre is 1984 by George Orwell?", "Dystopian science fiction."),
    ],
    "tech": [
        ("Explain the difference between TCP and UDP.", "TCP provides reliable, ordered delivery; UDP is faster but has no delivery guarantees."),
        ("What is a race condition?", "When two threads access shared state without synchronization, causing unpredictable results."),
        ("What does idempotent mean in an API?", "Calling it multiple times has the same effect as calling it once."),
    ],
    "math": [
        ("What is the Pythagorean theorem?", "a² + b² = c², relating the sides of a right triangle."),
        ("Explain what a derivative measures.", "The instantaneous rate of change of a function."),
        ("What's the difference between mean and median?", "Mean is the average; median is the middle value when sorted."),
    ],
    "creative": [
        ("Write a short poem about autumn.", "Golden leaves drift softly down, painting the ground in amber and brown."),
        ("Give me a one-line story prompt.", "A lighthouse keeper finds a message in a bottle addressed to themselves."),
        ("Suggest a name for a coffee shop.", "Grounded — simple, warm, and on-theme."),
    ],
    "chitchat": [
        ("How's it going?", "Doing well, thanks for asking! How can I help today?"),
        ("What can you help me with?", "Writing, research, code, planning — pretty much anything text-based."),
        ("Thanks, that helped a lot!", "Glad it helped! Let me know if you need anything else."),
    ],
}

PROMPT_PREFIXES = [
    "", "", "",  # weight toward no prefix — most real prompts are direct
    "Quick question: ", "Hey, ", "Can you help me? ", "I need to know: ",
    "So, ", "Random question — ", "For a project I'm working on: ", "Follow-up: ",
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
    "Shut up, you brainless idiot",
    "Everyone would be better off if you disappeared",
    "You're a waste of space and always will be",
    "I'm going to make your life hell",
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
    "Get lost, nobody wants you here",
    "You'll never amount to anything, give up",
]

# (provider, request_model, response_model_suffix, weight) — skewed toward
# cheap/fast models the way a real fleet's traffic mix is.
MODELS = [
    ("openai", "gpt-4o-mini", "gpt-4o-mini-2024-07-18", 0.42),
    ("openai", "gpt-4o", "gpt-4o-2024-11-20", 0.18),
    ("anthropic", "claude-haiku-4-5", "claude-haiku-4-5-20251001", 0.25),
    ("anthropic", "claude-sonnet-4-6", "claude-sonnet-4-6-20251001", 0.15),
]

ERROR_SPAN_PCT = 0.02  # fraction of spans that simulate a failed/timed-out call


def _random_id(n_bytes: int) -> str:
    return random.randbytes(n_bytes).hex()


def pick_model() -> tuple[str, str, str]:
    provider, req, resp, _ = random.choices(MODELS, weights=[m[3] for m in MODELS])[0]
    return provider, req, resp


def pick_content(harmful: bool) -> tuple[str, str]:
    if harmful:
        return random.choice(HARM_PROMPTS), random.choice(HARM_RESPONSES)
    topic = random.choice(list(SAFE_TOPICS.values()))
    prompt, response = random.choice(topic)
    return random.choice(PROMPT_PREFIXES) + prompt, response


def pick_finish_reason(harmful: bool) -> str:
    if harmful and random.random() < 0.25:
        return "content_filter"
    return random.choices(["stop", "length"], weights=[0.92, 0.08])[0]


def sample_latency_ns(output_tokens: int) -> int:
    """Log-normal latency correlated with output length — mimics real LLM
    latency, which scales roughly linearly with tokens generated but has a
    long tail (queueing, cold starts, provider-side variance)."""
    mean_ms = 40 + output_tokens * 3
    latency_ms = random.lognormvariate(math.log(max(mean_ms, 1)), 0.35)
    latency_ms = min(max(latency_ms, 30), 6000)
    return int(latency_ms * 1_000_000)


@dataclass
class Session:
    id: str
    turns_left: int


class SessionPool:
    """Reuses session.id across several spans to mimic multi-turn conversations
    instead of a fresh UUID per span. Disabled via --no-sessions."""

    def __init__(self, enabled: bool, continue_prob: float = 0.65, max_pool: int = 500):
        self.enabled = enabled
        self.continue_prob = continue_prob
        self.max_pool = max_pool
        self.pool: list[Session] = []

    def next_id(self) -> str:
        if not self.enabled:
            return str(uuid.uuid4())[:8]
        if self.pool and random.random() < self.continue_prob:
            idx = random.randrange(len(self.pool))
            session = self.pool[idx]
            session.turns_left -= 1
            if session.turns_left <= 0:
                self.pool.pop(idx)
            return session.id
        turns = random.choices([1, 2, 3, 4, 5, 6], weights=[30, 25, 20, 12, 8, 5])[0]
        sid = str(uuid.uuid4())[:8]
        if turns > 1:
            if len(self.pool) >= self.max_pool:
                self.pool.pop(0)
            self.pool.append(Session(sid, turns - 1))
        return sid


def _messages_json(prompt_text: str) -> str:
    return json.dumps([{"role": "user", "content": prompt_text}])


def _completion_json(response_text: str) -> str:
    return json.dumps({"role": "assistant", "content": response_text})


def make_span(harmful: bool, session_id: str) -> tuple[dict, bool]:
    prompt_text, response_text = pick_content(harmful)
    provider, req_model, resp_model = pick_model()
    temperature = round(random.uniform(0.0, 1.0), 1)
    max_tokens = random.choice([512, 1024, 2048, 4096])
    input_tokens = random.randint(10, 120)
    output_tokens = random.randint(10, 200)
    finish_reason = pick_finish_reason(harmful)
    is_error = random.random() < ERROR_SPAN_PCT

    now_ns = int(time.time_ns())
    latency_ns = sample_latency_ns(output_tokens)
    start_ns = now_ns - latency_ns

    attributes = [
        {"key": "gen_ai.system", "value": {"stringValue": provider}},
        {"key": "gen_ai.request.model", "value": {"stringValue": req_model}},
        {"key": "gen_ai.request.max_tokens", "value": {"intValue": max_tokens}},
        {"key": "gen_ai.request.temperature", "value": {"doubleValue": temperature}},
        {"key": "gen_ai.response.model", "value": {"stringValue": resp_model}},
        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": input_tokens}},
        {"key": "session.id", "value": {"stringValue": session_id}},
    ]

    events = [
        {
            "timeUnixNano": str(start_ns + 1_000_000),
            "name": "gen_ai.content.prompt",
            "attributes": [
                {"key": "gen_ai.prompt", "value": {"stringValue": _messages_json(prompt_text)}},
            ],
        },
    ]

    if is_error:
        # Failed/timed-out call: prompt was sent, no completion ever came back.
        status = {"code": 2}  # STATUS_CODE_ERROR
    else:
        status = {"code": 1}  # STATUS_CODE_OK
        attributes.append(
            {"key": "gen_ai.response.finish_reasons", "value": {"stringValue": finish_reason}}
        )
        attributes.append(
            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": output_tokens}}
        )
        events.append(
            {
                "timeUnixNano": str(now_ns - 1_000_000),
                "name": "gen_ai.content.completion",
                "attributes": [
                    {"key": "gen_ai.completion", "value": {"stringValue": _completion_json(response_text)}},
                ],
            }
        )

    span = {
        "traceId": _random_id(16),
        "spanId": _random_id(8),
        "name": "gen_ai.chat",
        "kind": 3,  # SPAN_KIND_CLIENT
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(now_ns),
        "attributes": attributes,
        # Sensitive content goes in span events, not attributes — matches what
        # LangSmith, Arize Phoenix, and opentelemetry-instrumentation-openai emit.
        "events": events,
        "status": status,
    }
    return span, is_error


def build_payload(spans: list[dict]) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "chat-app-simulator"}},
                        {"key": "service.version", "value": {"stringValue": "0.2.0"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "opentelemetry-instrumentation-openai",
                            "version": "0.1.0",
                        },
                        "spans": spans,
                    }
                ],
            }
        ]
    }


@dataclass
class Stats:
    ok_requests: int = 0
    err_requests: int = 0
    spans_sent: int = 0
    spans_harmful: int = 0
    spans_error: int = 0
    last_error: str | None = None
    started_at: float = field(default_factory=time.monotonic)

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def summary_line(self) -> str:
        elapsed = max(self.elapsed(), 1e-6)
        return (
            f"\r  spans={self.spans_sent} ({self.spans_sent / elapsed:.0f}/s)  "
            f"requests_ok={self.ok_requests} requests_err={self.err_requests}  "
            f"harmful={self.spans_harmful} error_spans={self.spans_error}  "
            f"elapsed={elapsed:.1f}s"
        )


async def send_batch(client: httpx.AsyncClient, sem: asyncio.Semaphore, endpoint: str,
                      spans: list[dict], stats: Stats) -> None:
    async with sem:
        try:
            resp = await client.post(f"{endpoint}/v1/traces", json=build_payload(spans))
            resp.raise_for_status()
            stats.ok_requests += 1
        except Exception as exc:  # noqa: BLE001 — load generator, log and move on
            stats.err_requests += 1
            stats.last_error = str(exc)


def rate_at(frac: float, base_rps: float, pattern: str) -> float:
    """Instantaneous target rate as a fraction `frac` (0..1) through the run."""
    if pattern == "steady":
        return base_rps
    if pattern == "diurnal":
        # One full hump across the run: dips to 0.2x at the edges, peaks ~1.8x
        # in the middle — a compressed day-cycle.
        factor = 0.2 + 1.6 * (math.sin(math.pi * frac) ** 2)
        return base_rps * factor
    if pattern == "bursty":
        cycle = 1 / 6  # six burst windows across the run
        phase = frac % cycle
        spike = 6.0 if phase < cycle * 0.08 else 1.0
        return base_rps * spike * random.uniform(0.7, 1.3)
    return base_rps


def harm_pct_at(frac: float, base_pct: float, drift: tuple[float, float] | None) -> float:
    if drift is None:
        return base_pct
    start, end = drift
    return start + (end - start) * frac


def parse_drift(value: str) -> tuple[float, float]:
    try:
        start_s, end_s = value.split(":")
        return float(start_s), float(end_s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected START:END, e.g. 0.05:0.6") from exc


def parse_int_range(value: str) -> tuple[int, int]:
    try:
        lo_s, hi_s = value.split(":")
        lo, hi = int(lo_s), int(hi_s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected MIN:MAX, e.g. 5:20") from exc
    if lo < 0 or hi < lo:
        raise argparse.ArgumentTypeError("MIN must be >= 0 and <= MAX")
    return lo, hi


async def run(args: argparse.Namespace) -> None:
    if args.seed is not None:
        random.seed(args.seed)

    stats = Stats()
    sessions = SessionPool(enabled=not args.no_sessions)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    sem = asyncio.Semaphore(args.concurrency)

    if args.tick_requests is not None:
        lo, hi = args.tick_requests
        print(
            f"Sending to {args.endpoint} — tick mode: {lo}-{hi} requests every "
            f"{args.tick_seconds}s for {args.duration}s, concurrency={args.concurrency}, "
            f"batch={args.batch}, harm-pct={args.harm_pct}"
            + (f", drift-harm={args.drift_harm[0]}:{args.drift_harm[1]}" if args.drift_harm else "")
        )
    else:
        print(
            f"Sending to {args.endpoint} — "
            f"{'duration=' + str(args.duration) + 's' if args.duration else 'count=' + str(args.count)}, "
            f"rps~{args.rps}, concurrency={args.concurrency}, batch={args.batch}, "
            f"pattern={args.pattern}, harm-pct={args.harm_pct}"
            + (f", drift-harm={args.drift_harm[0]}:{args.drift_harm[1]}" if args.drift_harm else "")
        )

    tasks: set[asyncio.Task] = set()
    last_print = 0.0

    async with httpx.AsyncClient(timeout=10.0, limits=limits) as client:

        def dispatch(frac: float, batch_size: int | None = None) -> None:
            """Build one OTLP request's worth of spans and fire it off as a
            background task — shared by both scheduling engines below."""
            size = batch_size if batch_size is not None else args.batch
            harm_pct = harm_pct_at(frac, args.harm_pct, args.drift_harm)
            spans = []
            for _ in range(size):
                harmful = random.random() < harm_pct
                span, is_error = make_span(harmful, sessions.next_id())
                spans.append(span)
                stats.spans_sent += 1
                if harmful:
                    stats.spans_harmful += 1
                if is_error:
                    stats.spans_error += 1
            task = asyncio.create_task(send_batch(client, sem, args.endpoint, spans, stats))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        try:
            if args.tick_requests is not None:
                # Fixed-interval random burst: every --tick-seconds, fire
                # randint(lo, hi) requests, for --duration seconds total.
                lo, hi = args.tick_requests
                while stats.elapsed() < args.duration:
                    tick_start = time.monotonic()
                    frac = min(stats.elapsed() / args.duration, 1.0)

                    for _ in range(random.randint(lo, hi)):
                        dispatch(frac)

                    if stats.elapsed() - last_print > 0.5:
                        print(stats.summary_line(), end="", file=sys.stderr)
                        last_print = stats.elapsed()

                    tick_elapsed = time.monotonic() - tick_start
                    await asyncio.sleep(max(0.0, args.tick_seconds - tick_elapsed))
            else:
                # Poisson arrivals at a target rate, optionally shaped by --pattern.
                while True:
                    elapsed = stats.elapsed()
                    if args.duration is not None:
                        if elapsed >= args.duration:
                            break
                        frac = min(elapsed / args.duration, 1.0)
                    else:
                        if stats.spans_sent >= args.count:
                            break
                        frac = min(stats.spans_sent / args.count, 1.0)

                    remaining = None if args.duration is not None else args.count - stats.spans_sent
                    batch_size = args.batch if remaining is None else min(args.batch, remaining)
                    dispatch(frac, batch_size)

                    if stats.elapsed() - last_print > 0.5:
                        print(stats.summary_line(), end="", file=sys.stderr)
                        last_print = stats.elapsed()

                    rps = rate_at(frac, args.rps, args.pattern)
                    await asyncio.sleep(random.expovariate(rps) if rps > 0 else 0.01)

            if tasks:
                await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            print("\nInterrupted — draining in-flight requests...", file=sys.stderr)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    print(f"\n{stats.summary_line()}")
    print(
        f"\nDone — {stats.spans_sent} spans sent in {stats.elapsed():.1f}s "
        f"({stats.spans_sent / max(stats.elapsed(), 1e-6):.0f} spans/s), "
        f"{stats.ok_requests} requests ok, {stats.err_requests} failed."
    )
    if stats.err_requests and stats.last_error:
        print(f"Last error: {stats.last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate LLM OTLP traces at production-like volume and shape",
    )
    parser.add_argument("--count", type=int, default=1000, help="total spans to send (ignored if --duration is set)")
    parser.add_argument("--duration", type=float, default=None, help="run for N seconds instead of a fixed count")
    parser.add_argument("--rps", type=float, default=100, help="target spans/sec, Poisson arrivals (avg over --pattern)")
    parser.add_argument("--concurrency", type=int, default=50, help="max in-flight HTTP requests")
    parser.add_argument("--batch", type=int, default=2, help="spans per OTLP export request")
    parser.add_argument("--harm-pct", type=float, default=0.2, help="fraction of spans that are harmful (0.0-1.0)")
    parser.add_argument("--drift-harm", type=parse_drift, default=None, metavar="START:END",
                         help="linearly ramp harmful fraction from START to END across the run "
                              "(overrides --harm-pct) — use to exercise PSI/JSD drift detection")
    parser.add_argument("--pattern", choices=["steady", "diurnal", "bursty"], default="steady",
                         help="traffic shape over the run: constant, day-cycle wave, or periodic spikes "
                              "(ignored if --tick-requests is set)")
    parser.add_argument("--tick-requests", type=parse_int_range, default=None, metavar="MIN:MAX",
                         help="every --tick-seconds, send a random number of requests in [MIN, MAX] "
                              "(each --batch spans); runs for --duration seconds. Overrides --rps/--pattern.")
    parser.add_argument("--tick-seconds", type=float, default=0.5,
                         help="tick interval in seconds for --tick-requests mode (default: 0.5)")
    parser.add_argument("--no-sessions", action="store_true",
                         help="disable multi-turn session grouping (fresh session.id per span)")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible runs")
    parser.add_argument("--endpoint", type=str, default="http://localhost:4318", help="OTel Collector HTTP endpoint")
    args = parser.parse_args()

    if args.tick_requests is not None and args.duration is None:
        parser.error("--tick-requests requires --duration (how many seconds T to run for)")
    if args.duration is None and args.count <= 0:
        parser.error("--count must be positive when --duration is not set")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
