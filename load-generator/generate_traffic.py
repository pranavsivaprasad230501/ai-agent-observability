import json
import os
import random
import time
import urllib.request

TARGET_URL = os.getenv("TARGET_URL", "http://localhost:8000/agent/chat")
DURATION_SECONDS = float(os.getenv("DURATION_SECONDS", "60"))

PROMPTS = [
    "Summarize the quarterly sales report",
    "Draft a follow-up email to a customer",
    "Outline a rollout plan for a new feature",
    "Generate test cases for a login flow",
    "Explain vector databases in one paragraph",
    "Suggest three KPIs for an AI agent product",
]


def main():
    time.sleep(3)  # give agent-api time to be ready
    deadline = time.time() + DURATION_SECONDS
    sent = 0
    while time.time() < deadline:
        prompt = random.choice(PROMPTS)
        data = json.dumps({"prompt": prompt}).encode()
        req = urllib.request.Request(
            TARGET_URL, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            sent += 1
        except Exception as exc:
            print(f"request failed: {exc}")
        time.sleep(random.uniform(0.2, 0.6))
    print(f"done: sent {sent} requests over {DURATION_SECONDS:.0f}s")


if __name__ == "__main__":
    main()
