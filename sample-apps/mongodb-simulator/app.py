"""
mongodb-simulator/app.py
────────────────────────────────────────────────────────────────
Simulates the MongoDB node eviction incident from Sandeep's
real production experience at Verizon.

Scenario:
  1. MongoDB is running fine (normal state)
  2. /simulate-node-maintenance endpoint called
  3. Connection errors spike — simulating node going to maintenance
  4. Cascading errors appear in dependent services
  5. /restore endpoint called — node back online, errors stop
  6. Demonstrates: managed MongoDB (like Atlas/DocumentDB) would
     have automatic failover, preventing this incident entirely

Metrics exposed (scraped by Prometheus):
  mongodb_connection_errors_total
  mongodb_connections_active
  mongodb_operations_total
  mongodb_operation_duration_seconds
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
    format='%(asctime)s | %(levelname)s | service=mongodb-simulator | %(message)s',
)
logger = logging.getLogger("mongodb-sim")

NAMESPACE = os.getenv("NAMESPACE", "default")

# ── SIMULATION STATE ──────────────────────────────────────────
_state = {
    "node_available":   True,   # False = node in maintenance
    "error_rate":       0.02,   # 2% baseline errors
    "latency_ms":       5,      # baseline operation latency
}

# ── PROMETHEUS METRICS ────────────────────────────────────────
mongodb_connection_errors_total = Counter(
    "mongodb_connection_errors_total",
    "MongoDB connection errors by type",
    ["namespace", "error_type"],
)

mongodb_connections_active = Gauge(
    "mongodb_connections_active",
    "Currently active MongoDB connections",
    ["namespace"],
)

mongodb_operations_total = Counter(
    "mongodb_operations_total",
    "Total MongoDB operations",
    ["namespace", "operation", "status"],
)

mongodb_operation_duration = Histogram(
    "mongodb_operation_duration_seconds",
    "MongoDB operation duration",
    ["operation"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0],
)

# ── APP ───────────────────────────────────────────────────────
app = FastAPI(title="MongoDB Simulator",
              description="Simulates MongoDB for AIOps incident demo")


def simulate_operation(operation: str) -> dict:
    """Simulate a MongoDB operation with current state."""
    start    = time.time()
    has_err  = random.random() < _state["error_rate"]
    latency  = _state["latency_ms"] + random.gauss(0, _state["latency_ms"] * 0.2)
    latency  = max(1, latency)

    time.sleep(latency / 1000)
    duration = time.time() - start

    if not _state["node_available"]:
        # Node in maintenance — connection refused
        mongodb_connection_errors_total.labels(
            namespace=NAMESPACE,
            error_type="connection_refused",
        ).inc()
        logger.error(
            f"MongoDB {operation} FAILED — "
            f"connection refused to mongo:27017 — "
            f"node is unavailable (maintenance window)"
        )
        mongodb_operations_total.labels(
            namespace=NAMESPACE, operation=operation, status="error"
        ).inc()
        return {"success": False, "error": "connection refused", "code": "ECONNREFUSED"}

    if has_err:
        error_type = random.choice(["timeout", "write_conflict", "cursor_expired"])
        mongodb_connection_errors_total.labels(
            namespace=NAMESPACE, error_type=error_type
        ).inc()
        logger.warning(f"MongoDB {operation} transient error: {error_type}")
        mongodb_operations_total.labels(
            namespace=NAMESPACE, operation=operation, status="error"
        ).inc()
        mongodb_operation_duration.labels(operation=operation).observe(duration)
        return {"success": False, "error": error_type}

    mongodb_operations_total.labels(
        namespace=NAMESPACE, operation=operation, status="success"
    ).inc()
    mongodb_operation_duration.labels(operation=operation).observe(duration)
    return {"success": True, "duration_ms": round(latency, 1)}


# ── ENDPOINTS ─────────────────────────────────────────────────

@app.get("/health/live")
async def liveness():
    return {"status": "alive"}


@app.get("/health/ready")
async def readiness():
    if not _state["node_available"]:
        return Response(
            content='{"status":"not ready","reason":"MongoDB node unavailable"}',
            status_code=503,
            media_type="application/json",
        )
    return {"status": "ready", "node_available": True}


@app.post("/api/v1/data")
async def write_data(collection: str = "transactions", records: int = 1):
    """Simulate MongoDB write operation."""
    result = simulate_operation("insert")
    if not result["success"]:
        return Response(
            content=f'{{"error":"{result["error"]}","collection":"{collection}"}}',
            status_code=500,
            media_type="application/json",
        )
    return {"inserted": records, "collection": collection, "acknowledged": True}


@app.get("/api/v1/data/{collection}")
async def read_data(collection: str):
    """Simulate MongoDB read operation."""
    result = simulate_operation("find")
    if not result["success"]:
        return Response(status_code=500,
                        content=f'{{"error":"{result["error"]}"}}',
                        media_type="application/json")
    docs = [{"_id": f"doc-{i}", "data": "sample"} for i in range(random.randint(1, 5))]
    return {"documents": docs, "collection": collection, "count": len(docs)}


# ── INCIDENT SIMULATION ENDPOINTS ────────────────────────────

@app.post("/simulate-node-maintenance")
async def start_maintenance():
    """
    🔴 START INCIDENT: Simulate node going to maintenance.

    This mimics the exact Verizon incident:
      - MongoDB node goes into maintenance
      - All connections fail with ECONNREFUSED
      - Dependent services (payment-api) start returning 500 errors
      - Errors flood into Loki logs
      - Prometheus metrics spike

    curl -X POST http://localhost:8002/simulate-node-maintenance
    """
    _state["node_available"] = False
    _state["error_rate"]     = 1.0    # 100% errors
    _state["latency_ms"]     = 3000   # 3s timeout per operation
    mongodb_connections_active.labels(namespace=NAMESPACE).set(0)

    logger.error(
        "🔴 NODE MAINTENANCE STARTED — "
        "MongoDB host mongo-0.mongo.default.svc.cluster.local is unreachable — "
        "all connection attempts failing — "
        "MongoNetworkError: connect ECONNREFUSED 10.96.x.x:27017"
    )
    logger.error(
        "MongoServerSelectionError: Server selection timed out after 30000ms — "
        "connection to mongo:27017 failed"
    )

    return {
        "status":  "incident started",
        "message": "MongoDB node is now in maintenance — connections will fail",
        "impact":  "All services depending on MongoDB will return 500 errors",
        "fix":     "POST /restore to bring node back online",
    }


@app.post("/restore")
async def restore_node():
    """
    🟢 END INCIDENT: Restore MongoDB node to normal operation.

    curl -X POST http://localhost:8002/restore
    """
    _state["node_available"] = False
    _state["error_rate"]     = 0.02   # back to 2% baseline
    _state["latency_ms"]     = 5
    mongodb_connections_active.labels(namespace=NAMESPACE).set(
        random.randint(5, 20)
    )
    logger.info("🟢 Node back online — MongoDB connections restored")
    return {
        "status":  "restored",
        "message": "MongoDB node is back online — normal operation resumed",
    }


@app.get("/status")
async def get_status():
    """Current simulation state."""
    return {
        "node_available": _state["node_available"],
        "error_rate":     _state["error_rate"],
        "latency_ms":     _state["latency_ms"],
        "timestamp":      datetime.utcnow().isoformat(),
    }


@app.get("/metrics")
async def prometheus_metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002, log_level="info")
