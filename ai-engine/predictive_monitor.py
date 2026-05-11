"""
predictive_monitor.py
────────────────────────────────────────────────────────────────
Runs every N minutes (configured via PREDICT_INTERVAL_SECONDS).
Fetches historical metric data from Prometheus and extrapolates
a trend to predict future breaches BEFORE they happen.

YOUR MONGODB INCIDENT — THIS IS HOW IT WOULD HAVE BEEN CAUGHT:
  1. Node goes into maintenance at 2 AM
  2. MongoDB pod restarts — connection errors start climbing
  3. This script runs at 2:15 AM
  4. Sees: MongoDB connection error rate trending upward
  5. Forecasts: will exceed threshold in ~45 minutes
  6. Fires early warning BEFORE the cascade hits user traffic
  7. On-call engineer gets alert at 2:15 AM, not 3:00 AM when
     users start complaining

HOW TO RUN:
  python predictive_monitor.py                  # run once
  python predictive_monitor.py --loop           # run continuously
  python predictive_monitor.py --loop --demo    # demo mode (fast interval)
────────────────────────────────────────────────────────────────
"""

import os
import sys
import time
import asyncio
import logging
import argparse
from datetime import datetime, timezone

import numpy as np
import requests
from dotenv import load_dotenv

from cost_tracker import predictive_alerts_total

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("predictor")

PROMETHEUS_URL      = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
AIOPS_ENGINE_URL    = os.getenv("AIOPS_ENGINE_URL", "http://localhost:8000")
PREDICT_INTERVAL    = int(os.getenv("PREDICT_INTERVAL_SECONDS", "60"))
FORECAST_HOURS      = 4       # how far ahead to forecast
LOOKBACK_HOURS      = 6       # how much history to use for trend
STEP                = "5m"    # Prometheus query resolution

# ── WHAT WE MONITOR ───────────────────────────────────────────
# Add or remove metrics here to customise what gets predicted.
# Each entry: (metric_name, promql, threshold, description)
MONITORED_METRICS = [
    (
        "cpu_usage_percent",
        'avg(rate(container_cpu_usage_seconds_total'
        '{container!="",namespace!="kube-system"}[5m])) * 100',
        80.0,
        "Average container CPU usage — breach = performance degradation",
    ),
    (
        "memory_usage_percent",
        'avg(container_memory_working_set_bytes'
        '{container!="",namespace!="kube-system"}) / '
        'avg(container_spec_memory_limit_bytes'
        '{container!="",namespace!="kube-system",container_spec_memory_limit_bytes>0}) * 100',
        85.0,
        "Container memory usage vs limit — breach = OOMKill risk",
    ),
    (
        "http_error_rate_percent",
        'rate(http_requests_total{status=~"5.."}[5m]) / '
        'rate(http_requests_total[5m]) * 100',
        10.0,
        "HTTP 5xx error rate — breach = customer-visible errors",
    ),
    (
        "mongodb_connection_errors",
        'rate(mongodb_connection_errors_total[5m])',
        5.0,
        "MongoDB connection error rate — breach = database unavailability",
    ),
    (
        "pod_restart_rate",
        'rate(kube_pod_container_status_restarts_total[15m])',
        0.1,
        "Pod restart rate — breach = crash-looping containers",
    ),
]


# ── PROMETHEUS DATA FETCHER ───────────────────────────────────

def fetch_metric_history(promql: str, hours: int = LOOKBACK_HOURS) -> list[tuple]:
    """
    Fetch time series data from Prometheus.
    Returns list of (timestamp, value) tuples.
    """
    end   = datetime.now(timezone.utc)
    start = end.timestamp() - (hours * 3600)

    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={
                "query": promql,
                "start": start,
                "end":   end.timestamp(),
                "step":  STEP,
            },
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()

        if result.get("status") != "success":
            return []

        # Aggregate multiple series (e.g., multiple containers) by averaging
        all_series = result.get("data", {}).get("result", [])
        if not all_series:
            return []

        # Build a time → [values] mapping to average across series
        time_map: dict = {}
        for series in all_series:
            for ts, val in series.get("values", []):
                ts = float(ts)
                try:
                    v = float(val)
                    if not (v != v):   # NaN check
                        time_map.setdefault(ts, []).append(v)
                except (ValueError, TypeError):
                    pass

        # Average values at each timestamp
        averaged = sorted(
            (ts, sum(vals)/len(vals))
            for ts, vals in time_map.items()
        )
        return averaged

    except Exception as e:
        logger.debug(f"Prometheus fetch failed for query: {promql[:60]}... — {e}")
        return []


# ── TREND FORECASTING ─────────────────────────────────────────

def forecast_linear(
    data_points: list[tuple],
    forecast_hours: int = FORECAST_HOURS,
) -> dict:
    """
    Fit a linear trend to the historical data and extrapolate forward.

    Returns:
        {
          current_value: float,
          forecast_value: float,    # predicted value at forecast_hours
          slope_per_hour: float,    # rate of change per hour
          time_to_breach: float,    # hours until threshold (None if not trending there)
          r_squared: float,         # how well the trend fits (0-1)
        }
    """
    if len(data_points) < 5:
        return {"error": "insufficient data", "current_value": 0}

    timestamps = np.array([ts for ts, _ in data_points])
    values     = np.array([v  for _, v  in data_points])

    # Normalise timestamps to hours from first point
    t_hours = (timestamps - timestamps[0]) / 3600.0

    # Fit linear regression: value = slope * t + intercept
    coeffs   = np.polyfit(t_hours, values, deg=1)
    slope    = coeffs[0]    # change per hour
    intercept = coeffs[1]

    # R-squared — how well linear model fits
    y_pred   = np.polyval(coeffs, t_hours)
    ss_res   = np.sum((values - y_pred) ** 2)
    ss_tot   = np.sum((values - values.mean()) ** 2)
    r_sq     = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

    current_value  = float(values[-1])
    t_current      = float(t_hours[-1])
    forecast_value = slope * (t_current + forecast_hours) + intercept

    return {
        "current_value":  round(current_value, 3),
        "forecast_value": round(forecast_value, 3),
        "slope_per_hour": round(float(slope), 4),
        "r_squared":      round(float(r_sq), 3),
        "data_points":    len(data_points),
    }


def time_to_breach(
    current_value: float,
    slope_per_hour: float,
    threshold: float,
) -> float | None:
    """
    Calculate hours until the trend reaches the threshold.
    Returns None if not trending toward breach.
    """
    if slope_per_hour <= 0:
        return None   # metric is flat or decreasing — no breach
    if current_value >= threshold:
        return 0.0    # already breached
    hours = (threshold - current_value) / slope_per_hour
    if hours < 0 or hours > 24:
        return None   # too far out to be actionable
    return round(hours, 1)


# ── ALERT TRIGGERING ──────────────────────────────────────────

def trigger_predictive_alert(
    metric_name: str,
    description: str,
    current_value: float,
    forecast_value: float,
    threshold: float,
    hours_to_breach: float,
    slope_per_hour: float,
):
    """
    Trigger a predictive alert via the AI engine's simulate endpoint.
    The engine will generate an RCA explaining the predicted trend.
    """
    alert_name = f"PredictedBreachIn{int(hours_to_breach*60)}min_{metric_name}"
    alert_desc = (
        f"Predictive alert: {metric_name} currently at {current_value:.1f} "
        f"(threshold: {threshold}). "
        f"At current trend (+{slope_per_hour:.3f}/hr), "
        f"breach predicted in {hours_to_breach:.1f} hours. "
        f"Forecast value in {FORECAST_HOURS}h: {forecast_value:.1f}. "
        f"Action required NOW to prevent breach. — {description}"
    )

    try:
        resp = requests.post(
            f"{AIOPS_ENGINE_URL}/incidents/simulate",
            params={
                "alert_name": alert_name,
                "service":    metric_name,
                "namespace":  "predicted",
                "severity":   "warning",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            predictive_alerts_total.labels(
                service=metric_name, metric=metric_name
            ).inc()
            logger.info(f"  ↗ Predictive alert triggered: {alert_name}")
        else:
            logger.warning(f"  Failed to trigger alert: {resp.status_code}")
    except Exception as e:
        logger.warning(f"  Could not reach AI engine: {e}")
        # Print alert to console as fallback
        print(f"\n⚠ PREDICTIVE ALERT: {alert_name}\n{alert_desc}\n")


# ── MAIN MONITORING LOOP ──────────────────────────────────────

def run_prediction_cycle():
    """
    Run one complete prediction cycle across all monitored metrics.
    Called every PREDICT_INTERVAL seconds.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logger.info(f"━━━ Predictive Monitor Cycle — {now} ━━━")

    alerts_fired = 0

    for metric_name, promql, threshold, description in MONITORED_METRICS:
        logger.info(f"  Checking: {metric_name}")

        # Fetch historical data
        data_points = fetch_metric_history(promql)
        if not data_points:
            logger.debug(f"    No data for {metric_name} — skipping")
            continue

        # Forecast
        forecast = forecast_linear(data_points)
        if "error" in forecast:
            logger.debug(f"    {forecast['error']}")
            continue

        current  = forecast["current_value"]
        predicted = forecast["forecast_value"]
        slope    = forecast["slope_per_hour"]
        r_sq     = forecast["r_squared"]

        logger.info(
            f"    Current={current:.2f} | "
            f"Forecast({FORECAST_HOURS}h)={predicted:.2f} | "
            f"Threshold={threshold} | "
            f"Slope={slope:+.4f}/hr | R²={r_sq:.2f}"
        )

        # Already breached?
        if current >= threshold:
            logger.warning(f"    ⚠ ALREADY BREACHED: {metric_name}={current:.2f} > {threshold}")
            continue

        # Trending toward breach?
        if r_sq < 0.3:
            logger.debug(f"    Low R²={r_sq:.2f} — trend not reliable enough to alert")
            continue

        breach_hours = time_to_breach(current, slope, threshold)
        if breach_hours is not None and breach_hours <= FORECAST_HOURS:
            logger.warning(
                f"    ⚠ PREDICTED BREACH in {breach_hours:.1f}h — "
                f"triggering predictive alert"
            )
            trigger_predictive_alert(
                metric_name=metric_name,
                description=description,
                current_value=current,
                forecast_value=predicted,
                threshold=threshold,
                hours_to_breach=breach_hours,
                slope_per_hour=slope,
            )
            alerts_fired += 1

    logger.info(f"━━━ Cycle complete — {alerts_fired} predictive alerts fired ━━━\n")
    return alerts_fired


# ── ENTRY POINT ───────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIOps Predictive Monitor")
    parser.add_argument("--loop",  action="store_true", help="Run continuously")
    parser.add_argument("--demo",  action="store_true", help="Use faster interval")
    args = parser.parse_args()

    interval = 30 if args.demo else PREDICT_INTERVAL
    logger.info(f"Predictive Monitor starting — interval={interval}s | "
                f"Prometheus={PROMETHEUS_URL}")

    if args.loop:
        logger.info("Running in loop mode. Press Ctrl+C to stop.")
        while True:
            try:
                run_prediction_cycle()
            except KeyboardInterrupt:
                logger.info("Shutting down.")
                sys.exit(0)
            except Exception as e:
                logger.error(f"Cycle failed: {e}")
            time.sleep(interval)
    else:
        logger.info("Running single cycle.")
        run_prediction_cycle()
