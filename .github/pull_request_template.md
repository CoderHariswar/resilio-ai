## What & why

<!-- One or two sentences. Link the issue: "Closes #123". -->

## How was this tested?

- [ ] `make check` passes (ruff, mypy --strict, pytest)
- [ ] New/changed behaviour has tests (providers: use `httpx.MockTransport`, see `tests/unit/test_telemetry_providers.py`)
- [ ] Tried it in Mock Mode: `resilio dev simulate --local --type <type>`

## Safety

- [ ] This PR does **not** widen what ResilioAI can do to production without human approval
      (if it does, explain the policy-engine impact and consider an ADR in `docs/ADR/`)
- [ ] No secrets, tokens, or customer data in code, tests, fixtures or screenshots

## Docs

- [ ] README / docs / `.env.example` updated if user-facing behaviour or settings changed
