"""
cost_tracker.py
────────────────────────────────────────────────────────────────
Tracks every Claude API call and pushes usage metrics to
Prometheus so you can see your AI spend in Grafana dashboards.

WHY THIS MATTERS:
  In financial services (LPL Financial use case), AI cost is a
  compliance requirement — you must prove you are not burning
  budget on untracked LLM calls. This module makes every single
  Claude API call visible, auditable, and alertable.

METRICS EXPOSED (scraped by Prometheus at /metrics):
  claude_api_calls_total          — counter per use_case, model, status
  claude_api_input_tokens_total   — counter per use_case
  claude_api_output_tokens_total  — counter per use_case
  claude_api_cost_usd_total       — counter (running total cost in USD)
  claude_api_duration_seconds     — histogram (how long each call took)
────────────────────────────────────────────────────────────────
"""

import time
import logging
from prometheus_client import Counter, Histogram, Gauge

logger = logging.getLogger(__name__)

# ── CLAUDE PRICING (as of 2025) ──────────────────────────────
# Source: https://www.anthropic.com/pricing
PRICING = {
    "claude-sonnet-4-20250514": {
        "input_per_million":  3.00,   # USD per 1M input tokens
        "output_per_million": 15.00,  # USD per 1M output tokens
    },
    "claude-haiku-4-5-20251001": {
        "input_per_million":  0.80,
        "output_per_million": 4.00,
    },
}
DEFAULT_MODEL = "claude-sonnet-4-20250514"

# ── PROMETHEUS METRICS ────────────────────────────────────────
claude_calls_total = Counter(
    "claude_api_calls_total",
    "Total number of Claude API calls made",
    ["use_case", "model", "status"],          # labels
)

claude_input_tokens_total = Counter(
    "claude_api_input_tokens_total",
    "Total input tokens sent to Claude API",
    ["use_case", "model"],
)

claude_output_tokens_total = Counter(
    "claude_api_output_tokens_total",
    "Total output tokens received from Claude API",
    ["use_case", "model"],
)

claude_cost_usd_total = Counter(
    "claude_api_cost_usd_total",
    "Total estimated cost of Claude API calls in USD",
    ["use_case"],
)

claude_duration_seconds = Histogram(
    "claude_api_duration_seconds",
    "Time taken for Claude API calls in seconds",
    ["use_case"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0],
)

incidents_processed_total = Counter(
    "aiops_incidents_processed_total",
    "Total incidents processed by the AI engine",
    ["severity", "status"],    # status: resolved | escalated | failed
)

rca_generation_seconds = Histogram(
    "aiops_rca_generation_seconds",
    "Time taken to generate a complete RCA (metrics fetch + LLM call)",
    buckets=[5.0, 10.0, 30.0, 60.0, 90.0, 120.0],
)

predictive_alerts_total = Counter(
    "aiops_predictive_alerts_total",
    "Total predictive alerts fired before breach happened",
    ["service", "metric"],
)

alerts_noise_reduced_total = Counter(
    "aiops_alerts_correlated_total",
    "Alerts grouped by AI correlation (noise reduction)",
)


# ── CORE TRACKING FUNCTION ────────────────────────────────────

def calculate_cost(
    input_tokens: int,
    output_tokens: int,
    model: str = DEFAULT_MODEL
) -> float:
    """Calculate the USD cost of a Claude API call."""
    pricing = PRICING.get(model, PRICING[DEFAULT_MODEL])
    input_cost  = (input_tokens  / 1_000_000) * pricing["input_per_million"]
    output_cost = (output_tokens / 1_000_000) * pricing["output_per_million"]
    return round(input_cost + output_cost, 6)


def track_claude_call(
    use_case: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_seconds: float,
    success: bool = True,
) -> dict:
    """
    Record a Claude API call in Prometheus metrics.

    Call this AFTER every Claude API call completes.

    Returns a dict with the cost breakdown for logging/storage.
    """
    status = "success" if success else "error"
    cost   = calculate_cost(input_tokens, output_tokens, model)

    # Increment Prometheus counters
    claude_calls_total.labels(
        use_case=use_case, model=model, status=status
    ).inc()

    claude_input_tokens_total.labels(
        use_case=use_case, model=model
    ).inc(input_tokens)

    claude_output_tokens_total.labels(
        use_case=use_case, model=model
    ).inc(output_tokens)

    claude_cost_usd_total.labels(use_case=use_case).inc(cost)

    claude_duration_seconds.labels(use_case=use_case).observe(duration_seconds)

    usage = {
        "use_case":         use_case,
        "model":            model,
        "input_tokens":     input_tokens,
        "output_tokens":    output_tokens,
        "total_tokens":     input_tokens + output_tokens,
        "cost_usd":         cost,
        "duration_seconds": round(duration_seconds, 2),
        "status":           status,
    }

    logger.info(
        f"[COST] use_case={use_case} | tokens={usage['total_tokens']} | "
        f"cost=${cost:.4f} | duration={duration_seconds:.1f}s"
    )
    return usage


class TimedClaudeCall:
    """
    Context manager that automatically tracks duration of a Claude call.

    Usage:
        with TimedClaudeCall("incident_rca") as timer:
            response = client.messages.create(...)
            timer.record(response.usage.input_tokens,
                         response.usage.output_tokens,
                         model)
    """
    def __init__(self, use_case: str):
        self.use_case   = use_case
        self.start_time = None
        self.usage_info = None

    def __enter__(self):
        self.start_time = time.time()
        return self

    def record(self, input_tokens: int, output_tokens: int,
               model: str = DEFAULT_MODEL, success: bool = True):
        duration = time.time() - self.start_time
        self.usage_info = track_claude_call(
            use_case=self.use_case,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_seconds=duration,
            success=success,
        )

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type and self.usage_info is None:
            # Call failed — record as error with 0 tokens
            duration = time.time() - self.start_time
            self.usage_info = track_claude_call(
                use_case=self.use_case,
                model=DEFAULT_MODEL,
                input_tokens=0,
                output_tokens=0,
                duration_seconds=duration,
                success=False,
            )
        return False  # don't suppress exceptions
