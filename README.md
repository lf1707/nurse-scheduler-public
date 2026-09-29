# Nurse Scheduler

Nurse Scheduler is a multi-tenant staff scheduling application built with
FastAPI, PostgreSQL row-level security, Redis, Celery, and Google OR-Tools
CP-SAT. It models nurses, shift templates, skill requirements, scheduling
rules, and solver-backed schedule generation.

This repository is a sanitized source distribution. It intentionally does not
include the private operational repository's Git history, deployment
orchestration, infrastructure configuration, release records, or credentials.

## Features

- Multi-tenant authentication, role-based authorization, and PostgreSQL RLS
- Nurse, shift, rule, and schedule management APIs
- CP-SAT schedule generation with asynchronous Celery workers
- HTMX/Alpine web interface
- Audit logging, export encryption, and operational health checks
- Email-verified tenant onboarding and subscription limits

## Requirements

- Python 3.11
- PostgreSQL 16
- Redis 7
- [uv](https://docs.astral.sh/uv/)

## Local development

Configure PostgreSQL and Redis, then create your local environment:

```bash
cp .env.example .env
uv sync --frozen --extra dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

In another terminal, start the worker:

```bash
uv run celery -A app.tasks.celery_app worker --loglevel=info \
  --queues=scheduling,ops
```

The application listens on `http://localhost:8000`. Set `DATABASE_URL`,
`REDIS_URL`, `JWT_SECRET`, `SECRET_KEY`, and super-admin credentials before
running anything outside your local machine.

## Tests

The test suite expects PostgreSQL and Redis. The GitHub Actions configuration
in `.github/workflows/checks.yml` is a runnable reference for service
configuration and required environment variables.

```bash
uv run ruff check app/ scripts/
uv run mypy app
uv run pytest app/tests/
```

## Security

Do not report security issues through public GitHub issues. See
[SECURITY.md](SECURITY.md).

## License

Released under the [MIT License](LICENSE). The CP-SAT implementation derives
part of its scheduling logic from
[roster-wizard](https://github.com/galojix/roster-wizard), also MIT licensed.
