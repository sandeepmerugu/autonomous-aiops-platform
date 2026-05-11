"""
grafana_client.py
────────────────────────────────────────────────────────────────
Programmatic client for querying Prometheus, Loki, and Grafana.

HOW THIS RELATES TO GRAFANA MCP:
  When you type in Claude browser:
    "show me error logs from the last 15 minutes in namespace payments"

  Claude calls these Grafana MCP tools behind the scenes:
    → search_logs(query='{namespace="payments"} |= "error"', since="15m")
    → query_prometheus(expr='rate(http_requests_total{status=~"5.."}[5m])')

  THIS FILE does the exact same thing — but from Python code,
  automatically triggered by an alert instead of by you typing.

  The queries are identical. The only difference is:
    Claude browser  = YOU trigger it manually
    This file       = ALERT triggers it automatically
────────────────────────────────────────────────────────────────
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
LOKI_URL       = os.getenv("LOKI_URL",       "http://loki:3100")
GRAFANA_URL    = os.getenv("GRAFANA_URL",     "http://grafana:3000")
GRAFANA_USER   = os.getenv("GRAFANA_USER",    "admin")
GRAFANA_PASS   = os.getenv("GRAFANA_PASSWORD","admin123")
TIMEOUT        = 15  # seconds


# ── PROMETHEUS CLIENT ─────────────────────────────────────────

async def query_prometheus(expr: str, time_range_minutes: int = 15) -> dict:
    """
    Run a PromQL instant query against Prometheus.

    Equivalent to: Claude MCP tool → query_prometheus(expr=...)

    Args:
        expr: PromQL expression e.g. 'rate(http_requests_total[5m])'
        time_range_minutes: look-back window baked into range vectors

    Returns:
        dict with keys: success, data, error
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": expr},
            )
            resp.raise_for_status()
            result = resp.json()

            if result.get("status") != "success":
                return {"success": False, "data": [], "error": result.get("error")}

            return {
                "success": True,
                "data": result.get("data", {}).get("result", []),
                "error": None,
            }
    except Exception as e:
        logger.error(f"Prometheus query failed: {expr} — {e}")
        return {"success": False, "data": [], "error": str(e)}


async def query_prometheus_range(
    expr: str,
    hours: int = 2,
    step: str = "1m",
) -> dict:
    """
    Run a PromQL range query — returns time series data.
    Used by the predictive monitor to fetch historical trends.
    """
    end   = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                params={
                    "query": expr,
                    "start": start.timestamp(),
                    "end":   end.timestamp(),
                    "step":  step,
                },
            )
            resp.raise_for_status()
            result = resp.json()
            return {
                "success": True,
                "data": result.get("data", {}).get("result", []),
                "error": None,
            }
    except Exception as e:
        logger.error(f"Prometheus range query failed — {e}")
        return {"success": False, "data": [], "error": str(e)}


async def get_incident_metrics(service: str, namespace: str) -> str:
    """
    Gather all relevant metrics for an incident.
    Returns a formatted string ready to be injected into the Claude prompt.

    This mimics what you do manually in Claude browser using Grafana MCP
    but runs automatically when an alert fires.
    """
    metrics_lines = []

    # 1. HTTP error rate
    error_rate_result = await query_prometheus(
        f'rate(http_requests_total{{service="{service}",status=~"5.."}}[5m])'
    )
    if error_rate_result["success"] and error_rate_result["data"]:
        for item in error_rate_result["data"][:3]:
            val = round(float(item["value"][1]) * 100, 2)
            metrics_lines.append(f"HTTP 5xx Error Rate: {val}% per second")

    # 2. Pod restart count
    restart_result = await query_prometheus(
        f'increase(kube_pod_container_status_restarts_total'
        f'{{namespace="{namespace}"}}[15m])'
    )
    if restart_result["success"] and restart_result["data"]:
        for item in restart_result["data"][:5]:
            pod  = item["metric"].get("pod", "unknown")
            val  = round(float(item["value"][1]), 0)
            if val > 0:
                metrics_lines.append(f"Pod '{pod}' restarts in last 15m: {int(val)}")

    # 3. CPU usage
    cpu_result = await query_prometheus(
        f'rate(container_cpu_usage_seconds_total'
        f'{{namespace="{namespace}",container!=""}}[5m])'
    )
    if cpu_result["success"] and cpu_result["data"]:
        for item in cpu_result["data"][:3]:
            container = item["metric"].get("container", "unknown")
            val       = round(float(item["value"][1]) * 100, 1)
            metrics_lines.append(f"Container '{container}' CPU: {val}%")

    # 4. Memory usage
    mem_result = await query_prometheus(
        f'container_memory_working_set_bytes'
        f'{{namespace="{namespace}",container!=""}}'
    )
    if mem_result["success"] and mem_result["data"]:
        for item in mem_result["data"][:3]:
            container = item["metric"].get("container", "unknown")
            mb        = round(float(item["value"][1]) / 1024 / 1024, 1)
            metrics_lines.append(f"Container '{container}' Memory: {mb} MB")

    # 5. MongoDB-specific: connection errors (your real incident scenario)
    mongo_result = await query_prometheus(
        f'increase(mongodb_connection_errors_total'
        f'{{namespace="{namespace}"}}[15m])'
    )
    if mongo_result["success"] and mongo_result["data"]:
        for item in mongo_result["data"][:3]:
            val = round(float(item["value"][1]), 0)
            if val > 0:
                metrics_lines.append(
                    f"MongoDB connection errors in last 15m: {int(val)}"
                )

    if not metrics_lines:
        return "No metric data available for this service in Prometheus."

    return "\n".join(f"  • {line}" for line in metrics_lines)


# ── LOKI CLIENT ───────────────────────────────────────────────

async def search_logs(
    namespace: str,
    service: Optional[str] = None,
    level: str = "error",
    minutes: int = 15,
    limit: int = 20,
) -> str:
    """
    Search Loki for logs from a namespace/service.

    Equivalent to: Claude MCP tool → search_logs(...)

    This is the EXACT same query you run manually in Claude browser
    when you type "show me error logs from the last 15 minutes in
    namespace X" — now automated and triggered by alerts.
    """
    # Build LogQL query
    selector = f'{{namespace="{namespace}"}}'
    if service:
        selector = f'{{namespace="{namespace}", service="{service}"}}'

    # Filter by log level
    logql = f'{selector} |= "{level}"'

    end_ns   = int(datetime.now(timezone.utc).timestamp() * 1e9)
    start_ns = int((datetime.now(timezone.utc) - timedelta(minutes=minutes))
                   .timestamp() * 1e9)

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                f"{LOKI_URL}/loki/api/v1/query_range",
                params={
                    "query": logql,
                    "start": start_ns,
                    "end":   end_ns,
                    "limit": limit,
                    "direction": "backward",
                },
            )
            resp.raise_for_status()
            result = resp.json()

            streams = result.get("data", {}).get("result", [])
            if not streams:
                return f"No {level} logs found in namespace '{namespace}' (last {minutes}m)."

            log_lines = []
            for stream in streams:
                for ts, line in stream.get("values", []):
                    # Convert nanosecond timestamp to human-readable
                    dt  = datetime.fromtimestamp(int(ts) / 1e9, tz=timezone.utc)
                    log_lines.append(f"  [{dt.strftime('%H:%M:%S')}] {line[:200]}")
                    if len(log_lines) >= limit:
                        break

            if not log_lines:
                return "No log entries found."

            return f"Last {len(log_lines)} {level.upper()} logs " \
                   f"(namespace={namespace}, last {minutes}m):\n" + \
                   "\n".join(log_lines)

    except Exception as e:
        logger.error(f"Loki query failed — {e}")
        return f"Loki unavailable — could not fetch logs: {e}"


# ── GRAFANA CLIENT ────────────────────────────────────────────

async def get_firing_alert_rules() -> list[dict]:
    """Fetch all currently firing alert rules from Grafana."""
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT,
            auth=(GRAFANA_USER, GRAFANA_PASS)
        ) as client:
            resp = await client.get(f"{GRAFANA_URL}/api/prometheus/grafana/api/v1/rules")
            resp.raise_for_status()
            groups = resp.json().get("data", {}).get("groups", [])
            firing = []
            for group in groups:
                for rule in group.get("rules", []):
                    if rule.get("state") == "firing":
                        firing.append({
                            "name":   rule.get("name"),
                            "labels": rule.get("labels", {}),
                            "state":  rule.get("state"),
                        })
            return firing
    except Exception as e:
        logger.warning(f"Could not fetch Grafana alert rules — {e}")
        return []


async def get_kubernetes_state(namespace: str) -> str:
    """
    Get relevant Kubernetes state from Prometheus kube-state-metrics.
    Returns a human-readable summary for the Claude prompt.
    """
    lines = []

    # Pod phases
    pod_result = await query_prometheus(
        f'kube_pod_status_phase{{namespace="{namespace}"}}'
    )
    if pod_result["success"]:
        phase_counts: dict = {}
        for item in pod_result["data"]:
            phase = item["metric"].get("phase", "Unknown")
            val   = int(float(item["value"][1]))
            if val > 0:
                phase_counts[phase] = phase_counts.get(phase, 0) + val
        for phase, count in phase_counts.items():
            lines.append(f"Pods in phase '{phase}': {count}")

    # Ready pods
    ready_result = await query_prometheus(
        f'kube_deployment_status_replicas_ready{{namespace="{namespace}"}}'
    )
    if ready_result["success"]:
        for item in ready_result["data"][:5]:
            dep = item["metric"].get("deployment", "unknown")
            val = int(float(item["value"][1]))
            lines.append(f"Deployment '{dep}' ready replicas: {val}")

    if not lines:
        return f"No Kubernetes state data available for namespace '{namespace}'."
    return "\n".join(f"  • {l}" for l in lines)
