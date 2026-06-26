import logging

from prometheus_client import Counter, Histogram

REQUEST_COUNT = Counter(
    "classifier_requests_total",
    "Total classify requests",
    ["endpoint", "label"],
)

REQUEST_LATENCY = Histogram(
    "classifier_request_latency_seconds",
    "Request latency in seconds",
    ["endpoint"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

BATCH_SIZE = Histogram(
    "classifier_batch_size",
    "Number of texts per batch request",
    buckets=[1, 2, 4, 8, 16, 32, 64],
)

LOG_ERRORS = Counter(
    "classifier_log_errors_total",
    "Log records emitted at ERROR level or above",
    ["level"],
)


class _PrometheusLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            LOG_ERRORS.labels(level=record.levelname).inc()


def attach_log_handler() -> None:
    logging.getLogger().addHandler(_PrometheusLogHandler())
