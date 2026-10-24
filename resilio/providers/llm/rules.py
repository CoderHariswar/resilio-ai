"""Evidence -> root-cause rules.

Used by :class:`MockLLMProvider` as its "reasoning", and by the benchmark as a
transparent baseline every real model should beat. Rules match against the
*redacted, fingerprinted* evidence groups, never against scenario labels.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from resilio.models import RemediationAction

A = RemediationAction


@dataclass(frozen=True, slots=True)
class RCARule:
    root_cause: str
    pattern: re.Pattern[str]
    action: RemediationAction
    allowed_actions: frozenset[RemediationAction]
    deploy_related: bool
    explanation: str


def _rule(
    root_cause: str,
    pattern: str,
    action: RemediationAction,
    allowed: Iterable[RemediationAction],
    deploy_related: bool,
    explanation: str,
) -> RCARule:
    return RCARule(
        root_cause,
        re.compile(pattern, re.IGNORECASE),
        action,
        frozenset(allowed) | {action, A.NONE},
        deploy_related,
        explanation,
    )


RULES: tuple[RCARule, ...] = (
    _rule(
        "memory_leak",
        r"out ?of ?memory|oomkill|heap space|gc overhead|memory limit",
        A.ROLLBACK,
        [A.RESTART],
        True,
        "Memory grows until the runtime is killed; the most recent deploy is the prime suspect.",
    ),
    _rule(
        "code_regression",
        r"typeerror|nullpointer|cannot read propert|attributeerror|nil pointer|\bhttp ?500\b",
        A.ROLLBACK,
        [],
        True,
        "New exceptions appeared right after a deploy - a code regression.",
    ),
    _rule(
        "connection_pool_exhaustion",
        r"connection pool|remaining connection slots|too many connections|acquire connection",
        A.RESTART,
        [A.SCALE_UP],
        False,
        "Connections are leaked or held too long until the pool is exhausted.",
    ),
    _rule(
        "dependency_failure",
        r"upstream \S+ timed out|circuit breaker|gateway timeout|\bhttp ?504\b",
        A.NONE,
        [],
        False,
        "An upstream dependency is failing; local remediation will not help.",
    ),
    _rule(
        "capacity_saturation",
        r"queue depth|cpu throttl|overloaded|shedding load|\bhttp ?503\b",
        A.SCALE_UP,
        [A.RESTART],
        False,
        "Demand exceeds provisioned capacity.",
    ),
    _rule(
        "certificate_expiry",
        r"x509|certificate (has )?expired|certificate expired|handshake failed",
        A.NONE,
        [],
        False,
        "A TLS certificate expired and must be rotated by a human.",
    ),
    _rule(
        "configuration_error",
        r"keyerror|missing required environment|invalid configuration|crashloopbackoff",
        A.ROLLBACK,
        [],
        True,
        "A configuration change removed or broke required settings.",
    ),
    _rule(
        "disk_exhaustion",
        r"no space left|enospc|errno 28|ephemeral-storage",
        A.RESTART,
        [A.SCALE_UP],
        False,
        "The writable volume is full.",
    ),
    _rule(
        "rate_limited",
        r"\bhttp ?429\b|\b429 too many|too many requests|rate limit",
        A.NONE,
        [],
        False,
        "A third-party quota is exhausted.",
    ),
    _rule(
        "database_deadlock",
        r"deadlock|lock wait timeout|serialization failure",
        A.ROLLBACK,
        [],
        True,
        "Conflicting lock ordering introduced by a recent change.",
    ),
)

RULES_BY_CAUSE: dict[str, RCARule] = {r.root_cause: r for r in RULES}
ROOT_CAUSES: tuple[str, ...] = (*RULES_BY_CAUSE, "unknown")


@dataclass(frozen=True, slots=True)
class RuleMatch:
    rule: RCARule
    score: int
    fingerprints: tuple[str, ...]


def _group_text(group: Mapping[str, Any]) -> str:
    return f"{group.get('error_type', '')} {group.get('sample', '')} {group.get('template', '')}"


def score_groups(groups: Iterable[Mapping[str, Any]]) -> list[RuleMatch]:
    """Score every rule by how many traces its pattern explains; best first."""
    groups = list(groups)
    matches: list[RuleMatch] = []
    for rule in RULES:
        hits = [g for g in groups if rule.pattern.search(_group_text(g))]
        if hits:
            matches.append(
                RuleMatch(
                    rule=rule,
                    score=sum(int(g.get("count", 1)) for g in hits),
                    fingerprints=tuple(str(g["fingerprint"]) for g in hits),
                )
            )
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches
