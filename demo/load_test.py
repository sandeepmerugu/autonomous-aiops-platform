"""
demo/load_test.py
────────────────────────────────────────────────────────────────
Generates continuous background traffic to payment-api.
Run this to create realistic Prometheus metrics for dashboards.

HOW TO RUN:
  python demo/load_test.py              # 10 concurrent threads, run forever
  python demo/load_test.py --duration 60  # run for 60 seconds
  python demo/load_test.py --threads 5    # 5 concurrent threads
────────────────────────────────────────────────────────────────
"""

import time
import random
import argparse
import threading
import requests
from datetime import datetime

PAYMENT_API = "http://localhost:8001"
MONGODB_API = "http://localhost:8002"


def worker(thread_id: int, end_time: float, stats: dict):
    """Single worker thread — sends requests continuously."""
    endpoints = [
        ("POST", f"{PAYMENT_API}/api/v1/payments", {"amount": random.uniform(10, 1000), "method": "card"}),
        ("GET",  f"{PAYMENT_API}/api/v1/payments/PAY-{random.randint(100000,999999)}", {}),
        ("GET",  f"{PAYMENT_API}/api/v1/transactions", {}),
        ("POST", f"{MONGODB_API}/api/v1/data", {"collection": "payments"}),
        ("GET",  f"{MONGODB_API}/api/v1/data/payments", {}),
    ]

    while time.time() < end_time:
        method, url, params = random.choice(endpoints)
        try:
            if method == "POST":
                r = requests.post(url, params=params, timeout=5)
            else:
                r = requests.get(url, timeout=5)

            with threading.Lock():
                if r.status_code < 400:
                    stats["ok"] += 1
                else:
                    stats["err"] += 1
        except Exception:
            with threading.Lock():
                stats["err"] += 1

        time.sleep(random.uniform(0.1, 0.5))


def run_load_test(threads: int, duration: int):
    print(f"Starting load test: {threads} threads, {duration}s duration")
    print(f"Target: {PAYMENT_API} + {MONGODB_API}")
    print("Press Ctrl+C to stop early\n")

    stats = {"ok": 0, "err": 0}
    end_time = time.time() + duration
    workers  = []

    for i in range(threads):
        t = threading.Thread(target=worker, args=(i, end_time, stats), daemon=True)
        t.start()
        workers.append(t)

    start = time.time()
    try:
        while time.time() < end_time:
            elapsed  = time.time() - start
            total    = stats["ok"] + stats["err"]
            rps      = total / elapsed if elapsed > 0 else 0
            err_pct  = (stats["err"] / total * 100) if total > 0 else 0
            remaining = int(end_time - time.time())
            print(
                f"\r  {datetime.now().strftime('%H:%M:%S')} | "
                f"RPS: {rps:.1f} | "
                f"OK: {stats['ok']} | "
                f"Errors: {stats['err']} ({err_pct:.1f}%) | "
                f"Remaining: {remaining}s   ",
                end="", flush=True,
            )
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\nStopped by user.")

    for t in workers:
        t.join(timeout=2)

    total = stats["ok"] + stats["err"]
    elapsed = time.time() - start
    print(f"\n\nLoad test complete:")
    print(f"  Duration:    {elapsed:.0f}s")
    print(f"  Total reqs:  {total}")
    print(f"  Success:     {stats['ok']} ({stats['ok']/total*100:.1f}%)")
    print(f"  Errors:      {stats['err']} ({stats['err']/total*100:.1f}%)")
    print(f"  Avg RPS:     {total/elapsed:.1f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--threads",  type=int, default=10)
    parser.add_argument("--duration", type=int, default=300)   # 5 minutes
    args = parser.parse_args()
    run_load_test(args.threads, args.duration)
