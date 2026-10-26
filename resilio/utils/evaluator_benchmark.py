"""Offline accuracy benchmark for the synthesizer + LLM-as-a-judge.

Generates labelled scenarios (10 outage types x 5 variants by default) and
measures two things:

**Judge quality** - each scenario carries a *candidate* RCA that is either
correct or deliberately wrong. Variants:

====  ==================================================  =========
var   candidate                                            label
====  ==================================================  =========
0, 1  ground-truth RCA                                     valid
2     ground truth, evidence mixed with a 2nd failure mode valid
3     fabricated rollback SHA / fabricated fingerprint     invalid
4     plausible but wrong root cause, real evidence        invalid
====  ==================================================  =========

Reported as precision/recall of "accept" plus the *hallucination escape
rate* (invalid candidates the judge let through).

**Pipeline quality** - the full synthesize -> evaluate -> revise loop runs on
the same evidence and is scored against ground truth: RCA accuracy, action
accuracy, first-pass hallucination rate, and how often the evaluator had to
correct the synthesizer.

**Confidence calibration** - for every accepted hypothesis, both the model's
self-reported confidence and the rule-based score are compared with whether
the root cause was right: Brier score and expected calibration error (10
bins). The scenarios come from the same templates the mock and the default
rules know, so this is a sanity check, not proof; run it with a real model.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from resilio.config import Settings
from resilio.core.database import InMemoryIncidentRepository
from resilio.graph.deps import WorkflowDeps
from resilio.graph.nodes.evaluator_node import check_grounding, evaluate_hypothesis
from resilio.graph.nodes.synthesizer_node import synthesize
from resilio.graph.nodes.vcs_node import correlate, onset_from_groups
from resilio.models import Deployment, RemediationAction
from resilio.providers.base import BaseLLMProvider
from resilio.providers.notifications.mock import MockNotifier
from resilio.providers.registry import build_confidence_scorer
from resilio.providers.remediation.mock import MockRemediationProvider, mock_deployments
from resilio.providers.telemetry.mock import MockTelemetryProvider, generate_traces
from resilio.scenarios import SCENARIO_TYPES, SCENARIOS
from resilio.scoring.confidence import signals_from_evidence
from resilio.utils.log_sanitizer import compact_traces

SERVICES = ("checkout", "catalog", "payments", "search", "auth", "orders", "inventory")
VARIANT_LABELS = {
    0: ("valid", True),
    1: ("valid", True),
    2: ("valid_ambiguous", True),
    3: ("fabricated", False),
    4: ("misattributed_root_cause", False),
}


@dataclass(slots=True)
class BenchmarkScenario:
    id: str
    incident_type: str
    service: str
    variant: int
    label: str
    label_valid: bool
    expected_root_cause: str
    expected_action: str
    trace_groups: list[dict[str, Any]]
    deployments: list[dict[str, Any]]
    suspect_deployment: dict[str, Any] | None
    rollback_target: str | None
    candidate: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkScenario:
        return cls(**data)


def _random_hex(rng: random.Random, length: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(length))


def generate_scenarios(count: int = 50, seed: int = 42) -> list[BenchmarkScenario]:
    """Deterministically generate ``count`` labelled scenarios."""
    rng = random.Random(seed)
    base_time = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    scenarios: list[BenchmarkScenario] = []
    for i in range(count):
        incident_type = SCENARIO_TYPES[i % len(SCENARIO_TYPES)]
        template = SCENARIOS[incident_type]
        variant = (i // len(SCENARIO_TYPES)) % len(VARIANT_LABELS)
        label, valid = VARIANT_LABELS[variant]
        service = rng.choice(SERVICES)
        start = base_time + timedelta(hours=i)

        extra: tuple[tuple[str, str], ...] = ()
        if variant == 2:
            other = SCENARIOS[SCENARIO_TYPES[(i + 3) % len(SCENARIO_TYPES)]]
            extra = other.error_messages[:1]
        traces = generate_traces(
            template,
            service,
            count=rng.randint(80, 320),
            seed=rng.getrandbits(32),
            start=start,
            end=start + timedelta(minutes=10),
            extra_messages=extra,
        )
        groups = [g.as_dict() for g in compact_traces(traces).groups]
        history = mock_deployments(
            service,
            latest_message=template.deploy_message if template.deploy_related else None,
            latest_at=start
            - (timedelta(minutes=4) if template.deploy_related else timedelta(days=2)),
        )
        suspect, target = correlate(history, start)

        signal_types = {error_type for error_type, _ in template.error_messages}
        evidence = [g["fingerprint"] for g in groups if g["error_type"] in signal_types][:5]
        action = template.recommended_action
        candidate: dict[str, Any] = {
            "root_cause": template.root_cause,
            "summary": template.description,
            "confidence": round(rng.uniform(0.8, 0.95), 2),
            "evidence": evidence,
            "action": action.value,
            "target_sha": target if action == RemediationAction.ROLLBACK else None,
            "reasoning": "ground truth",
        }
        if variant == 3:
            if action == RemediationAction.ROLLBACK:
                candidate["target_sha"] = _random_hex(rng, 40)
            else:
                candidate["evidence"] = [*evidence, _random_hex(rng, 12)]
        elif variant == 4:
            wrong = SCENARIOS[SCENARIO_TYPES[(i + 1) % len(SCENARIO_TYPES)]]
            candidate["root_cause"] = wrong.root_cause
            candidate["summary"] = wrong.description
            candidate["action"] = wrong.recommended_action.value
            candidate["target_sha"] = (
                target if wrong.recommended_action == RemediationAction.ROLLBACK else None
            )

        scenarios.append(
            BenchmarkScenario(
                id=f"bench-{i:03d}-{incident_type}",
                incident_type=incident_type,
                service=service,
                variant=variant,
                label=label,
                label_valid=valid,
                expected_root_cause=template.root_cause,
                expected_action=action.value,
                trace_groups=groups,
                deployments=[d.model_dump(mode="json") for d in history],
                suspect_deployment=suspect.model_dump(mode="json") if suspect else None,
                rollback_target=target,
                candidate=candidate,
            )
        )
    return scenarios


def save_scenarios(scenarios: list[BenchmarkScenario], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(s) for s in scenarios], indent=2), encoding="utf-8")


def load_scenarios(path: Path) -> list[BenchmarkScenario]:
    return [BenchmarkScenario.from_dict(item) for item in json.loads(path.read_text("utf-8"))]


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ScenarioResult:
    id: str
    incident_type: str
    label: str
    label_valid: bool
    judge_accepted: bool
    judge_score: float
    pipeline_root_cause: str
    pipeline_action: str
    pipeline_accepted: bool
    pipeline_attempts: int
    first_pass_hallucinated: bool
    rca_correct: bool
    action_correct: bool
    #: Both None when no hypothesis was accepted.
    llm_confidence: float | None = None
    rules_confidence: float | None = None


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def calibration(pairs: Sequence[tuple[float, bool]], bins: int = 10) -> dict[str, float]:
    """Brier score and expected calibration error of ``(confidence, correct)`` pairs."""
    n = len(pairs)
    if not n:
        return {"n": 0, "brier": 0.0, "ece": 0.0, "mean_confidence": 0.0, "accuracy": 0.0}
    brier = sum((c - float(y)) ** 2 for c, y in pairs) / n
    ece = 0.0
    for index in range(bins):
        members = [(c, y) for c, y in pairs if min(int(c * bins), bins - 1) == index]
        if members:
            mean_confidence = sum(c for c, _ in members) / len(members)
            accuracy = sum(y for _, y in members) / len(members)
            ece += abs(accuracy - mean_confidence) * len(members) / n
    return {
        "n": n,
        "brier": round(brier, 4),
        "ece": round(ece, 4),
        "mean_confidence": round(sum(c for c, _ in pairs) / n, 4),
        "accuracy": round(sum(y for _, y in pairs) / n, 4),
    }


def _onset_gap_minutes(scenario: BenchmarkScenario) -> float | None:
    if not scenario.suspect_deployment:
        return None
    onset = onset_from_groups(scenario.trace_groups)
    if onset is None:
        return None
    deployed_at = Deployment.model_validate(scenario.suspect_deployment).deployed_at
    return (onset - deployed_at).total_seconds() / 60


def _unit(value: Any) -> float | None:
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class BenchmarkReport:
    llm: str
    scenarios: int
    duration_seconds: float
    tokens: int
    judge_precision: float
    judge_recall: float
    judge_f1: float
    judge_accuracy: float
    hallucination_escape_rate: float
    misattribution_escape_rate: float
    rca_accuracy: float
    action_accuracy: float
    first_pass_hallucination_rate: float
    final_hallucination_rate: float
    correction_rate: float
    escalation_rate: float
    #: ``{"llm": {...}, "rules": {...}}`` from :func:`calibration`.
    calibration: dict[str, dict[str, float]] = field(default_factory=dict)
    results: list[ScenarioResult] = field(default_factory=list)

    def by_type(self) -> dict[str, dict[str, float]]:
        table: dict[str, dict[str, float]] = {}
        for incident_type in dict.fromkeys(r.incident_type for r in self.results):
            rows = [r for r in self.results if r.incident_type == incident_type]
            table[incident_type] = {
                "n": len(rows),
                "judge_accuracy": _ratio(
                    sum(r.judge_accepted == r.label_valid for r in rows), len(rows)
                ),
                "rca_accuracy": _ratio(sum(r.rca_correct for r in rows), len(rows)),
                "corrections": float(sum(r.pipeline_attempts - 1 for r in rows)),
            }
        return table

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["by_type"] = self.by_type()
        return data


def _benchmark_deps(llm: BaseLLMProvider, settings: Settings) -> WorkflowDeps:
    telemetry = MockTelemetryProvider()
    return WorkflowDeps(
        settings=settings,
        telemetry=telemetry,
        remediation=MockRemediationProvider(telemetry),
        notifier=MockNotifier(),
        llm=llm,
        repository=InMemoryIncidentRepository(),
        confidence_scorer=build_confidence_scorer(settings),
    )


async def run_benchmark(
    scenarios: list[BenchmarkScenario], llm: BaseLLMProvider, settings: Settings
) -> BenchmarkReport:
    """Score the judge and the end-to-end RCA loop on ``scenarios``."""
    deps = _benchmark_deps(llm, settings)
    started = time.perf_counter()
    tokens = 0
    results: list[ScenarioResult] = []

    for scenario in scenarios:
        groups, deployments = scenario.trace_groups, scenario.deployments
        judged, used = await evaluate_hypothesis(deps, scenario.candidate, groups, deployments)
        tokens += used

        attempts, feedback = 0, None
        first_pass_hallucinated = False
        hypothesis: dict[str, Any] = {}
        evaluation: dict[str, Any] = {"accepted": False}
        while attempts <= settings.max_evaluator_retries:
            hypothesis, used = await synthesize(
                deps,
                incident_id=scenario.id,
                event={"service": scenario.service, "title": scenario.incident_type},
                groups=groups,
                deployments=deployments,
                suspect=scenario.suspect_deployment,
                rollback_target=scenario.rollback_target,
                feedback=feedback,
                attempt=attempts,
            )
            tokens += used
            if attempts == 0:
                first_pass_hallucinated = bool(check_grounding(hypothesis, groups, deployments))
            evaluation, used = await evaluate_hypothesis(deps, hypothesis, groups, deployments)
            tokens += used
            attempts += 1
            if evaluation["accepted"]:
                break
            feedback = evaluation["feedback"]

        accepted = bool(evaluation["accepted"])
        llm_confidence = rules_confidence = None
        if accepted:
            llm_confidence = _unit(hypothesis.get("confidence"))
            signals = signals_from_evidence(
                hypothesis=hypothesis,
                evaluation=evaluation,
                trace_groups=groups,
                deploy_onset_gap_minutes=_onset_gap_minutes(scenario),
                revision_count=attempts - 1,
            )
            rules_confidence = deps.confidence_scorer.score(signals).value
        results.append(
            ScenarioResult(
                id=scenario.id,
                incident_type=scenario.incident_type,
                label=scenario.label,
                label_valid=scenario.label_valid,
                judge_accepted=bool(judged["accepted"]),
                judge_score=float(judged["score"]),
                pipeline_root_cause=str(hypothesis.get("root_cause")),
                pipeline_action=str(hypothesis.get("action")),
                pipeline_accepted=accepted,
                pipeline_attempts=attempts,
                first_pass_hallucinated=first_pass_hallucinated,
                rca_correct=accepted
                and hypothesis.get("root_cause") == scenario.expected_root_cause,
                action_correct=accepted and hypothesis.get("action") == scenario.expected_action,
                llm_confidence=llm_confidence,
                rules_confidence=rules_confidence,
            )
        )

    tp = sum(r.judge_accepted and r.label_valid for r in results)
    fp = sum(r.judge_accepted and not r.label_valid for r in results)
    fn = sum(not r.judge_accepted and r.label_valid for r in results)
    tn = sum(not r.judge_accepted and not r.label_valid for r in results)
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    fabricated = [r for r in results if r.label == "fabricated"]
    misattributed = [r for r in results if r.label == "misattributed_root_cause"]
    accepted_runs = [r for r in results if r.pipeline_accepted]
    n = len(results)
    return BenchmarkReport(
        llm=getattr(llm, "model", llm.name),
        scenarios=n,
        duration_seconds=round(time.perf_counter() - started, 3),
        tokens=tokens,
        judge_precision=precision,
        judge_recall=recall,
        judge_f1=round(2 * precision * recall / (precision + recall), 4)
        if precision + recall
        else 0.0,
        judge_accuracy=_ratio(tp + tn, n),
        hallucination_escape_rate=_ratio(
            sum(r.judge_accepted for r in fabricated), len(fabricated)
        ),
        misattribution_escape_rate=_ratio(
            sum(r.judge_accepted for r in misattributed), len(misattributed)
        ),
        rca_accuracy=_ratio(sum(r.rca_correct for r in results), n),
        action_accuracy=_ratio(sum(r.action_correct for r in results), n),
        first_pass_hallucination_rate=_ratio(sum(r.first_pass_hallucinated for r in results), n),
        final_hallucination_rate=_ratio(
            sum(not r.rca_correct for r in accepted_runs), len(accepted_runs)
        ),
        correction_rate=_ratio(sum(r.pipeline_attempts > 1 for r in results), n),
        escalation_rate=_ratio(n - len(accepted_runs), n),
        calibration={
            "llm": calibration(
                [(r.llm_confidence, r.rca_correct) for r in results if r.llm_confidence is not None]
            ),
            "rules": calibration(
                [
                    (r.rules_confidence, r.rca_correct)
                    for r in results
                    if r.rules_confidence is not None
                ]
            ),
        },
        results=results,
    )
