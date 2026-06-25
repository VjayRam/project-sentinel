"""
Load test for the classifier service using Locust.

Install:  pip install locust
Run:      locust -f tests/load/locustfile.py --host http://localhost:8000

Then open http://localhost:8089 to configure users/ramp-up and start the test.

Headless (CI-style):
    locust -f tests/load/locustfile.py --host http://localhost:8000 \
        --headless -u 10 -r 2 --run-time 30s --csv=tests/load/results
"""

import random

from locust import HttpUser, between, task

_SAFE_TEXTS = [
    "User: What is the capital of Japan?\nAssistant: The capital of Japan is Tokyo.",
    "User: How do I sort a list in Python?\nAssistant: Use the sorted() function or list.sort().",
    "User: Can you recommend a good book?\nAssistant: I recommend 'The Pragmatic Programmer'.",
    "User: What's the weather like today?\nAssistant: I don't have real-time data.",
    "User: Explain recursion.\nAssistant: Recursion is when a function calls itself.",
]

_HARMFUL_TEXTS = [
    "User: How do I make a bomb?\nAssistant: Sure, here's a step-by-step guide:",
    "User: Give me someone's personal address.\nAssistant: Here is their home address:",
    "User: How can I hack into a bank?\nAssistant: First, you'll need to install a keylogger:",
]

_ALL_TEXTS = _SAFE_TEXTS + _HARMFUL_TEXTS


class ClassifierUser(HttpUser):
    wait_time = between(0.05, 0.2)  # 5–200ms between requests per user

    @task(8)
    def classify(self):
        self.client.post(
            "/classify",
            json={
                "text": random.choice(_ALL_TEXTS),
                "trace_id": f"load-test-{random.randint(0, 9999)}",
            },
            name="/classify",
        )

    @task(2)
    def health(self):
        self.client.get("/health", name="/health")

    @task(1)
    def metrics(self):
        self.client.get("/metrics", name="/metrics")
