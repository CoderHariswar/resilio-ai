# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Rule-based confidence scoring: a `confidence` graph node scores each accepted hypothesis from
  verified signals (deploy timing, cited error share, judge score, revisions, same-cause past
  incidents) with weights and cap/floor/multiply rules from `confidence.yaml` or
  `CONFIDENCE_CONFIG_PATH`. See `confidence.example.yaml` and ADR 0003.
- Pluggable confidence scorers: `@register_confidence_scorer` and the `resilio.confidence`
  entry-point group.
- The confidence breakdown appears in the timeline, approval and auto-remediation messages,
  `resilio show`, `resilio status` and the review page.
- Metrics `resilio_confidence_score` and `resilio_llm_confidence_delta`.
- `resilio eval run` reports Brier score and expected calibration error for the model's
  confidence and the rule-based score (`--confidence-source llm|rules` to show one).

### Changed
- The policy engine uses the rule-based confidence. The model's self-reported value is kept as
  `llm_confidence` for comparison only.

### Fixed
- `past_match` no longer scores 0 for an incident with no same-cause history; its weight goes to
  the other signals. Before, first-time restarts that needed one revision fell below
  `MIN_AUTO_CONFIDENCE` and waited for approval, which also made the CI smoke test flaky.

## [0.1.0] - 2026-10-24

### Added
- LangGraph incident workflow: telemetry → VCS correlation → synthesizer → evaluator
  (grounding checks + LLM-as-a-judge, bounded revisions) → risk-tiered policy →
  human approval gate (`interrupt()`) → remediation with health verification → escalation.
- Providers: telemetry (OpenTelemetry/Jaeger, Datadog, Sentry, Prometheus, Mock),
  remediation (GitHub Actions, ArgoCD, Kubernetes API, Mock), notifications (Slack,
  PagerDuty, MS Teams, Terminal, Mock), LLMs (OpenAI, Anthropic, DeepSeek, Ollama, vLLM, Mock).
  Includes a registry with entry-point plugin discovery.
- FastAPI service with Sentry/Datadog/Alertmanager/generic webhooks, HMAC verification,
  bearer-token approvals, a review page, `/healthz`, `/readyz`, and `/metrics`.
- PostgreSQL persistence: incidents, pgvector incident memory, and LangGraph checkpoints.
  Redis queue with alert-storm deduplication. In-memory fallbacks for both.
- `resilio` CLI: `dev simulate|replay|scenarios`, `status`, `approve`, `reject`, `show`,
  `eval run|seed`, `providers`, `serve`.
- 50-scenario offline benchmark for judge precision and hallucination escape rates.
- Mock Mode: `docker compose up --build` with zero API keys.
- Real-model benchmark results in the README: llama3.2:3b and qwen2.5:7b via Ollama.

### Changed
- Calibrated the judge prompt after the first real-model run: it now rejects only for named,
  concrete defects and writes its own diagnosis of the evidence first. The original prompt made
  small models reject every correct analysis (llama3.2:3b recall: 0% on a 5-scenario smoke test,
  96.7% on the full 50).
