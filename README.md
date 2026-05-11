# Autonomous AIOps Platform
### AI-Powered Incident Response with Prometheus · Loki · Grafana MCP · Claude

> **One-command demo** — detects production incidents, generates AI-powered root cause analysis using Claude, and posts actionable remediation steps — fully automated, zero human trigger required.

---

## The Problem This Solves

In a real incident at Verizon, a MongoDB node went into maintenance at 2 AM:

```
2 AM   → Node enters maintenance window
2 AM   → MongoDB pod evicted, connection errors start
3 AM   → Users start seeing 500 errors on payment service
3 AM   → On-call engineer paged (1 hour after incident started)
4 AM   → Root cause found manually (2 hours of investigation)
```

**With this platform:**

```
2 AM   → Node enters maintenance window
2:00   → MongoDB connection errors spike in Prometheus
2:01   → AI engine detects pattern, queries Loki for ECONNREFUSED logs
2:01   → Claude generates RCA in 90 seconds
2:02   → On-call engineer receives: "MongoDB node eviction — restart pod or
          migrate to managed MongoDB (Atlas/DocumentDB) for auto-failover"
2:02   → Incident resolved before users notice
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    YOUR SERVICES                                 │
│   payment-api (:8001)       mongodb-simulator (:8002)           │
│        │                              │                          │
│        └──────── metrics + logs ──────┘                         │
└─────────────────────────┬────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│                   OBSERVABILITY LAYER                            │
│                                                                  │
│  Prometheus (:9090) ─── scrapes metrics every 15s               │
│  Loki       (:3100) ─── receives logs via Promtail               │
│  Promtail          ─── collects Docker container logs           │
│  Grafana    (:3000) ─── dashboards + alert rules                │
│                                │                                 │
│              Grafana alert fires webhook                         │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                    AIOPS ENGINE (:8000)                          │
│                                                                  │
│  1. Alert Receiver  (FastAPI webhook endpoint)                   │
│        │                                                         │
│  2. Context Collector                                            │
│        ├── Prometheus: error rates, pod restarts, CPU, memory   │
│        ├── Loki:       ECONNREFUSED, ERROR, FAILED log lines     │
│        └── K8s:        pod phases, ready replicas               │
│        │                                                         │
│  3. Claude API  ──── structured RCA prompt ────► JSON response  │
│        │             (versioned prompt v1.0)                     │
│  4. Cost Tracker ─── records tokens + cost → Prometheus         │
│        │                                                         │
│  5. Notification ─── console output + optional Slack            │
│        │                                                         │
│  6. SQLite Store ─── persists all incidents + RCAs              │
│        │                                                         │
│  7. Web UI  ──────── http://localhost:8000/incidents/ui          │
└──────────────────────────────────────────────────────────────────┘
                               │
┌──────────────────────────────┴───────────────────────────────────┐
│              PREDICTIVE MONITOR (runs every 60s)                 │
│                                                                  │
│  Fetches 6h historical data from Prometheus                      │
│  Fits linear trend using numpy                                   │
│  If predicted breach within 4h → triggers early warning         │
│                                                                  │
│  Metrics monitored:                                              │
│    • CPU usage > 80%                                             │
│    • Memory usage > 85%                                          │
│    • HTTP error rate > 10%                                       │
│    • MongoDB connection errors > 5/s                             │
│    • Pod restart rate > 0.1/s                                    │
└──────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### Prerequisites
- Docker Desktop installed and running
- Python 3.10+ (for demo scripts)
- Anthropic API key — get one at [console.anthropic.com](https://console.anthropic.com) ($10 credit lasts months)

### Step 1 — Clone and configure

```bash
git clone https://github.com/sandeepmerugu/autonomous-aiops-platform.git
cd autonomous-aiops-platform

cp .env.example .env
```

Open `.env` and add your Anthropic API key:
```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

### Step 2 — Start the platform

```bash
make up
# or: docker compose up -d --build
```

Wait ~30 seconds for all services to start, then open:

| Service | URL | Credentials |
|---|---|---|
| **AIOps Incident Dashboard** | http://localhost:8000/incidents/ui | — |
| **Grafana Dashboards** | http://localhost:3000 | admin / admin123 |
| **Prometheus** | http://localhost:9090 | — |
| **AI Engine API** | http://localhost:8000 | — |

### Step 3 — Run the MongoDB incident demo

```bash
# Terminal 1: Generate background traffic
python demo/load_test.py --threads 5 --duration 300

# Terminal 2: Run the incident simulation
python demo/simulate_mongodb_incident.py
```

**What you will see:**
1. Normal traffic baseline established
2. MongoDB node enters "maintenance" — connection errors spike
3. Payment API starts returning HTTP 500 errors
4. AI engine generates RCA within ~90 seconds
5. RCA appears at http://localhost:8000/incidents/ui
6. MongoDB restored — errors drop back to baseline

### Step 4 — Trigger incidents manually

Without running the full demo script, you can trigger an incident with one command:

```bash
# Simulate MongoDB incident
make simulate-incident

# Break the payment API (all requests fail)
make break-payment-api

# Restore it
make heal-payment-api
```

---

## How Grafana MCP Fits In

You are already using **Grafana MCP with Claude browser** to query your production cluster. This platform automates exactly what you do manually.

**What you type in Claude browser:**
```
"Show me error logs from the last 15 minutes in namespace payments"
"What is the HTTP error rate for payment-api right now?"
"Show me pod restart counts in namespace default"
```

**What `grafana_client.py` does automatically when an alert fires:**
```python
# These are the exact same queries — just automated
logs    = await search_logs(namespace="payments", level="error", minutes=15)
metrics = await query_prometheus('rate(http_requests_total{service="payment-api",status=~"5.."}[5m])')
k8s     = await get_kubernetes_state(namespace="default")
```

The output goes straight into the Claude prompt alongside the alert data.

---

## File Structure

```
autonomous-aiops-platform/
│
├── README.md                              ← You are here
├── docker-compose.yml                     ← Full stack: one command start
├── Makefile                               ← make up | make demo | make logs
├── .env.example                           ← Copy to .env, add API key
│
├── ai-engine/
│   ├── incident_responder.py              ← Core FastAPI app + webhook receiver
│   ├── grafana_client.py                  ← Prometheus + Loki + Grafana API calls
│   ├── predictive_monitor.py              ← Trend analysis + early warning
│   ├── cost_tracker.py                    ← Claude API cost → Prometheus metrics
│   ├── prompt_templates.py                ← Versioned prompts (LLMOps pattern)
│   ├── requirements.txt
│   └── Dockerfile
│
├── sample-apps/
│   ├── payment-api/
│   │   ├── app.py                         ← FastAPI with real Prometheus metrics
│   │   └── Dockerfile
│   └── mongodb-simulator/
│       ├── app.py                         ← MongoDB incident simulation
│       └── Dockerfile
│
├── observability/
│   ├── prometheus/
│   │   ├── prometheus.yml                 ← Scrape configs for all services
│   │   └── alert-rules.yml                ← Alert rules (MongoDB, HTTP errors, etc.)
│   ├── grafana/
│   │   ├── provisioning/                  ← Auto-provisioned datasources + dashboards
│   │   └── dashboards/
│   │       ├── aiops-overview.json        ← Service health + incident metrics
│   │       └── claude-cost-tracker.json   ← AI spend dashboard
│   ├── loki/
│   │   └── loki-config.yml
│   └── promtail/
│       └── promtail-config.yml            ← Docker log collection config
│
└── demo/
    ├── simulate_mongodb_incident.py       ← Full incident simulation script
    └── load_test.py                       ← HTTP traffic generator
```

---

## Grafana Alert → AI Engine Setup

To connect Grafana alerts to the AI engine webhook:

1. Open Grafana → **Alerting → Contact Points → New Contact Point**
2. Type: **Webhook**
3. URL: `http://aiops-engine:8000/webhook`
4. Click **Test** — you should see a test incident appear in the dashboard

Then assign this contact point to any alert rule.

---

## How the RCA Prompt Works

Every Claude API call uses a versioned prompt from `prompt_templates.py`:

```python
# The prompt injects real data — Claude cannot hallucinate metrics
template = get_prompt("incident_rca")   # fetches version "rca_v1.0"

user_prompt = template.format(
    alert_name      = "MongoDBConnectionErrors",
    service         = "payment-api",
    prometheus_metrics = "• MongoDB connection errors: 47.3/s\n• HTTP 5xx rate: 89%",
    loki_logs       = "[02:01:35] MongoNetworkError: connect ECONNREFUSED 10.96.x.x:27017",
    k8s_state       = "• Pods in phase 'Running': 2\n• Pods in phase 'Pending': 1",
)
```

Claude returns structured JSON:
```json
{
  "root_cause": "MongoDB pod was evicted when the underlying node entered maintenance, causing ECONNREFUSED errors across all dependent services.",
  "severity": 4,
  "confidence": "HIGH",
  "remediation_steps": [
    "Check node status: kubectl get nodes — identify node in NotReady state",
    "Manually reschedule MongoDB pod to a healthy node",
    "Migrate from standalone MongoDB to managed service (Atlas/DocumentDB) for automatic failover"
  ],
  "prevention": "Use managed MongoDB with multi-AZ replication to eliminate single-node failure impact.",
  "escalate": false
}
```

---

## Cost Transparency

Every Claude API call is tracked in Prometheus and visible in Grafana:

| Metric | What it tracks |
|---|---|
| `claude_api_calls_total` | Total calls per use_case and status |
| `claude_api_input_tokens_total` | Input tokens per use_case |
| `claude_api_output_tokens_total` | Output tokens per use_case |
| `claude_api_cost_usd_total` | Running USD cost |
| `claude_api_duration_seconds` | How long each call took |

**Typical cost per incident:** $0.003–$0.008 (less than one cent)

---

## Stopping the Platform

```bash
make down              # stop containers, keep data
make clean             # stop containers + delete all data (volumes)
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| AI Engine | Python, FastAPI, Anthropic Claude API |
| Metrics | Prometheus, prometheus-client |
| Logs | Loki, Promtail |
| Dashboards | Grafana (auto-provisioned) |
| Trend Analysis | NumPy linear regression |
| Persistence | SQLite |
| Container runtime | Docker Compose |
| Language | Python 3.11 |

---

## Author

**Sandeep Merugu** — Lead DevOps Consultant  
Built from real production incident experience managing microservices at Verizon and LPL Financial.

[LinkedIn](https://linkedin.com/in/sandeepmerugu) | [GitHub](https://github.com/sandeepmerugu)
