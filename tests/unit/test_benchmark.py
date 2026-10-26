from __future__ import annotations

from collections import Counter
from pathlib import Path

from resilio.config import Settings
from resilio.graph.nodes.evaluator_node import check_grounding
from resilio.providers.llm.mock import MockLLMProvider
from resilio.scenarios import SCENARIO_TYPES
from resilio.utils.evaluator_benchmark import (
    calibration,
    generate_scenarios,
    load_scenarios,
    run_benchmark,
    save_scenarios,
)


def test_generates_fifty_balanced_deterministic_scenarios() -> None:
    scenarios = generate_scenarios()
    assert len(scenarios) == 50
    assert Counter(s.incident_type for s in scenarios) == dict.fromkeys(SCENARIO_TYPES, 5)
    assert Counter(s.label_valid for s in scenarios) == {True: 30, False: 20}
    again = generate_scenarios()
    assert [s.candidate for s in scenarios] == [s.candidate for s in again]
    assert [s.trace_groups for s in scenarios] != [
        s.trace_groups for s in generate_scenarios(seed=7)
    ]


def test_labels_match_grounding_for_fabrications() -> None:
    for scenario in generate_scenarios():
        issues = check_grounding(scenario.candidate, scenario.trace_groups, scenario.deployments)
        if scenario.label == "fabricated":
            assert issues, scenario.id
        elif scenario.label_valid:
            assert not issues, (scenario.id, issues)


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    scenarios = generate_scenarios(count=12)
    path = tmp_path / "bench" / "scenarios.json"
    save_scenarios(scenarios, path)
    assert load_scenarios(path) == scenarios


async def test_benchmark_report_metrics() -> None:
    settings = Settings(_env_file=None, mock_llm_fault_rate=0.5)  # type: ignore[call-arg]
    report = await run_benchmark(generate_scenarios(), MockLLMProvider(fault_rate=0.5), settings)
    assert report.scenarios == 50
    assert report.judge_precision >= 0.95
    assert report.hallucination_escape_rate == 0.0
    assert report.first_pass_hallucination_rate > 0.2
    assert report.correction_rate > 0.2
    assert report.final_hallucination_rate == 0.0
    assert report.rca_accuracy >= 0.9
    data = report.to_dict()
    assert set(data["by_type"]) == set(SCENARIO_TYPES)
    assert data["by_type"]["memory-leak"]["n"] == 5

    accepted = [r for r in report.results if r.pipeline_accepted]
    assert all(r.llm_confidence is not None and r.rules_confidence is not None for r in accepted)
    assert set(report.calibration) == {"llm", "rules"}
    assert report.calibration["rules"]["n"] == len(accepted)
    assert 0.0 <= report.calibration["rules"]["brier"] <= 1.0


def test_calibration_scores() -> None:
    assert calibration([]) == {
        "n": 0,
        "brier": 0.0,
        "ece": 0.0,
        "mean_confidence": 0.0,
        "accuracy": 0.0,
    }
    perfect = calibration([(1.0, True), (0.0, False)])
    assert (perfect["brier"], perfect["ece"]) == (0.0, 0.0)
    # Two bins: 0.9 -> 1/2 right, 0.25 -> 1/2 right.
    pairs = [(0.9, True), (0.9, False), (0.25, True), (0.25, False)]
    result = calibration(pairs)
    assert result["brier"] == round((0.01 + 0.81 + 0.5625 + 0.0625) / 4, 4)
    assert result["ece"] == round(0.5 * 0.4 + 0.5 * 0.25, 4)
    assert (result["mean_confidence"], result["accuracy"]) == (0.575, 0.5)
