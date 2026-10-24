# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Judge prompt now rejects only for named, concrete defects and writes its own diagnosis of the
  evidence before comparing it with the analysis. The old prompt made small models reject every
  correct RCA because causality wasn't "proven" (llama3.2:3b recall: 0% on a 5-scenario smoke test, 96.7% on the full 50).

### Added
- First real-model benchmark results in the README: llama3.2:3b and qwen2.5:7b via Ollama.

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
