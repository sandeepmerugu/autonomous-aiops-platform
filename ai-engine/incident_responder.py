"""
incident_responder.py
────────────────────────────────────────────────────────────────
The core AIOps brain.

WHAT THIS DOES:
  1. Receives alert webhooks from Grafana (POST /webhook)
  2. Automatically queries Prometheus metrics + Loki logs for context
  3. Sends context to Claude API for RCA generation
  4. Stores the incident + RCA in SQLite
  5. Notifies via console (and optionally Slack)
  6. Exposes a web UI at http://localhost:8000 to view all incidents
  7. Exposes /metrics for Prometheus scraping (cost + performance data)

HOW TO RUN:
  uvicorn incident_responder:app --host 0.0.0.0 --port 8000

HOW TO TEST WITHOUT AN ALERT:
  curl -X POST http://localhost:8000/webhook \
    -H "Content-Type: application/json" \
    -d @demo/sample_alert.json
────────────────────────────────────────────────────────────────
"""

import os
import json
import time
import sqlite3
import logging
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional

import anthropic
import httpx
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from prometheus_client import make_asgi_app, REGISTRY
from pydantic import BaseModel
from dotenv import load_dotenv

from prompt_templates import get_prompt
from cost_tracker import (
    TimedClaudeCall,
    incidents_processed_total,
    rca_generation_seconds,
    predictive_alerts_total,
)
from grafana_client import (
    get_incident_metrics,
    search_logs,
    get_kubernetes_state,
)

# ── SETUP ─────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("aiops")

ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL        = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
CLAUDE_MAX_TOKENS   = int(os.getenv("CLAUDE_MAX_TOKENS", "1500"))
SLACK_WEBHOOK_URL   = os.getenv("SLACK_WEBHOOK_URL", "")
NOTIFICATION_MODE   = os.getenv("NOTIFICATION_MODE", "console")
DB_PATH             = os.getenv("DB_PATH", "/tmp/incidents.db")

if not ANTHROPIC_API_KEY:
    logger.warning(
        "ANTHROPIC_API_KEY not set — RCA generation will be skipped. "
        "Add your key to .env to enable AI-powered analysis."
    )

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


# ── DATABASE ──────────────────────────────────────────────────

def init_db():
    """Create the incidents table if it doesn't exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_name     TEXT NOT NULL,
            service        TEXT,
            namespace      TEXT,
            severity       INTEGER,
            status         TEXT DEFAULT 'open',
            root_cause     TEXT,
            confidence     TEXT,
            remediation    TEXT,
            prevention     TEXT,
            escalate       BOOLEAN DEFAULT 0,
            input_tokens   INTEGER DEFAULT 0,
            output_tokens  INTEGER DEFAULT 0,
            cost_usd       REAL DEFAULT 0,
            rca_seconds    REAL DEFAULT 0,
            raw_alert      TEXT,
            created_at     TEXT DEFAULT (datetime('now')),
            resolved_at    TEXT
        )
    """)
    conn.commit()
    conn.close()
    logger.info(f"Database ready: {DB_PATH}")


def save_incident(data: dict) -> int:
    """Save an incident to SQLite. Returns the incident ID."""
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.execute("""
        INSERT INTO incidents
          (alert_name, service, namespace, severity, status,
           root_cause, confidence, remediation, prevention, escalate,
           input_tokens, output_tokens, cost_usd, rca_seconds, raw_alert)
        VALUES
          (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        data.get("alert_name"),
        data.get("service"),
        data.get("namespace"),
        data.get("severity"),
        data.get("status", "open"),
        data.get("root_cause"),
        data.get("confidence"),
        json.dumps(data.get("remediation_steps", [])),
        data.get("prevention"),
        data.get("escalate", False),
        data.get("input_tokens", 0),
        data.get("output_tokens", 0),
        data.get("cost_usd", 0),
        data.get("rca_seconds", 0),
        json.dumps(data.get("raw_alert", {})),
    ))
    incident_id = cur.lastrowid
    conn.commit()
    conn.close()
    return incident_id


def get_all_incidents(limit: int = 50) -> list[dict]:
    """Fetch recent incidents from SQLite."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── ALERT PARSING ─────────────────────────────────────────────

class AlertPayload(BaseModel):
    """
    Grafana alert webhook payload.
    Grafana sends this format when an alert fires.
    """
    receiver: Optional[str] = "aiops-engine"
    status: Optional[str] = "firing"
    alerts: list[dict] = []
    # Grafana sometimes sends these top-level fields
    title: Optional[str] = None
    message: Optional[str] = None


def extract_alert_info(alert: dict) -> dict:
    """Extract key fields from a single Grafana alert object."""
    labels      = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    return {
        "alert_name":        labels.get("alertname", "UnknownAlert"),
        "service":           labels.get("service") or labels.get("job", "unknown"),
        "namespace":         labels.get("namespace", "default"),
        "alert_severity":    labels.get("severity", "unknown"),
        "alert_description": annotations.get("description") or annotations.get("summary", ""),
        "fired_at":          alert.get("startsAt", datetime.now(timezone.utc).isoformat()),
    }


# ── RCA GENERATION ────────────────────────────────────────────

async def generate_rca(alert_info: dict, raw_alert: dict) -> dict:
    """
    Main RCA pipeline:
      1. Collect metrics from Prometheus
      2. Collect logs from Loki
      3. Get Kubernetes state
      4. Send all context to Claude
      5. Parse and return structured RCA

    Returns a dict that gets saved to the database.
    """
    service   = alert_info["service"]
    namespace = alert_info["namespace"]
    rca_start = time.time()

    logger.info(
        f"━━━ RCA STARTED ━━━ "
        f"alert={alert_info['alert_name']} | "
        f"service={service} | namespace={namespace}"
    )

    # ── Step 1: Collect Prometheus metrics ───────────────────
    logger.info("  [1/4] Fetching Prometheus metrics...")
    prometheus_metrics = await get_incident_metrics(service, namespace)

    # ── Step 2: Collect Loki logs ─────────────────────────────
    logger.info("  [2/4] Querying Loki for error logs...")
    loki_logs = await search_logs(namespace=namespace, service=service, level="error")

    # ── Step 3: Get Kubernetes state ──────────────────────────
    logger.info("  [3/4] Fetching Kubernetes state...")
    k8s_state = await get_kubernetes_state(namespace)

    # ── Step 4: Send to Claude ────────────────────────────────
    logger.info("  [4/4] Calling Claude API for RCA generation...")

    if not claude_client:
        logger.warning("No API key — returning mock RCA.")
        rca_seconds = round(time.time() - rca_start, 2)
        return {
            "alert_name":       alert_info["alert_name"],
            "service":          service,
            "namespace":        namespace,
            "severity":         3,
            "status":           "open",
            "root_cause":       "Claude API key not configured — manual analysis required.",
            "confidence":       "N/A",
            "remediation_steps": ["Add ANTHROPIC_API_KEY to .env and restart the AI engine."],
            "prevention":       "Configure API key for automated RCA.",
            "escalate":         True,
            "input_tokens":     0,
            "output_tokens":    0,
            "cost_usd":         0,
            "rca_seconds":      rca_seconds,
            "raw_alert":        raw_alert,
        }

    # Get versioned prompt
    template = get_prompt("incident_rca")
    user_prompt = template.user_prompt_template.format(
        alert_name=alert_info["alert_name"],
        service=service,
        namespace=namespace,
        alert_severity=alert_info["alert_severity"],
        fired_at=alert_info["fired_at"],
        alert_description=alert_info["alert_description"],
        prometheus_metrics=prometheus_metrics,
        loki_logs=loki_logs,
        k8s_state=k8s_state,
    )

    rca_data = {}
    with TimedClaudeCall("incident_rca") as timer:
        try:
            response = claude_client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                system=template.system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            timer.record(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                model=CLAUDE_MODEL,
            )

            # Parse the JSON response from Claude
            raw_text = response.content[0].text.strip()
            # Strip markdown code fences if Claude added them
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
            rca_data = json.loads(raw_text.strip())

            rca_data["input_tokens"]  = response.usage.input_tokens
            rca_data["output_tokens"] = response.usage.output_tokens
            rca_data["cost_usd"]      = timer.usage_info["cost_usd"] if timer.usage_info else 0

        except json.JSONDecodeError:
            logger.error("Claude returned non-JSON — using raw text as root_cause.")
            rca_data = {
                "root_cause":       response.content[0].text[:500],
                "severity":         3,
                "confidence":       "LOW",
                "remediation_steps": ["Manual investigation required."],
                "prevention":       "Improve prompt template to ensure JSON output.",
                "escalate":         True,
                "related_runbooks": [],
            }
        except Exception as e:
            logger.error(f"Claude API call failed: {e}")
            rca_data = {
                "root_cause":       f"RCA generation failed: {e}",
                "severity":         4,
                "confidence":       "LOW",
                "remediation_steps": ["Manual investigation required."],
                "prevention":       "Check API key and network connectivity.",
                "escalate":         True,
                "related_runbooks": [],
            }

    rca_seconds = round(time.time() - rca_start, 2)
    rca_generation_seconds.observe(rca_seconds)

    result = {
        "alert_name":       alert_info["alert_name"],
        "service":          service,
        "namespace":        namespace,
        "severity":         rca_data.get("severity", 3),
        "status":           "escalated" if rca_data.get("escalate") else "open",
        "root_cause":       rca_data.get("root_cause", "Unknown"),
        "confidence":       rca_data.get("confidence", "LOW"),
        "remediation_steps": rca_data.get("remediation_steps", []),
        "prevention":       rca_data.get("prevention", ""),
        "escalate":         rca_data.get("escalate", False),
        "input_tokens":     rca_data.get("input_tokens", 0),
        "output_tokens":    rca_data.get("output_tokens", 0),
        "cost_usd":         rca_data.get("cost_usd", 0),
        "rca_seconds":      rca_seconds,
        "raw_alert":        raw_alert,
    }

    return result


# ── NOTIFICATION ──────────────────────────────────────────────

async def notify(incident_id: int, rca: dict):
    """
    Send incident notification.
    Mode is controlled by NOTIFICATION_MODE env var:
      console — always prints to terminal (default)
      slack   — posts to Slack webhook
      both    — console + Slack
    """
    sev_emoji = {1: "🟢", 2: "🔵", 3: "🟡", 4: "🟠", 5: "🔴"}.get(rca["severity"], "⚪")
    escalate  = "⚠ ESCALATE IMMEDIATELY" if rca["escalate"] else "✓ Can be resolved by on-call"

    steps_text = "\n".join(
        f"  {i+1}. {s}"
        for i, s in enumerate(rca.get("remediation_steps", []))
    )

    message = f"""
╔══════════════════════════════════════════════════════════════╗
║             AUTONOMOUS AIOPS — INCIDENT #{incident_id:<5}               ║
╠══════════════════════════════════════════════════════════════╣
║  Alert    : {rca['alert_name']:<48} ║
║  Service  : {rca['service']:<48} ║
║  Namespace: {rca['namespace']:<48} ║
║  Severity : {sev_emoji} {rca['severity']}/5  |  Confidence: {rca['confidence']:<30} ║
╠══════════════════════════════════════════════════════════════╣
║  ROOT CAUSE                                                  ║
╟──────────────────────────────────────────────────────────────╢
  {rca['root_cause']}

╠══════════════════════════════════════════════════════════════╣
║  REMEDIATION STEPS                                           ║
╟──────────────────────────────────────────────────────────────╢
{steps_text}

╠══════════════════════════════════════════════════════════════╣
║  PREVENTION : {rca['prevention'][:53]:<53} ║
║  ACTION     : {escalate:<53} ║
╠══════════════════════════════════════════════════════════════╣
║  RCA Time  : {rca['rca_seconds']:.1f}s  |  Tokens: {rca['input_tokens']+rca['output_tokens']}  |  Cost: ${rca['cost_usd']:.4f}          ║
║  Dashboard : http://localhost:8000/incidents                 ║
╚══════════════════════════════════════════════════════════════╝
"""

    if NOTIFICATION_MODE in ("console", "both"):
        print(message)

    if NOTIFICATION_MODE in ("slack", "both") and SLACK_WEBHOOK_URL:
        slack_body = {
            "text": f"{sev_emoji} *AIOps Incident #{incident_id}* — {rca['alert_name']}",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text",
                    "text": f"{sev_emoji} Incident #{incident_id}: {rca['alert_name']}"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Service:*\n{rca['service']}"},
                    {"type": "mrkdwn", "text": f"*Severity:*\n{rca['severity']}/5"},
                    {"type": "mrkdwn", "text": f"*Confidence:*\n{rca['confidence']}"},
                    {"type": "mrkdwn", "text": f"*Namespace:*\n{rca['namespace']}"},
                ]},
                {"type": "section",
                    "text": {"type": "mrkdwn",
                             "text": f"*Root Cause:*\n{rca['root_cause']}"}},
                {"type": "section",
                    "text": {"type": "mrkdwn",
                             "text": f"*Steps:*\n" + "\n".join(
                                 f"{i+1}. {s}"
                                 for i, s in enumerate(rca.get("remediation_steps", []))
                             )}},
                {"type": "context", "elements": [{"type": "mrkdwn",
                    "text": f"RCA in {rca['rca_seconds']}s | "
                            f"Tokens: {rca['input_tokens']+rca['output_tokens']} | "
                            f"Cost: ${rca['cost_usd']:.4f}"}]},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(SLACK_WEBHOOK_URL, json=slack_body)
        except Exception as e:
            logger.warning(f"Slack notification failed: {e}")


# ── BACKGROUND PROCESSING ─────────────────────────────────────

async def process_alert_background(alert_info: dict, raw_alert: dict):
    """Run the full RCA pipeline in the background."""
    try:
        rca         = await generate_rca(alert_info, raw_alert)
        incident_id = save_incident(rca)
        await notify(incident_id, rca)

        sev_label = str(rca.get("severity", 3))
        status    = rca.get("status", "open")
        incidents_processed_total.labels(
            severity=sev_label, status=status
        ).inc()

        logger.info(f"━━━ RCA COMPLETE ━━━ incident_id={incident_id} "
                    f"severity={rca['severity']} time={rca['rca_seconds']}s "
                    f"cost=${rca['cost_usd']:.4f}")

    except Exception as e:
        logger.exception(f"RCA pipeline failed: {e}")
        incidents_processed_total.labels(severity="unknown", status="failed").inc()


# ── FASTAPI APP ───────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("━" * 60)
    logger.info(" Autonomous AIOps Engine — STARTED")
    logger.info(f" Model:  {CLAUDE_MODEL}")
    logger.info(f" Notify: {NOTIFICATION_MODE}")
    logger.info(f" DB:     {DB_PATH}")
    if not ANTHROPIC_API_KEY:
        logger.warning(" ⚠  No ANTHROPIC_API_KEY — AI analysis disabled")
    logger.info("━" * 60)
    yield


app = FastAPI(
    title="Autonomous AIOps Engine",
    description="AI-powered incident response — Prometheus + Loki + Claude",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount Prometheus metrics endpoint
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


# ── API ENDPOINTS ─────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check — used by Kubernetes readiness/liveness probes."""
    return {
        "status":    "healthy",
        "model":     CLAUDE_MODEL,
        "ai_enabled": bool(claude_client),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/webhook")
async def receive_alert(request: Request, background_tasks: BackgroundTasks):
    """
    Receive Grafana alert webhook.

    Configure Grafana contact point:
      Type: Webhook
      URL:  http://aiops-engine:8000/webhook

    Grafana fires this when an alert rule triggers.
    The AI engine processes it in the background (non-blocking).
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    logger.info(f"Webhook received: {json.dumps(body)[:200]}...")

    alerts = body.get("alerts", [])
    if not alerts:
        # Handle single alert or Grafana unified alerting format
        if "labels" in body:
            alerts = [body]
        else:
            return JSONResponse({"status": "ok", "processed": 0})

    for alert in alerts:
        if alert.get("status", "firing") != "firing":
            continue   # skip resolved alerts
        alert_info = extract_alert_info(alert)
        background_tasks.add_task(
            process_alert_background, alert_info, body
        )
        logger.info(
            f"Queued RCA for: {alert_info['alert_name']} "
            f"/ {alert_info['service']} / {alert_info['namespace']}"
        )

    return JSONResponse({
        "status":    "accepted",
        "processed": len(alerts),
        "message":   "RCA generation started — check /incidents for results",
    })


@app.post("/incidents/simulate")
async def simulate_incident(
    background_tasks: BackgroundTasks,
    alert_name: str = "MongoDBConnectionErrors",
    service: str    = "mongodb-simulator",
    namespace: str  = "default",
    severity: str   = "critical",
):
    """
    Manually trigger an incident WITHOUT a real alert.
    Use this for demos and testing.

    curl -X POST "http://localhost:8000/incidents/simulate?alert_name=MongoDBConnectionErrors"
    """
    mock_alert = {
        "alerts": [{
            "status":   "firing",
            "labels": {
                "alertname": alert_name,
                "service":   service,
                "namespace": namespace,
                "severity":  severity,
            },
            "annotations": {
                "description": (
                    f"Simulated incident: {alert_name} in {service}. "
                    "Use this to test the RCA pipeline without a real alert."
                ),
                "summary": f"{alert_name} detected in {namespace}",
            },
            "startsAt": datetime.now(timezone.utc).isoformat(),
        }]
    }
    alert_info = extract_alert_info(mock_alert["alerts"][0])
    background_tasks.add_task(process_alert_background, alert_info, mock_alert)
    return {
        "status":  "simulation started",
        "alert":   alert_name,
        "service": service,
        "message": "RCA will appear in /incidents within ~30 seconds",
    }


@app.get("/incidents")
async def list_incidents(limit: int = 20):
    """Return recent incidents as JSON."""
    return get_all_incidents(limit=limit)


@app.get("/incidents/ui", response_class=HTMLResponse)
async def incidents_ui():
    """
    Human-readable incident dashboard.
    Open http://localhost:8000/incidents/ui in your browser.
    """
    incidents = get_all_incidents(limit=20)

    rows = ""
    for inc in incidents:
        steps = json.loads(inc.get("remediation", "[]"))
        steps_html = "".join(f"<li>{s}</li>" for s in steps)
        sev_color = {
            1: "#28a745", 2: "#17a2b8",
            3: "#ffc107", 4: "#fd7e14", 5: "#dc3545"
        }.get(inc.get("severity", 3), "#6c757d")
        rows += f"""
        <tr>
          <td>{inc['id']}</td>
          <td>{inc['created_at']}</td>
          <td>{inc['alert_name']}</td>
          <td>{inc['service']}</td>
          <td>{inc['namespace']}</td>
          <td><span style="color:{sev_color};font-weight:bold">{inc['severity']}/5</span></td>
          <td>{inc['confidence'] or 'N/A'}</td>
          <td>{inc['root_cause'] or 'Processing...'}</td>
          <td><ol style="margin:0;padding-left:16px">{steps_html}</ol></td>
          <td>{inc['rca_seconds'] or 0:.1f}s</td>
          <td>${inc['cost_usd'] or 0:.4f}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head>
  <title>AIOps Incident Dashboard</title>
  <style>
    body{{font-family:Calibri,Arial,sans-serif;margin:20px;background:#f5f5f5}}
    h1{{color:#1a1a2e}} table{{width:100%;border-collapse:collapse;background:#fff;
    border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)}}
    th{{background:#1a1a2e;color:#fff;padding:10px;text-align:left;font-size:12px}}
    td{{padding:8px;border-bottom:1px solid #eee;font-size:12px;vertical-align:top}}
    tr:hover{{background:#f0f4ff}} .badge{{padding:3px 8px;border-radius:12px;
    color:#fff;font-size:11px}}
    .refresh{{float:right;color:#666;font-size:12px}}
    .simulate-btn{{background:#1a1a2e;color:#fff;border:none;padding:8px 16px;
    border-radius:4px;cursor:pointer;margin:10px 0}}
  </style>
  <meta http-equiv="refresh" content="10">
</head>
<body>
  <h1>🤖 Autonomous AIOps — Incident Dashboard
    <span class="refresh">Auto-refreshes every 10s</span>
  </h1>
  <p>
    <a href="/health">Health</a> |
    <a href="/metrics">Prometheus Metrics</a> |
    <a href="/incidents">JSON API</a>
  </p>
  <button class="simulate-btn"
    onclick="fetch('/incidents/simulate?alert_name=MongoDBConnectionErrors',
    {{method:'POST'}}).then(()=>setTimeout(()=>location.reload(),5000))">
    ▶ Simulate MongoDB Incident
  </button>
  <table>
    <thead>
      <tr>
        <th>#</th><th>Time</th><th>Alert</th><th>Service</th>
        <th>Namespace</th><th>Severity</th><th>Confidence</th>
        <th>Root Cause</th><th>Steps</th><th>Time</th><th>Cost</th>
      </tr>
    </thead>
    <tbody>{rows if rows else '<tr><td colspan="11" style="text-align:center;padding:40px;color:#999">No incidents yet. Click Simulate or wait for a real alert.</td></tr>'}</tbody>
  </table>
</body>
</html>"""
