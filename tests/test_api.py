"""
tests/test_api.py

Unit tests for the FastAPI endpoints.
Uses an in-memory SQLite database via SQLAlchemy so no PostgreSQL or Redis is
required.  The Redis queue helpers are monkey-patched to no-ops.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import app, get_db
from app.database import Base, Domain, DomainStatus


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_engine():
    """
    In-memory SQLite engine for testing.

    StaticPool forces all SQLAlchemy checkouts to reuse the *same* underlying
    connection, which is required for SQLite in-memory databases – otherwise
    each pool checkout gets a brand-new, empty database.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """Session bound to the in-memory engine."""
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


@pytest.fixture()
def client(db_session, monkeypatch):
    """TestClient with the DB dependency overridden to use SQLite."""

    def override_get_db():
        yield db_session

    # Prevent real Redis calls
    monkeypatch.setattr("app.api.enqueue_domain_check", lambda *a, **kw: None)
    monkeypatch.setattr("app.api.enqueue_snipe", lambda *a, **kw: None)

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# ─── /health ──────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_returns_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


# ─── /domains (list) ──────────────────────────────────────────────────────────

class TestListDomains:
    def test_empty_list(self, client):
        response = client.get("/domains")
        assert response.status_code == 200
        data = response.json()
        assert data["domains"] == []
        assert data["total"] == 0

    def test_returns_seeded_domains(self, client, db_session):
        db_session.add(Domain(name="alpha.com", tld="com", status=DomainStatus.ACTIVE))
        db_session.add(Domain(name="beta.com", tld="com", status=DomainStatus.EXPIRING_SOON))
        db_session.commit()

        response = client.get("/domains")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        names = {d["name"] for d in data["domains"]}
        assert names == {"alpha.com", "beta.com"}

    def test_pagination_skip_and_limit(self, client, db_session):
        for i in range(5):
            db_session.add(Domain(name=f"domain{i}.com", tld="com", status=DomainStatus.ACTIVE))
        db_session.commit()

        response = client.get("/domains?skip=2&limit=2")
        assert response.status_code == 200
        data = response.json()
        assert len(data["domains"]) == 2
        assert data["total"] == 5

    def test_limit_capped_at_200(self, client):
        response = client.get("/domains?limit=9999")
        assert response.status_code == 200

    def test_filter_by_valid_status(self, client, db_session):
        db_session.add(Domain(name="soon.com", tld="com", status=DomainStatus.EXPIRING_SOON))
        db_session.add(Domain(name="active.com", tld="com", status=DomainStatus.ACTIVE))
        db_session.commit()

        response = client.get("/domains?status=expiring_soon")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["domains"][0]["name"] == "soon.com"

    def test_filter_by_invalid_status_returns_400(self, client):
        response = client.get("/domains?status=nonexistent")
        assert response.status_code == 400


# ─── /domains/{name} (get single) ─────────────────────────────────────────────

class TestGetDomain:
    def test_get_existing_domain(self, client, db_session):
        db_session.add(Domain(name="mysite.com", tld="com", status=DomainStatus.ACTIVE))
        db_session.commit()

        response = client.get("/domains/mysite.com")
        assert response.status_code == 200
        assert response.json()["name"] == "mysite.com"

    def test_get_missing_domain_returns_404(self, client):
        response = client.get("/domains/notexist.com")
        assert response.status_code == 404


# ─── /domains (add) ───────────────────────────────────────────────────────────

class TestAddDomain:
    def test_add_clean_domain(self, client):
        response = client.post("/domains", json={"name": "mycleansite.com"})
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "mycleansite.com"
        assert data["queued"] is True

    def test_add_trademarked_domain_returns_409(self, client):
        response = client.post("/domains", json={"name": "google.com"})
        assert response.status_code == 409

    def test_add_duplicate_domain_returns_409(self, client, db_session):
        db_session.add(Domain(name="duplicate.com", tld="com", status=DomainStatus.ACTIVE))
        db_session.commit()

        response = client.post("/domains", json={"name": "duplicate.com"})
        assert response.status_code == 409

    def test_add_domain_persisted_in_db(self, client, db_session):
        client.post("/domains", json={"name": "newsite.io"})
        domain = db_session.query(Domain).filter(Domain.name == "newsite.io").first()
        assert domain is not None
        assert domain.tld == "io"


# ─── /domains/{name} (delete) ─────────────────────────────────────────────────

class TestRemoveDomain:
    def test_remove_existing_domain(self, client, db_session):
        db_session.add(Domain(name="removeme.com", tld="com", status=DomainStatus.ACTIVE))
        db_session.commit()

        response = client.delete("/domains/removeme.com")
        assert response.status_code == 200
        assert response.json()["deleted"] == "removeme.com"

        remaining = db_session.query(Domain).filter(Domain.name == "removeme.com").first()
        assert remaining is None

    def test_remove_missing_domain_returns_404(self, client):
        response = client.delete("/domains/nobody.com")
        assert response.status_code == 404


# ─── /domains/{name}/snipe ───────────────────────────────────────────────────

class TestManualSnipe:
    def test_snipe_existing_domain(self, client, db_session):
        db_session.add(Domain(name="target.com", tld="com", status=DomainStatus.PENDING_DELETE))
        db_session.commit()

        response = client.post("/domains/target.com/snipe")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "target.com"
        assert data["sniped"] is True

    def test_snipe_missing_domain_returns_404(self, client):
        response = client.post("/domains/ghost.com/snipe")
        assert response.status_code == 404


# ─── /metrics/{name} ─────────────────────────────────────────────────────────

class TestGetMetrics:
    def test_metrics_empty_when_none_stored(self, client, db_session):
        db_session.add(Domain(name="nometrics.com", tld="com", status=DomainStatus.ACTIVE))
        db_session.commit()

        response = client.get("/metrics/nometrics.com")
        assert response.status_code == 200
        assert response.json() == {"name": "nometrics.com", "metrics": {}}

    def test_metrics_returns_404_for_unknown_domain(self, client):
        response = client.get("/metrics/unknown.com")
        assert response.status_code == 404
