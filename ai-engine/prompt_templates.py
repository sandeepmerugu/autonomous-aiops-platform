"""
prompt_templates.py
────────────────────────────────────────────────────────────────
Versioned prompt library for the AIOps engine.

WHY VERSION PROMPTS?
  In production AI systems, prompt changes can silently change
  behaviour. Versioning lets you:
  - Roll back a bad prompt the same way you roll back bad code
  - Track which prompt generated which RCA
  - A/B test prompts and compare output quality
  - Meet audit requirements (financial services, healthcare)

HOW TO ADD A NEW PROMPT VERSION:
  1. Add a new entry to PROMPT_REGISTRY
  2. Bump the version string
  3. Keep the old version — never delete (audit trail)
────────────────────────────────────────────────────────────────
"""

from dataclasses import dataclass
from datetime import date


@dataclass
class PromptTemplate:
    version: str
    use_case: str
    author: str
    created_date: str
    system_prompt: str
    user_prompt_template: str   # uses {placeholders}
    max_tokens: int
    temperature: float


# ── PROMPT REGISTRY ──────────────────────────────────────────
PROMPT_REGISTRY: dict[str, PromptTemplate] = {

    # ── RCA Generation v1.0 ──────────────────────────────────
    "rca_v1.0": PromptTemplate(
        version="1.0",
        use_case="incident_rca",
        author="sandeep-merugu",
        created_date="2025-01-01",
        system_prompt="""You are an expert Site Reliability Engineer (SRE) with deep \
knowledge of Kubernetes, Prometheus, and distributed systems. Your job is to analyze \
production incidents and provide concise, actionable root cause analysis.

RULES YOU MUST FOLLOW:
1. Only analyse the metric and log data provided — never guess beyond the data.
2. Root cause must be 1-3 sentences maximum — be precise, not verbose.
3. Remediation steps must be numbered, specific, and immediately actionable.
4. If data is insufficient, say so clearly rather than speculating.
5. Never include PII, account numbers, or customer data in your response.
6. Severity scale: 1 (low/informational) to 5 (critical/production-down).

RESPONSE FORMAT — always return valid JSON matching this exact schema:
{
  "root_cause": "string — 1-3 sentences",
  "severity": number (1-5),
  "confidence": "HIGH | MEDIUM | LOW",
  "remediation_steps": ["step 1", "step 2", "step 3"],
  "prevention": "string — one sentence on how to prevent recurrence",
  "related_runbooks": ["runbook name or URL if known"],
  "escalate": boolean
}""",
        user_prompt_template="""INCIDENT DETAILS
================
Alert Name:    {alert_name}
Service:       {service}
Namespace:     {namespace}
Severity:      {alert_severity}
Fired At:      {fired_at}
Description:   {alert_description}

PROMETHEUS METRICS (last 15 minutes)
=====================================
{prometheus_metrics}

LOKI ERROR LOGS (last 15 minutes)
===================================
{loki_logs}

KUBERNETES STATE
================
{k8s_state}

Based on the above data, provide your root cause analysis in the JSON format \
specified in your system prompt.""",
        max_tokens=1000,
        temperature=0.1,   # low temperature = consistent, factual responses
    ),

    # ── Predictive Alert v1.0 ────────────────────────────────
    "predictive_v1.0": PromptTemplate(
        version="1.0",
        use_case="predictive_alert",
        author="sandeep-merugu",
        created_date="2025-01-01",
        system_prompt="""You are an SRE capacity planning expert. You analyse metric \
trends and warn about problems before they happen. Be concise and specific.""",
        user_prompt_template="""SERVICE: {service}
METRIC:  {metric_name}
CURRENT VALUE: {current_value}
PREDICTED VALUE IN {forecast_hours} HOURS: {predicted_value}
THRESHOLD: {threshold}
TREND DATA (hourly averages): {trend_data}

This metric is predicted to breach the threshold in {time_to_breach}.
In 2-3 sentences, explain: what is likely causing this trend, and what \
action should the on-call engineer take RIGHT NOW to prevent the breach.""",
        max_tokens=300,
        temperature=0.2,
    ),

    # ── Alert Correlation v1.0 ───────────────────────────────
    "correlation_v1.0": PromptTemplate(
        version="1.0",
        use_case="alert_correlation",
        author="sandeep-merugu",
        created_date="2025-01-01",
        system_prompt="""You are an expert at identifying alert storms — \
when multiple alerts fire but all have the same root cause. \
You reduce noise by grouping related alerts.""",
        user_prompt_template="""The following {alert_count} alerts fired within \
{time_window} of each other:

{alert_list}

Are these alerts likely related to the same root cause? \
Return JSON: {{"related": boolean, "common_cause": "string or null", \
"primary_alert": "alert name to investigate first"}}""",
        max_tokens=300,
        temperature=0.1,
    ),
}


def get_prompt(use_case: str, version: str = "latest") -> PromptTemplate:
    """
    Retrieve a prompt by use case. Defaults to the latest version.

    Usage:
        template = get_prompt("incident_rca")
        template = get_prompt("incident_rca", version="1.0")
    """
    if version == "latest":
        # Find highest version for this use_case
        matching = {
            k: v for k, v in PROMPT_REGISTRY.items()
            if v.use_case == use_case
        }
        if not matching:
            raise ValueError(f"No prompt found for use_case='{use_case}'")
        latest_key = sorted(matching.keys())[-1]
        return PROMPT_REGISTRY[latest_key]

    lookup_key = f"{use_case}_v{version}"
    if lookup_key not in PROMPT_REGISTRY:
        raise ValueError(f"Prompt not found: {lookup_key}")
    return PROMPT_REGISTRY[lookup_key]
