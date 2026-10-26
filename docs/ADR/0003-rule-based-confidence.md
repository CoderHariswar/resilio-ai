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
proportion: `judge_score` when the judge is unavailable, and `deploy_timing`
for actions other than rollback (a restart is not caused by a deploy).

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
- The default weights are a judgment call, not a fit. With them, a clean
  first-time restart scores about 0.80-0.86, close to the 0.8 threshold, and a
  revision pushes it below, so restarts that needed a correction now ask for
  approval. `past_match` counts 0 when there is no history, which costs a new
  incident up to 0.14.
- On the synthetic benchmark the rule-based score is *less* well calibrated
  than the mock LLM's number (Brier 0.047 vs 0.011): the mock gets every root
  cause right and its "self-report" is itself a formula over the same
  templates. That comparison cannot show either way which source is better;
  it needs a real model and, ultimately, real incidents.

Revisit when there are enough labelled incidents to fit weights or a
calibrator, or if a real-model benchmark shows the rule-based score is worse
calibrated than the model's own.
