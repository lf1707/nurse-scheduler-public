# Contributing

Thanks for considering a contribution to Nurse Scheduler.

## Development workflow

1. Fork the repository and create a focused branch from `main`.
2. Install dependencies with `uv sync --frozen --extra dev`.
3. Keep changes minimal and follow the existing FastAPI, SQLAlchemy, Celery,
   HTMX, and pytest patterns.
4. Add or update tests for behavior changes.
5. Run the local verification commands:

   ```bash
   uv run ruff check app/ scripts/
   uv run mypy app
   uv run pytest app/tests/
   ```

6. Open a pull request with the motivation, behavior change, and test results.

## Code guidelines

- Preserve tenant isolation and add coverage for cross-tenant access.
- Do not introduce plaintext secrets, credentials, hostnames, tokens, logs, or
  customer data in examples or tests.
- Prefer the existing domain models and service boundaries over parallel
  implementations.
- Keep database migrations forward-only and reversible in operational terms.

## Issue reports

Include the expected behavior, observed behavior, minimal reproduction, Python
version, and relevant logs with secrets redacted. For security vulnerabilities,
follow [SECURITY.md](SECURITY.md) instead.
