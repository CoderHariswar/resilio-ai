"""Rule-based confidence scoring.

An LLM's self-reported confidence is a guess: uncalibrated, unstable between
runs, and impossible to explain. The confidence the policy engine acts on is
instead computed here from signals the workflow has already verified, with
weights and rules read from ``confidence.yaml`` (see ``confidence.example.yaml``)::

    value = sum(raw_score(signal) * weight(signal))   # weights sum to 1
    value = rule_n(... rule_1(value))                  # cap / floor / multiply

A signal that does not apply (no judge verdict, or an action outside the
signal's ``actions``) hands its weight to the others in proportion, so the
score always spans the full [0, 1] range. Every term is recorded in
:class:`ConfidenceResult`.

Scoring is pure: no I/O, and the same signals always give the same result.
Loading the config is the only I/O and happens once, at startup.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from resilio.models import RemediationAction

DEFAULT_CONFIG_FILE = "confidence.yaml"
WEIGHT_TOLERANCE = 1e-6
SIGNAL_NAMES = ("deploy_timing", "dominant_error", "judge_score", "first_try", "past_match")
_LABELS = {
    "deploy_timing": "deploy timing",
    "dominant_error": "dominant error",
    "judge_score": "judge",
    "first_try": "first try",
    "past_match": "past match",
}
_ACTIONS = tuple(a.value for a in RemediationAction)


class ConfidenceConfigError(ValueError):
    """The confidence config file is unreadable or invalid."""


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


# --------------------------------------------------------------------------- #
# Inputs and outputs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ConfidenceSignals:
    """Verified facts about one accepted hypothesis."""

    action: str
    #: Minutes from the suspect deploy to the error onset; None without a suspect.
    deploy_onset_gap_minutes: float | None = None
    #: Share (0-1) of all error occurrences that fall in the cited fingerprint groups.
    cited_error_share: float = 0.0
    #: The LLM judge's score; None when the judge was unavailable.
    judge_score: float | None = None
    judge_available: bool = True
    #: Rejected attempts before this hypothesis was accepted.
    revision_count: int = 0
    #: Similarity of the closest past incident with the same root cause; None if none.
    best_memory_similarity: float | None = None


def signals_from_evidence(
    *,
    hypothesis: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    trace_groups: Sequence[Mapping[str, Any]],
    total_traces: int | None = None,
    deploy_onset_gap_minutes: float | None = None,
    similar_incidents: Sequence[Mapping[str, Any]] = (),
    revision_count: int = 0,
) -> ConfidenceSignals:
    """Build :class:`ConfidenceSignals` from JSON-shaped graph state."""
    cited = {str(e) for e in hypothesis.get("evidence", [])}
    total = total_traces or sum(int(g.get("count", 0)) for g in trace_groups)
    covered = sum(
        int(g.get("count", 0)) for g in trace_groups if str(g.get("fingerprint")) in cited
    )
    judge_available = bool(evaluation.get("judge_available", True))
    cause = hypothesis.get("root_cause")
    similarities = [
        float(s.get("similarity") or 0.0) for s in similar_incidents if s.get("root_cause") == cause
    ]
    return ConfidenceSignals(
        action=str(hypothesis.get("action", "none")),
        deploy_onset_gap_minutes=deploy_onset_gap_minutes,
        cited_error_share=covered / total if total > 0 else 0.0,
        judge_score=float(evaluation.get("score", 0.0)) if judge_available else None,
        judge_available=judge_available,
        revision_count=revision_count,
        best_memory_similarity=max(similarities) if similarities else None,
    )


@dataclass(frozen=True, slots=True)
class SignalScore:
    signal: str
    #: None when the signal did not apply and its weight went to the others.
    raw_score: float | None
    #: Effective weight after redistribution.
    weight: float
    points: float
    note: str = ""


@dataclass(frozen=True, slots=True)
class AppliedRule:
    when: str
    effect: str
    amount: float
    before: float
    after: float


@dataclass(frozen=True, slots=True)
class ConfidenceResult:
    value: float
    breakdown: tuple[SignalScore, ...]
    rules: tuple[AppliedRule, ...] = ()

    def summary(self) -> str:
        """``0.82 (deploy timing +0.30, judge +0.25, ...)`` for timelines and messages."""
        ranked = sorted(self.breakdown, key=lambda s: -s.points)
        terms = [f"{_LABELS.get(s.signal, s.signal)} {s.points:+.2f}" for s in ranked if s.points]
        terms += [f"{r.effect} {r.amount:g} ({r.when})" for r in self.rules]
        return f"{self.value:.2f} ({', '.join(terms) or 'no signals'})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "summary": self.summary(),
            "breakdown": [asdict(s) for s in self.breakdown],
            "rules": [asdict(r) for r in self.rules],
        }


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
Unit = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegative = Annotated[float, Field(ge=0.0)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SignalConfig(_Strict):
    weight: Unit
    #: Only score this signal for these actions; for others its weight is redistributed.
    actions: list[str] | None = None

    @field_validator("actions")
    @classmethod
    def _known_actions(cls, value: list[str] | None) -> list[str] | None:
        unknown = sorted(set(value or ()) - set(_ACTIONS))
        if unknown:
            raise ValueError(f"unknown action(s) {', '.join(unknown)}; use {', '.join(_ACTIONS)}")
        return value


class DeployTimingConfig(SignalConfig):
    weight: Unit = 0.30
    actions: list[str] | None = Field(default_factory=lambda: ["rollback"])
    strong_window_min: NonNegative = 30
    weak_window_hours: NonNegative = 6

    @model_validator(mode="after")
    def _windows_ordered(self) -> Self:
        if self.strong_window_min > self.weak_window_hours * 60:
            raise ValueError("strong_window_min must not exceed weak_window_hours * 60")
        return self


class DominantErrorConfig(SignalConfig):
    weight: Unit = 0.20


class JudgeScoreConfig(SignalConfig):
    weight: Unit = 0.30


class FirstTryConfig(SignalConfig):
    weight: Unit = 0.10
    #: Score for 0, 1, 2, ... revisions; the last entry applies to any higher count.
    scores: list[Unit] = Field(default_factory=lambda: [1.0, 0.5, 0.25], min_length=1)


class PastMatchConfig(SignalConfig):
    weight: Unit = 0.10
    min_similarity: Unit = 0.5


class SignalsConfig(_Strict):
    deploy_timing: DeployTimingConfig = Field(default_factory=DeployTimingConfig)
    dominant_error: DominantErrorConfig = Field(default_factory=DominantErrorConfig)
    judge_score: JudgeScoreConfig = Field(default_factory=JudgeScoreConfig)
    first_try: FirstTryConfig = Field(default_factory=FirstTryConfig)
    past_match: PastMatchConfig = Field(default_factory=PastMatchConfig)

    def weights(self) -> dict[str, float]:
        return {name: self.get(name).weight for name in SIGNAL_NAMES}

    def get(self, name: str) -> SignalConfig:
        config: SignalConfig = getattr(self, name)
        return config

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> Self:
        weights = self.weights()
        total = sum(weights.values())
        if abs(total - 1.0) > WEIGHT_TOLERANCE:
            detail = ", ".join(f"{name}={weight:g}" for name, weight in weights.items())
            raise ValueError(f"weights must sum to 1.0, got {total:g} ({detail})")
        return self


_FLAG_CONDITIONS = ("rollback_without_suspect_deploy", "judge_unavailable")
_SUPPORTED_CONDITIONS = (
    "rollback_without_suspect_deploy, judge_unavailable, "
    f"action_is:<{'|'.join(_ACTIONS)}>, revisions_at_least:<n>"
)


def _parse_condition(when: str) -> tuple[str, str]:
    name, sep, arg = when.partition(":")
    if name in _FLAG_CONDITIONS and not sep:
        return name, ""
    if name == "action_is" and arg in _ACTIONS:
        return name, arg
    if name == "revisions_at_least" and arg.isdigit():
        return name, arg
    raise ValueError(f"unknown condition '{when}'. Supported: {_SUPPORTED_CONDITIONS}")


class RuleConfig(_Strict):
    """``when`` a named condition holds, apply exactly one effect."""

    when: str
    cap: Unit | None = None
    floor: Unit | None = None
    multiply: NonNegative | None = None

    @field_validator("when")
    @classmethod
    def _known_condition(cls, value: str) -> str:
        _parse_condition(value)
        return value

    @model_validator(mode="after")
    def _one_effect(self) -> Self:
        effects = [e for e in ("cap", "floor", "multiply") if getattr(self, e) is not None]
        if len(effects) != 1:
            raise ValueError(
                f"rule '{self.when}' needs exactly one of cap, floor, multiply "
                f"(got {', '.join(effects) or 'none'})"
            )
        return self

    @property
    def effect(self) -> tuple[str, float]:
        for name in ("cap", "floor", "multiply"):
            amount = getattr(self, name)
            if amount is not None:
                return name, float(amount)
        raise AssertionError("validated rule has no effect")  # pragma: no cover

    def matches(self, signals: ConfidenceSignals) -> bool:
        name, arg = _parse_condition(self.when)
        if name == "rollback_without_suspect_deploy":
            return signals.action == "rollback" and signals.deploy_onset_gap_minutes is None
        if name == "judge_unavailable":
            return not signals.judge_available
        if name == "action_is":
            return signals.action == arg
        return signals.revision_count >= int(arg)

    def apply(self, value: float) -> float:
        effect, amount = self.effect
        if effect == "cap":
            return min(value, amount)
        if effect == "floor":
            return max(value, amount)
        return _clamp(value * amount)


def _default_rules() -> list[RuleConfig]:
    return [
        RuleConfig(when="rollback_without_suspect_deploy", cap=0.4),
        RuleConfig(when="judge_unavailable", multiply=0.8),
    ]


class ConfidenceSection(_Strict):
    scorer: str = "weighted_rules"
    signals: SignalsConfig = Field(default_factory=SignalsConfig)
    rules: list[RuleConfig] = Field(default_factory=_default_rules)


class ConfidenceConfig(_Strict):
    confidence: ConfidenceSection = Field(default_factory=ConfidenceSection)


def _describe(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors()
    )


def load_confidence_config(path: str | Path | None = None) -> ConfidenceConfig:
    """Load ``path``, or ``./confidence.yaml`` if present, or the built-in defaults.

    Raises:
        ConfidenceConfigError: naming the offending field, if the file is invalid
            or an explicitly configured ``path`` does not exist.
    """
    source = Path(path) if path else Path(DEFAULT_CONFIG_FILE)
    if not source.is_file():
        if path:
            raise ConfidenceConfigError(f"confidence config {source} not found")
        return ConfidenceConfig()
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfidenceConfigError(f"cannot read confidence config {source}: {exc}") from exc
    try:
        return ConfidenceConfig.model_validate(data or {})
    except ValidationError as exc:
        raise ConfidenceConfigError(
            f"invalid confidence config {source}: {_describe(exc)}"
        ) from exc


# --------------------------------------------------------------------------- #
# Scorers
# --------------------------------------------------------------------------- #
class BaseConfidenceScorer(ABC):
    """Turns verified signals into the confidence the policy engine acts on.

    Register alternatives with ``@register_confidence_scorer("name")`` and
    select them with ``confidence.scorer`` in ``confidence.yaml``.
    """

    name: ClassVar[str] = "unnamed"

    @classmethod
    def from_config(cls, config: ConfidenceSection) -> Self:
        """Build the scorer from the ``confidence:`` section. Override as needed."""
        return cls()

    @abstractmethod
    def score(self, signals: ConfidenceSignals) -> ConfidenceResult:
        """Return a value in [0, 1] with a breakdown explaining it. Must be pure."""


class WeightedRuleScorer(BaseConfidenceScorer):
    """Weighted sum of five signals, then ordered cap/floor/multiply rules."""

    name = "weighted_rules"

    def __init__(self, config: ConfidenceSection | None = None) -> None:
        self.config = config or ConfidenceSection()

    @classmethod
    def from_config(cls, config: ConfidenceSection) -> Self:
        return cls(config)

    def raw_score(self, name: str, signals: ConfidenceSignals) -> float | None:
        """Map one signal to [0, 1]; None means "does not apply"."""
        cfg = self.config.signals
        if name == "deploy_timing":
            gap = signals.deploy_onset_gap_minutes
            if gap is None:
                return 0.0
            if gap <= cfg.deploy_timing.strong_window_min:
                return 1.0
            return 0.5 if gap <= cfg.deploy_timing.weak_window_hours * 60 else 0.0
        if name == "dominant_error":
            return _clamp(signals.cited_error_share)
        if name == "judge_score":
            if signals.judge_score is None or not signals.judge_available:
                return None
            return _clamp(signals.judge_score)
        if name == "first_try":
            scores = cfg.first_try.scores
            return scores[min(max(signals.revision_count, 0), len(scores) - 1)]
        if name == "past_match":
            similarity = signals.best_memory_similarity
            if similarity is None or similarity < cfg.past_match.min_similarity:
                return 0.0
            return _clamp(similarity)
        raise KeyError(name)

    def score(self, signals: ConfidenceSignals) -> ConfidenceResult:
        cfg = self.config.signals
        raw: dict[str, float | None] = {}
        notes: dict[str, str] = {}
        for name in SIGNAL_NAMES:
            actions = cfg.get(name).actions
            if actions is not None and signals.action not in actions:
                raw[name], notes[name] = None, f"not used for {signals.action}"
                continue
            raw[name] = self.raw_score(name, signals)
            if raw[name] is None:
                notes[name] = "judge unavailable"

        active = {name: cfg.get(name).weight for name in SIGNAL_NAMES if raw[name] is not None}
        total_weight = sum(active.values())
        breakdown: list[SignalScore] = []
        value = 0.0
        for name in SIGNAL_NAMES:
            score = raw[name]
            weight = active.get(name, 0.0) / total_weight if total_weight else 0.0
            points = score * weight if score is not None else 0.0
            value += points
            breakdown.append(
                SignalScore(
                    signal=name,
                    raw_score=None if score is None else round(score, 4),
                    weight=round(weight, 4),
                    points=round(points, 4),
                    note=notes.get(name, ""),
                )
            )

        value = _clamp(value)
        applied: list[AppliedRule] = []
        for rule in self.config.rules:
            if rule.matches(signals):
                effect, amount = rule.effect
                after = rule.apply(value)
                applied.append(
                    AppliedRule(
                        when=rule.when,
                        effect=effect,
                        amount=amount,
                        before=round(value, 4),
                        after=round(after, 4),
                    )
                )
                value = after
        return ConfidenceResult(
            value=round(value, 4), breakdown=tuple(breakdown), rules=tuple(applied)
        )
