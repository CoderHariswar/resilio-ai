# 0003. Rule-based confidence instead of LLM self-reported confidence

- Status: Accepted
- Date: 2026-10-26
- Deciders: ResilioAI maintainers

## Context

The policy engine raises an action's risk tier when confidence is below
`MIN_AUTO_CONFIDENCE` (0.8). Until now that number was whatever the
synthesizer LLM wrote in its JSON. A self-reported confidence:

- **is not calibrated.** Models routinely say 0.9 for answers that are right
  far less often than 90% of the time, and small local models more so.
- **varies between runs** of the same evidence, so the same incident can be
  auto-remediated once and sent for approval the next time.
- **cannot be explained.** An on-call engineer asked to approve a rollback
  cannot see why the agent is "0.85 sure".
- **can be steered** by the evidence itself: log lines are attacker-controlled
  text inside the prompt.

The workflow already verifies most of what a careful SRE would look at: which
fingerprint groups the hypothesis cites and how much of the error volume they
cover, whether a deploy landed shortly before the onset, the judge's verdict,
and how many revisions it took. Confidence should come from those facts.

## Decision

Compute confidence in a dedicated `confidence` graph node, between the
evaluator's accept route and the policy node, with a pluggable scorer
([`scoring/confidence.py`](../../resilio/scoring/confidence.py)). The default
`WeightedRuleScorer` takes a weighted sum of five signals (`deploy_timing`,
`dominant_error`, `judge_score`, `first_try`, `past_match`) and then applies
ordered `cap` / `floor` / `multiply` rules chosen from a fixed set of named
conditions. A signal that does not apply hands its weight to the others in
proportion: `judge_score` when the judge is unavailable, `deploy_timing`
for actions other than rollback (a restart is not caused by a deploy), and
`past_match` when no past incident shares the root cause (absence of history
is not evidence against a diagnosis).

Weights and rules live in `confidence.yaml` (or `CONFIDENCE_CONFIG_PATH`),
validated with Pydantic at startup; without a file the built-in defaults,
identical to `confidence.example.yaml`, apply. Scorers register through the
provider registry (`@register_confidence_scorer`, entry-point group
`resilio.confidence`). The policy acts on the rule-based value only. The
model's number is kept as `llm_confidence` for logging and for the benchmark's
calibration comparison.

A separate node rather than code inside the policy node keeps the policy
engine about risk alone, gives the score its own timeline entry and
`resilio_node_duration_seconds{node="confidence"}` series, and makes the
scorer replaceable without touching risk logic.

## Alternatives considered

**Keep the LLM's self-report.** Zero work, and with a strong model it can be
reasonable. Rejected for the reasons above: it is not explainable or stable,
and the agent acts on it.

**Token log-probabilities.** Turning the probability of the chosen
`root_cause` token into a confidence is more principled than a written number.
Rejected for now: not every supported provider exposes log-probabilities, the
value measures how likely the text was rather than whether the diagnosis is
right, and it still ignores the verified facts we already have.

**A learned calibrator** (logistic regression or isotonic regression over the
same signals, fitted to labelled incidents). The best long-term option, and
the signals were chosen so one can be dropped in as another registered scorer.
Rejected as the default because it needs real labelled incidents, which a
fresh install does not have; fitting it to the synthetic benchmark would only
learn the templates.

**Free-form rule expressions** (e.g. `when: "judge_score > 0.8 and ..."`).
Flexible, but evaluating user-supplied expressions in the process that can
roll back production is a needless risk. A fixed set of named conditions
covers the cases we know about, and new ones are a few lines of reviewed code.

## Consequences

- Every confidence value is deterministic and explainable: the breakdown is
  in the timeline, the approval message, `resilio show` and the incident row.
- Operators can tune weights and rules per environment without code changes;
  a bad file stops startup with an error naming the field.
- Confidence can only raise risk through the existing policy rule. Grounding
  failures still reject, `action: none` still escalates, and a perfect score
  does not skip the approval gate for a medium-risk action. Integration tests
  pin all three.
- The default weights are a judgment call, not a fit. With them, a
  first-time restart with a fully confident judge scores about 0.9, or about
  0.87 after one revision, so it still runs unattended; a judge score below
  about 0.75 sends it for approval.
- An earlier draft scored `past_match` as 0 when there was no history. That
  cost every new incident 0.10 of weight, pushed restarts that needed one
  revision below 0.8, and made the CI smoke test fail for the quarter of runs
  where the mock LLM injects a hallucination. No history now hands its weight
  to the other signals instead.
- On the synthetic benchmark the two sources come out nearly identical (Brier
  0.012 rule-based vs 0.011 for the mock LLM's number). The mock gets every
  root cause right and its "self-report" is itself a formula over the same
  templates, so the comparison cannot show which source is better; it needs a
  real model and, ultimately, real incidents.

Revisit when there are enough labelled incidents to fit weights or a
calibrator, or if a real-model benchmark shows the rule-based score is worse
calibrated than the model's own.
