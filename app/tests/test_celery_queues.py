"""B2-4: Celery ops/scheduling queue routing regression tests."""

from app.tasks.celery_app import celery_app


def test_ops_tasks_route_to_ops_queue() -> None:
    routes = celery_app.conf.task_routes
    for prefix in ("tenant_application", "subscription", "audit", "system"):
        assert routes[f"{prefix}.*"]["queue"] == "ops"
