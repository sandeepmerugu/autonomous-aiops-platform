"""
payment-api/app.py
────────────────────────────────────────────────────────────────
A realistic payment service that generates real Prometheus metrics.
Designed to produce meaningful observability data for the demo.

What this app does:
  - Serves real HTTP endpoints with real latency simulation
  - Randomly generates HTTP 500 errors (configurable rate)
  - Exposes Prometheus metrics at /metrics
  - Logs structured errors that Loki/Promtail will pick up
  - Has a /break endpoint to trigger a failure cascade for the demo
────────────────────────────────────────────────────────────────
"""

import os
import time
import random
import logging
from datetime import datetime

from fastapi import FastAPI, Response
from prometheus_client import (
    Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST,
)
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | service=payment-api | %(message)s',
)
logger = logging.getLogger("payment-api")

# ── CONFIG ────────────────────────────────────────────────────
ERROR_RATE    = float(os.getenv("ERROR_RATE", "0.05"))      # 5% errors by default
BASE_LATENCY  = float(os.getenv("BASE_LATENCY_MS", "50"))   # ms
SERVICE_NAME  = "payment-api"
NAMESPACE     = os.getenv("NAMESPACE", "default")

# Whether to force all requests to fail (set by /break endpoint)
_force_error = False

# ── PROMETHEUS METRICS ────────────────────────────────────────
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status", "service"],
)

http_request_duration = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration",
    ["method", "endpoint", "service"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

active_connections = Gauge(
    "active_connections",
    "Current active connections",
    ["service"],
)

payment_processed_total = Counter(
    "payment_processed_total",
    "Total payments processed",
    ["status", "method"],   # status: success|failed, method: card|bank|wallet
)

mongodb_connection_errors_total = Counter(
    "mongodb_connection_errors_total",
    "MongoDB connection errors",
    ["namespace"],
)

# ── APP ───────────────────────────────────────────────────────
app = FastAPI(title="Payment API", description="Sample payment service for AIOps demo")


def should_error() -> bool:
    """Decide whether this request should return an error."""
    if _force_error:
        return True
    return random.random() < ERROR_RATE


def simulate_latency():
    """Add realistic latency variation."""
    jitter = random.gauss(0, BASE_LATENCY * 0.3)
    latency_ms = max(10, BASE_LATENCY + jitter)
    time.sleep(latency_ms / 1000)


@app.middleware("http")
async def metrics_middleware(request, call_next):
    """Record metrics for every request automatically."""
    start = time.time()
    active_connections.labels(service=SERVICE_NAME).inc()

    try:
        response = await call_next(request)
        duration  = time.time() - start
        endpoint  = request.url.path
        method    = request.method

        http_requests_total.labels(
            method=method,
            endpoint=endpoint,
            status=str(response.status_code),
            service=SERVICE_NAME,
        ).inc()
        http_request_duration.labels(
            method=method, endpoint=endpoint, service=SERVICE_NAME
        ).observe(duration)

        return response
    finally:
        active_connections.labels(service=SERVICE_NAME).dec()


# ── ENDPOINTS ─────────────────────────────────────────────────

@app.get("/health/live")
async def liveness():
    return {"status": "alive", "service": SERVICE_NAME}


@app.get("/health/ready")
async def readiness():
    if _force_error:
        return Response(
            content='{"status":"not ready","reason":"forced error mode"}',
            status_code=503,
            media_type="application/json",
        )
    return {"status": "ready", "service": SERVICE_NAME}


@app.post("/api/v1/payments")
async def process_payment(amount: float = 100.0, method: str = "card"):
    """Simulate payment processing with realistic errors."""
    simulate_latency()

    if should_error():
        # Simulate MongoDB connection error (your real incident scenario)
        mongodb_connection_errors_total.labels(namespace=NAMESPACE).inc()
        logger.error(
            f"MongoDB connection timeout processing payment — "
            f"amount={amount} method={method} "
            f"error=connection refused to mongo:27017"
        )
        payment_processed_total.labels(status="failed", method=method).inc()
        return Response(
            content='{"error":"Database connection failed","code":"DB_TIMEOUT"}',
            status_code=500,
            media_type="application/json",
        )

    payment_id = f"PAY-{random.randint(100000, 999999)}"
    logger.info(f"Payment processed — id={payment_id} amount={amount} method={method}")
    payment_processed_total.labels(status="success", method=method).inc()
    return {
        "payment_id": payment_id,
        "status":     "success",
        "amount":     amount,
        "method":     method,
        "timestamp":  datetime.utcnow().isoformat(),
    }


@app.get("/api/v1/payments/{payment_id}")
async def get_payment(payment_id: str):
    """Retrieve a payment by ID."""
    simulate_latency()
    if should_error():
        logger.error(f"Failed to retrieve payment={payment_id} — MongoDB read timeout")
        mongodb_connection_errors_total.labels(namespace=NAMESPACE).inc()
        return Response(status_code=500,
                        content='{"error":"Database read timeout"}',
                        media_type="application/json")
    return {"payment_id": payment_id, "status": "completed", "amount": 150.00}


@app.get("/api/v1/transactions")
async def list_transactions():
    """List recent transactions."""
    simulate_latency()
    if should_error():
        logger.error("Failed to list transactions — connection pool exhausted")
        mongodb_connection_errors_total.labels(namespace=NAMESPACE).inc()
        return Response(status_code=500,
                        content='{"error":"Connection pool exhausted"}',
                        media_type="application/json")
    return {"transactions": [], "total": 0, "page": 1}


# ── DEMO CONTROL ENDPOINTS ────────────────────────────────────

@app.post("/break")
async def break_service():
    """
    Force all subsequent requests to fail.
    Used by simulate_mongodb_incident.py to create a real incident.

    curl -X POST http://localhost:8001/break
    """
    global _force_error
    _force_error = True
    logger.error("💥 Service BROKEN — all requests will return 500 errors")
    return {"status": "broken", "message": "All requests now return 500"}


@app.post("/heal")
async def heal_service():
    """
    Restore normal operation after a simulated incident.

    curl -X POST http://localhost:8001/heal
    """
    global _force_error
    _force_error = False
    logger.info("✅ Service HEALED — back to normal operation")
    return {"status": "healthy", "message": "Service restored to normal"}


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus scrape endpoint."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
