"""WeightedRuleScorer, its YAML config, and signal extraction."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from resilio.scoring.confidence import (
    ConfidenceConfig,
    ConfidenceConfigError,
    ConfidenceResult,
    ConfidenceSection,
    ConfidenceSignals,
    WeightedRuleScorer,
    load_confidence_config,
    signals_from_evidence,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "confidence.example.yaml"

# The worked example in the docs: deploy 9 min before onset, cited groups hold
# 6,100 of 8,412 errors, judge 0.82, one revision, past incident at 0.78.
WORKED = ConfidenceSignals(
    action="rollback",
    deploy_onset_gap_minutes=9,
    cited_error_share=6100 / 8412,
    judge_score=0.82,
    revision_count=1,
    best_memory_similarity=0.78,
)


def _scorer(**section: Any) -> WeightedRuleScorer:
    return WeightedRuleScorer(ConfidenceSection.model_validate(section))


def _points(result: ConfidenceResult) -> dict[str, float]:
    return {s.signal: s.points for s in result.breakdown}


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def test_worked_example() -> None:
    result = WeightedRuleScorer().score(WORKED)
    assert round(result.value, 2) == 0.82
    assert _points(result) == {
        "deploy_timing": 0.3,
        "dominant_error": 0.145,
        "judge_score": 0.246,
        "first_try": 0.05,
        "past_match": 0.078,
    }
    assert result.rules == ()
    assert result.summary().startswith("0.82 (deploy timing +0.30, judge +0.25, dominant error")


@pytest.mark.parametrize(
    ("gap", "expected"),
    [(None, 0.0), (0, 1.0), (9, 1.0), (30, 1.0), (31, 0.5), (360, 0.5), (361, 0.0)],
)
def test_deploy_timing(gap: float | None, expected: float) -> None:
    signals = replace(WORKED, deploy_onset_gap_minutes=gap)
    assert WeightedRuleScorer().raw_score("deploy_timing", signals) == expected


@pytest.mark.parametrize(("share", "expected"), [(0.4, 0.4), (1.3, 1.0), (-0.2, 0.0)])
def test_dominant_error_is_clamped(share: float, expected: float) -> None:
    signals = replace(WORKED, cited_error_share=share)
    assert WeightedRuleScorer().raw_score("dominant_error", signals) == expected


@pytest.mark.parametrize(("revisions", "expected"), [(0, 1.0), (1, 0.5), (2, 0.25), (7, 0.25)])
def test_first_try(revisions: int, expected: float) -> None:
    signals = replace(WORKED, revision_count=revisions)
    assert WeightedRuleScorer().raw_score("first_try", signals) == expected


def test_first_try_mapping_is_configurable() -> None:
    scorer = _scorer(
        signals={
            "first_try": {"weight": 0.1, "scores": [1.0, 0.0]},
        }
    )
    assert scorer.raw_score("first_try", replace(WORKED, revision_count=3)) == 0.0


@pytest.mark.parametrize(("similarity", "expected"), [(None, None), (0.49, 0.0), (0.5, 0.5)])
def test_past_match(similarity: float | None, expected: float | None) -> None:
    signals = replace(WORKED, best_memory_similarity=similarity)
    assert WeightedRuleScorer().raw_score("past_match", signals) == expected


def test_deploy_timing_weight_goes_to_others_for_non_rollbacks() -> None:
    signals = ConfidenceSignals(action="restart", cited_error_share=1.0, judge_score=1.0)
    result = WeightedRuleScorer().score(signals)
    deploy = result.breakdown[0]
    assert (deploy.raw_score, deploy.weight, deploy.note) == (None, 0.0, "not used for restart")
    assert round(sum(s.weight for s in result.breakdown), 3) == 1.0
    past = result.breakdown[-1]
    assert (past.raw_score, past.weight) == (None, 0.0)
    assert past.note == "no past incident with this root cause"
    assert result.value == 1.0  # every applicable signal is perfect


def test_first_time_restart_after_one_revision_still_clears_threshold() -> None:
    # The Mock Mode case CI exercises: no history, one correction, judge 1.0.
    signals = ConfidenceSignals(
        action="restart", cited_error_share=0.85, judge_score=1.0, revision_count=1
    )
    value = WeightedRuleScorer().score(signals).value
    assert value == round((0.2 * 0.85 + 0.3 + 0.1 * 0.5) / 0.6, 4)
    assert value >= 0.8


def test_missing_judge_redistributes_weight_then_rule_applies() -> None:
    signals = replace(WORKED, judge_score=None, judge_available=False, revision_count=0)
    result = WeightedRuleScorer().score(signals)
    judge = next(s for s in result.breakdown if s.signal == "judge_score")
    assert (judge.raw_score, judge.weight, judge.note) == (None, 0.0, "judge unavailable")
    before = (0.3 + 0.2 * WORKED.cited_error_share + 0.1 + 0.1 * 0.78) / 0.7
    assert result.rules[0].when == "judge_unavailable"
    assert result.rules[0].before == round(before, 4)
    assert result.value == round(before * 0.8, 4)


def test_value_is_clamped_to_one() -> None:
    perfect = ConfidenceSignals(
        action="rollback",
        deploy_onset_gap_minutes=1,
        cited_error_share=1.0,
        judge_score=1.0,
        best_memory_similarity=1.0,
    )
    assert WeightedRuleScorer().score(perfect).value == 1.0
    multiply = _scorer(rules=[{"when": "action_is:rollback", "multiply": 3}])
    assert multiply.score(WORKED).value == 1.0


def test_rollback_without_suspect_is_capped() -> None:
    result = WeightedRuleScorer().score(replace(WORKED, deploy_onset_gap_minutes=None))
    assert result.value == 0.4
    assert result.rules[0].effect == "cap"
    assert "cap 0.4 (rollback_without_suspect_deploy)" in result.summary()


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ({"when": "action_is:rollback", "floor": 0.95}, 0.95),
        ({"when": "action_is:restart", "floor": 0.95}, None),
        ({"when": "revisions_at_least:1", "cap": 0.5}, 0.5),
        ({"when": "revisions_at_least:2", "cap": 0.5}, None),
        ({"when": "judge_unavailable", "multiply": 0.5}, None),
    ],
)
def test_rule_effects(rule: dict[str, Any], expected: float | None) -> None:
    base = _scorer(rules=[]).score(WORKED).value
    result = _scorer(rules=[rule]).score(WORKED)
    assert result.value == (base if expected is None else expected)
    assert len(result.rules) == (0 if expected is None else 1)


def test_rules_apply_in_order() -> None:
    cap_then_multiply = [
        {"when": "action_is:rollback", "cap": 0.5},
        {"when": "action_is:rollback", "multiply": 0.5},
    ]
    a = _scorer(rules=cap_then_multiply).score(WORKED)
    b = _scorer(rules=list(reversed(cap_then_multiply))).score(WORKED)
    assert a.value == 0.25
    assert b.value == round(WeightedRuleScorer().score(WORKED).value * 0.5, 4)
    assert [r.effect for r in a.rules] == ["cap", "multiply"]
    assert [r.effect for r in b.rules] == ["multiply", "cap"]


def test_scoring_is_deterministic() -> None:
    scorer = WeightedRuleScorer()
    assert scorer.score(WORKED) == scorer.score(WORKED)
    assert WeightedRuleScorer().score(WORKED).as_dict() == scorer.score(WORKED).as_dict()


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_defaults_without_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_confidence_config() == ConfidenceConfig()


def test_example_file_matches_built_in_defaults() -> None:
    assert load_confidence_config(EXAMPLE) == ConfidenceConfig()


def test_working_directory_confidence_yaml_is_picked_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "confidence.yaml").write_text("confidence:\n  rules: []\n")
    assert load_confidence_config().confidence.rules == []


def test_valid_custom_file(tmp_path: Path) -> None:
    path = tmp_path / "custom.yaml"
    path.write_text(
        "confidence:\n"
        "  signals:\n"
        "    deploy_timing: {weight: 0.5, strong_window_min: 10, actions: null}\n"
        "    dominant_error: {weight: 0.5}\n"
        "    judge_score: {weight: 0}\n"
        "    first_try: {weight: 0}\n"
        "    past_match: {weight: 0}\n"
        "  rules:\n"
        "    - when: revisions_at_least:2\n"
        "      floor: 0.1\n"
    )
    section = load_confidence_config(path).confidence
    assert section.signals.deploy_timing.strong_window_min == 10
    assert section.signals.deploy_timing.actions is None
    restart = replace(WORKED, action="restart", deploy_onset_gap_minutes=None)
    expected = round(0.5 * WORKED.cited_error_share, 4)
    assert WeightedRuleScorer(section).score(restart).value == expected


@pytest.mark.parametrize(
    ("body", "fragments"),
    [
        (
            "confidence:\n  signals:\n    judge_score: {weight: 0.2}\n",
            ["confidence.signals", "weights must sum to 1.0, got 0.9"],
        ),
        (
            "confidence:\n  signals:\n    judge_score: {weight: 1.5}\n",
            ["confidence.signals.judge_score.weight", "less than or equal to 1"],
        ),
        (
            "confidence:\n  signals:\n    deploy_timing: {weight: 0.3, strong_window_min: -5}\n",
            ["confidence.signals.deploy_timing.strong_window_min"],
        ),
        (
            "confidence:\n  signals:\n    judge_score: {wieght: 0.3}\n",
            ["confidence.signals.judge_score.wieght", "Extra inputs are not permitted"],
        ),
        ("confidence:\n  scorr: weighted_rules\n", ["confidence.scorr"]),
        (
            "confidence:\n  rules:\n    - when: llm_says_so\n      cap: 0.5\n",
            ["confidence.rules.0.when", "unknown condition 'llm_says_so'"],
        ),
        (
            "confidence:\n  rules:\n    - when: action_is:nuke\n      cap: 0.5\n",
            ["unknown condition 'action_is:nuke'"],
        ),
        (
            "confidence:\n  rules:\n    - when: judge_unavailable\n"
            "      cap: 0.5\n      floor: 0.1\n",
            ["confidence.rules.0", "exactly one of cap, floor, multiply (got cap, floor)"],
        ),
        (
            "confidence:\n  signals:\n    deploy_timing: {weight: 0.3, actions: [reboot]}\n",
            ["confidence.signals.deploy_timing.actions", "unknown action(s) reboot"],
        ),
        ("confidence: [1, 2\n", ["cannot read confidence config"]),
        ("- just\n- a list\n", ["<root>", "valid dictionary"]),
    ],
)
def test_invalid_config_names_the_field(tmp_path: Path, body: str, fragments: list[str]) -> None:
    path = tmp_path / "confidence.yaml"
    path.write_text(body)
    with pytest.raises(ConfidenceConfigError) as info:
        load_confidence_config(path)
    for fragment in fragments:
        assert fragment in str(info.value)


def test_explicit_missing_path_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfidenceConfigError, match="not found"):
        load_confidence_config(tmp_path / "nope.yaml")


# --------------------------------------------------------------------------- #
# Signal extraction
# --------------------------------------------------------------------------- #
def test_signals_from_evidence() -> None:
    signals = signals_from_evidence(
        hypothesis={"action": "rollback", "root_cause": "memory_leak", "evidence": ["a", "b"]},
        evaluation={"score": 0.9, "judge_available": True},
        trace_groups=[
            {"fingerprint": "a", "count": 60},
            {"fingerprint": "b", "count": 20},
            {"fingerprint": "c", "count": 20},
        ],
        total_traces=200,
        deploy_onset_gap_minutes=4.0,
        similar_incidents=[
            {"root_cause": "disk_exhaustion", "similarity": 0.99},
            {"root_cause": "memory_leak", "similarity": 0.7},
        ],
        revision_count=1,
    )
    assert signals == ConfidenceSignals(
        action="rollback",
        deploy_onset_gap_minutes=4.0,
        cited_error_share=0.4,
        judge_score=0.9,
        judge_available=True,
        revision_count=1,
        best_memory_similarity=0.7,
    )


def test_signals_without_judge_or_history() -> None:
    signals = signals_from_evidence(
        hypothesis={"action": "restart", "evidence": ["a"]},
        evaluation={"score": 1.0, "judge_available": False},
        trace_groups=[{"fingerprint": "a", "count": 5}, {"fingerprint": "b", "count": 5}],
    )
    assert signals.judge_score is None
    assert signals.cited_error_share == 0.5
    assert signals.best_memory_similarity is None
