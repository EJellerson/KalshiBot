from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from weather_arb.dashboard import app as dashboard_app


def test_default_cors_origins_are_localhost_only(monkeypatch):
    monkeypatch.delenv("WEATHER_ARB_DASHBOARD_CORS_ORIGINS", raising=False)

    origins = dashboard_app._cors_allowed_origins()

    assert origins == ["http://127.0.0.1:8077", "http://localhost:8077"]
    assert "*" not in origins


def test_env_cors_origins_are_parsed(monkeypatch):
    monkeypatch.setenv(
        "WEATHER_ARB_DASHBOARD_CORS_ORIGINS",
        "http://127.0.0.1:8077, http://localhost:3000",
    )

    origins = dashboard_app._cors_allowed_origins()

    assert origins == ["http://127.0.0.1:8077", "http://localhost:3000"]


def test_admin_token_disabled_fails_closed(monkeypatch):
    monkeypatch.delenv("WEATHER_ARB_DASHBOARD_ADMIN_TOKEN", raising=False)

    with pytest.raises(HTTPException) as exc:
        dashboard_app.require_admin_token("anything")

    assert exc.value.status_code == 403
    assert "WEATHER_ARB_DASHBOARD_ADMIN_TOKEN is not set" in str(exc.value.detail)


def test_admin_token_rejects_missing_or_invalid_header(monkeypatch):
    monkeypatch.setenv("WEATHER_ARB_DASHBOARD_ADMIN_TOKEN", "secret-token")

    for supplied in (None, "", "wrong-token"):
        with pytest.raises(HTTPException) as exc:
            dashboard_app.require_admin_token(supplied)

        assert exc.value.status_code == 403
        assert "invalid or missing X-Admin-Token" in str(exc.value.detail)


def test_admin_token_accepts_matching_header(monkeypatch):
    monkeypatch.setenv("WEATHER_ARB_DASHBOARD_ADMIN_TOKEN", "secret-token")

    assert dashboard_app.require_admin_token("secret-token") is None


def test_mutating_ops_routes_require_admin_dependency():
    app = dashboard_app.create_app()
    routes = {
        (route.path, frozenset(route.methods or set())): [
            getattr(dependency.call, "__name__", "")
            for dependency in route.dependant.dependencies
        ]
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/ops/")
    }

    assert routes[("/api/ops/status", frozenset({"GET"}))] == []
    for path in (
        "/api/ops/restart",
        "/api/ops/shutdown",
        "/api/ops/start-scheduler",
    ):
        assert "require_admin_token" in routes[(path, frozenset({"POST"}))]
