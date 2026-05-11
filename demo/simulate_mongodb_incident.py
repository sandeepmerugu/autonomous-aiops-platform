"""
demo/simulate_mongodb_incident.py
────────────────────────────────────────────────────────────────
Simulates the EXACT MongoDB node-maintenance incident from
Sandeep's real production experience at Verizon.

WHAT THIS SCRIPT DOES:
  1. Generates baseline traffic so Prometheus has normal metrics
  2. Triggers MongoDB node maintenance (connections fail)
  3. Sends traffic — errors flood in, logs fill with ECONNREFUSED
  4. Triggers the AI engine to generate RCA automatically
  5. Waits for RCA to appear and prints the result
  6. Restores MongoDB (incident over)
  7. Shows the full before/after metrics

HOW TO RUN:
  # Make sure docker compose up -d is running first
  python demo/simulate_mongodb_incident.py

WHAT YOU WILL SEE:
  - Payment API starts returning 500 errors
  - MongoDB connection error rate spikes in Prometheus
  - Error logs appear in Loki (visible in Grafana)
  - AI engine generates RCA within ~30 seconds
  - RCA appears at http://localhost:8000/incidents/ui
────────────────────────────────────────────────────────────────
"""

import time
import json
import threading
import requests
from datetime import datetime

PAYMENT_API_URL    = "http://localhost:8001"
MONGODB_URL        = "http://localhost:8002"
AIOPS_URL          = "http://localhost:8000"
GRAFANA_URL        = "http://localhost:3000"


def print_banner(text: str, char: str = "━"):
    width = 62
    print(f"\n{char * width}")
    print(f"  {text}")
    print(f"{char * width}")


def print_step(step: int, text: str):
    print(f"\n[STEP {step}] {text}")
    print("─" * 62)


def check_services():
    """Verify all services are running before starting demo."""
    print_banner("Checking all services are running...", "─")
    services = {
        "AIOps Engine":        f"{AIOPS_URL}/health",
        "Payment API":         f"{PAYMENT_API_URL}/health/live",
        "MongoDB Simulator":   f"{MONGODB_URL}/health/live",
    }
    all_ok = True
    for name, url in services.items():
        try:
            r = requests.get(url, timeout=3)
            status = "✅ UP" if r.status_code == 200 else f"⚠ {r.status_code}"
        except Exception:
            status = "❌ DOWN"
            all_ok = False
        print(f"  {name:<25} {status}")

    if not all_ok:
        print("\n❌ Some services are down. Run: docker compose up -d")
        print("   Wait 30 seconds then retry.")
        return False
    return True


def generate_traffic(duration: int, error_expected: bool = False):
    """Send traffic to payment-api for N seconds."""
    end_time = time.time() + duration
    ok_count = err_count = 0
    while time.time() < end_time:
        try:
            r = requests.post(
                f"{PAYMENT_API_URL}/api/v1/payments",
                params={"amount": 99.99, "method": "card"},
                timeout=5,
            )
            if r.status_code == 200:
                ok_count += 1
            else:
                err_count += 1
        except Exception:
            err_count += 1
        time.sleep(0.2)

    total = ok_count + err_count
    err_pct = (err_count / total * 100) if total > 0 else 0
    print(f"  Traffic sent: {total} requests | "
          f"OK: {ok_count} | "
          f"Errors: {err_count} ({err_pct:.1f}%)")
    return ok_count, err_count


def get_mongodb_status() -> dict:
    """Get current MongoDB simulator state."""
    try:
        return requests.get(f"{MONGODB_URL}/status", timeout=3).json()
    except Exception:
        return {}


def get_recent_incident() -> dict | None:
    """Fetch the most recent incident from the AI engine."""
    try:
        incidents = requests.get(f"{AIOPS_URL}/incidents?limit=1", timeout=5).json()
        return incidents[0] if incidents else None
    except Exception:
        return None


# ── MAIN DEMO FLOW ────────────────────────────────────────────

def run_demo():
    print_banner("🤖 AUTONOMOUS AIOPS — MONGODB INCIDENT SIMULATION", "═")
    print("""
This demo simulates the MongoDB node-maintenance incident
from Sandeep's real production experience at Verizon:

  BEFORE (manual):  Alert missed → users complained → RCA took hours
  AFTER  (AIOps):   AI detects pattern → RCA in 90s → no user impact

Press Enter to start the demo, or Ctrl+C to cancel.
    """)
    input()

    # ── Pre-flight check ──────────────────────────────────────
    if not check_services():
        return

    # ── Step 1: Baseline traffic ──────────────────────────────
    print_step(1, "Generating 20 seconds of NORMAL baseline traffic...")
    print("  This gives Prometheus normal metrics to compare against.")

    traffic_thread = threading.Thread(
        target=generate_traffic, args=(20, False), daemon=True
    )
    traffic_thread.start()

    for i in range(20, 0, -1):
        print(f"\r  Baseline traffic running... {i}s remaining  ", end="", flush=True)
        time.sleep(1)
    traffic_thread.join()
    print(f"\n  Baseline established.")

    # ── Step 2: Trigger MongoDB maintenance ───────────────────
    print_step(2, "🔴 TRIGGERING INCIDENT: MongoDB node → maintenance mode")
    print("  This simulates your exact Verizon incident:")
    print("  Node goes to maintenance → MongoDB pod evicted →")
    print("  ECONNREFUSED errors flood into all dependent services")

    r = requests.post(f"{MONGODB_URL}/simulate-node-maintenance", timeout=5)
    print(f"\n  MongoDB simulator response: {r.json()['message']}")

    status = get_mongodb_status()
    print(f"  Node available: {status.get('node_available', '?')}")
    print(f"  Error rate:     {status.get('error_rate', '?') * 100:.0f}%")

    time.sleep(2)

    # ── Step 3: Send traffic through broken service ───────────
    print_step(3, "Sending traffic through BROKEN service (30 seconds)...")
    print("  Watch what happens:")
    print("  → Payment API returns HTTP 500 errors")
    print("  → ECONNREFUSED logs flood into Loki")
    print("  → Prometheus mongodb_connection_errors_total spikes")

    ok, err = generate_traffic(30, error_expected=True)

    # ── Step 4: Trigger AI engine RCA ─────────────────────────
    print_step(4, "🤖 Triggering AI engine to generate RCA...")
    print("  In production this happens automatically when Grafana alert fires.")
    print("  Simulating alert webhook now...")

    r = requests.post(
        f"{AIOPS_URL}/incidents/simulate",
        params={
            "alert_name": "MongoDBConnectionErrors",
            "service":    "mongodb-simulator",
            "namespace":  "default",
            "severity":   "critical",
        },
        timeout=10,
    )
    print(f"\n  AI Engine response: {r.json().get('message')}")

    # ── Step 5: Wait for RCA ───────────────────────────────────
    print_step(5, "Waiting for Claude to generate RCA...")
    print("  Claude is:")
    print("  1. Querying Prometheus for error rate metrics")
    print("  2. Querying Loki for ECONNREFUSED error logs")
    print("  3. Analysing the pattern")
    print("  4. Generating structured root cause analysis")
    print()

    rca = None
    for attempt in range(30):
        time.sleep(3)
        print(f"\r  Waiting for RCA... {(attempt+1)*3}s elapsed", end="", flush=True)
        latest = get_recent_incident()
        if latest and latest.get("root_cause") and "Claude API key" not in latest.get("root_cause", ""):
            rca = latest
            break

    print()

    # ── Step 6: Display RCA ───────────────────────────────────
    print_step(6, "🔎 RCA RESULT FROM CLAUDE")
    if rca:
        steps = json.loads(rca.get("remediation", "[]"))
        print(f"""
  ┌─────────────────────────────────────────────────────┐
  │  Incident #{rca['id']:<5}  Severity: {rca['severity']}/5  Confidence: {rca['confidence'] or 'N/A'}
  ├─────────────────────────────────────────────────────┤
  │  ROOT CAUSE:
  │  {rca['root_cause'][:100]}
  │
  │  REMEDIATION STEPS:""")
        for i, step in enumerate(steps, 1):
            print(f"  │  {i}. {step}")
        print(f"""  │
  │  PREVENTION: {(rca.get('prevention') or '')[:70]}
  │
  │  RCA generated in: {rca['rca_seconds'] or 0:.1f}s
  │  Claude cost:      ${rca['cost_usd'] or 0:.4f}
  └─────────────────────────────────────────────────────┘""")
    else:
        print("  ⚠ RCA not yet available. Check http://localhost:8000/incidents/ui")
        print("  (If ANTHROPIC_API_KEY is not set, mock RCA will show instead)")

    # ── Step 7: Restore MongoDB ────────────────────────────────
    print_step(7, "🟢 RESOLVING INCIDENT: Restoring MongoDB node")
    requests.post(f"{MONGODB_URL}/restore", timeout=5)
    print("  Node restored to normal operation.")
    print("  Error rate returning to baseline (<5%)")

    time.sleep(3)
    ok, err = generate_traffic(10)

    # ── Step 8: Summary ───────────────────────────────────────
    print_banner("✅ DEMO COMPLETE — Summary", "═")
    print(f"""
  What just happened:
  ─────────────────────────────────────────────────────
  ✓ Baseline traffic established (normal metrics)
  ✓ MongoDB node went to maintenance (ECONNREFUSED)
  ✓ Payment API returned HTTP 500 errors
  ✓ Error logs flooded Loki
  ✓ Prometheus mongodb_connection_errors_total spiked
  ✓ AI engine generated structured RCA automatically
  ✓ MongoDB restored — errors dropped to baseline

  What to view next:
  ─────────────────────────────────────────────────────
  📊 Incident dashboard:  http://localhost:8000/incidents/ui
  📈 Grafana dashboards:  http://localhost:3000  (admin/admin123)
  🔍 Prometheus metrics:  http://localhost:9090
  📝 Loki logs:           http://localhost:3000 → Explore → Loki

  Key Grafana queries to try in Claude Desktop (via Grafana MCP):
  ─────────────────────────────────────────────────────
  "Show me MongoDB connection errors from the last 30 minutes"
  "What was the HTTP error rate for payment-api during the incident?"
  "Show me ECONNREFUSED logs from the last hour"
    """)


if __name__ == "__main__":
    run_demo()
