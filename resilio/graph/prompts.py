"""Prompt templates for the synthesizer and the LLM-as-a-judge.

Prompts receive only redacted, fingerprinted evidence. Every claim the model
makes must cite a fingerprint or a deployment SHA that appears in the prompt,
which is what lets the evaluator detect hallucinations mechanically.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from resilio.providers.llm.rules import ROOT_CAUSES

ACTIONS = ("rollback", "restart", "scale_up", "none")

SYNTHESIZER_SYSTEM = f"""You are ResilioAI, a senior Site Reliability Engineer performing \
root-cause analysis on a production incident.

Rules:
- Use ONLY the evidence provided. Cite evidence by its fingerprint (the 12-char id in brackets).
- `root_cause` must be one of: {", ".join(ROOT_CAUSES)}.
- `action` must be one of: {", ".join(ACTIONS)}. Choose "none" when no safe automated action exists
  (e.g. third-party outage, expired certificate) - a human will be paged.
- For "rollback", `target_sha` MUST be copied verbatim from the deployment history and must be an
  older deployment than the current one. Never invent a SHA.
- `confidence` is a float in [0, 1]. Be calibrated, not optimistic.

Return JSON: {{"root_cause": str, "summary": str, "confidence": float, "evidence": [fingerprint],
"action": str, "target_sha": str | null, "reasoning": str}}"""

JUDGE_SYSTEM = """You are a senior SRE reviewing another engineer's incident root-cause analysis \
before it is acted on. Your job is to catch real defects, not to demand proof: incident RCA is \
always probabilistic, and a plausible, evidence-backed explanation is exactly what is needed.

REJECT only for a concrete defect you can name:
1. A cited fingerprint does not support the stated root cause (e.g. a disk error cited for a
   memory leak).
2. The most frequent evidence clearly points to a DIFFERENT root cause than the one stated.
3. The action does not address the root cause, or is unsafe (e.g. scaling up for a memory leak).
4. Any deterministic grounding finding is listed below - these are always fatal.

Otherwise ACCEPT. Do NOT reject because causality is not "proven", because more data could be
gathered, or because alternative explanations are conceivable but unsupported by the evidence.

Score: 0.9-1.0 sound; 0.7-0.89 sound with minor gaps (still accept); below 0.7 only when one of
the defects above applies (reject).

Work in this order. First, ignoring the analysis, write your own diagnosis of the MOST frequent
evidence lines in `evidence_diagnosis`. Then compare it with the analysis's root cause: if they
describe different failure modes (e.g. "database deadlock" vs "certificate expiry"), that is
defect 2 and you must reject.

Return JSON: {"evidence_diagnosis": str, "score": float 0-1, "verdict": "accept" | "reject",
"issues": [str], "feedback": str (concrete revision instructions; empty if accepted)}"""


def _render_groups(groups: Sequence[Mapping[str, Any]]) -> str:
    return (
        "\n".join(
            f"[{g['fingerprint']}] x{g['count']} {g['error_type']}: {g['sample']} "
            f"(first={g.get('first_seen', '')}, last={g.get('last_seen', '')})"
            for g in groups
        )
        or "(no evidence)"
    )


def _render_deployments(deployments: Sequence[Mapping[str, Any]]) -> str:
    return (
        "\n".join(
            f"{'CURRENT ' if i == 0 else ''}{d['sha']} at {d['deployed_at']} "
            f"by {d.get('author', '?')}: {d.get('message', '')}"
            for i, d in enumerate(deployments)
        )
        or "(no deployment history)"
    )


def render_synthesizer_prompt(
    *,
    event: Mapping[str, Any],
    groups: Sequence[Mapping[str, Any]],
    deployments: Sequence[Mapping[str, Any]],
    suspect: Mapping[str, Any] | None,
    rollback_target: str | None,
    similar: Sequence[Mapping[str, Any]],
    feedback: str | None,
) -> str:
    parts = [
        f"## Alert\nservice={event['service']} env={event.get('environment')} "
        f"severity={event.get('severity')}\ntitle: {event['title']}",
        f"## Evidence (fingerprinted, most frequent first)\n{_render_groups(groups)}",
        f"## Deployment history (newest first)\n{_render_deployments(deployments)}",
    ]
    if suspect:
        parts.append(
            f"## Correlation\nError onset follows deploy {suspect['sha']} "
            f"('{suspect.get('message', '')}'). "
            f"Previous good version: {rollback_target or 'unknown'}."
        )
    if similar:
        parts.append(
            "## Similar past incidents\n"
            + "\n".join(
                f"- {s.get('incident_id')} (similarity {s.get('similarity')}): "
                f"{s.get('root_cause')} fixed by {s.get('action')}"
                for s in similar
            )
        )
    if feedback:
        parts.append(
            f"## Reviewer feedback on your previous answer (address all of it)\n{feedback}"
        )
    return "\n\n".join(parts)


def render_judge_prompt(
    *,
    hypothesis: Mapping[str, Any],
    groups: Sequence[Mapping[str, Any]],
    deployments: Sequence[Mapping[str, Any]],
    grounding_issues: Sequence[str],
) -> str:
    return "\n\n".join(
        [
            f"## Evidence\n{_render_groups(groups)}",
            f"## Deployment history (newest first)\n{_render_deployments(deployments)}",
            f"## Analysis under review\n{json.dumps(dict(hypothesis), indent=2)}",
            "## Deterministic grounding findings\n"
            + ("\n".join(f"- {i}" for i in grounding_issues) or "- none"),
        ]
    )
