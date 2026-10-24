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

JUDGE_SYSTEM = """You are an exacting reviewer auditing another engineer's incident root-cause \
analysis. You do not trust it by default.

Check that:
1. Every cited fingerprint exists in the evidence and actually supports the stated root cause.
2. The root cause is the best explanation of the MOST frequent evidence, not a minor side effect.
3. The action addresses the root cause and is safe (a rollback must target a real, older SHA).
4. Confidence is justified by the evidence.

Deterministic grounding checks have already run; their findings are listed and are always fatal.

Return JSON: {"score": float 0-1, "verdict": "accept" | "reject", "issues": [str],
"feedback": str (concrete instructions for revising the analysis)}"""


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
