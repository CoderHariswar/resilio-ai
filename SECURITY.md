# Security Policy

ResilioAI can change production systems, so we take vulnerabilities seriously.

## Reporting

Please **do not open a public issue**. Report privately through
[GitHub Security Advisories](https://github.com/CoderHariswar/resilio-ai/security/advisories/new).
Include affected versions, reproduction steps, and impact. We aim to acknowledge reports within
3 business days and to agree on a disclosure timeline with you.

## Supported versions

Security fixes land on `main` and in the latest released minor version.

## Hardening checklist for deployments

- Set `WEBHOOK_SECRET` (HMAC on every webhook) and `API_TOKEN` (bearer token on approve/reject).
- Run behind TLS. Set `PUBLIC_URL` to the HTTPS URL used in approval links.
- Grant remediation providers the minimum permissions documented in each module's docstring.
- Keep `AUTO_APPROVE_MAX_RISK` at `low` (or `none`) until you trust the system in your environment.
- Use `LLM_PROVIDER=ollama`/`vllm` if incident evidence must not leave your network.
- Replace the development Postgres credentials in `docker-compose.yml`.
