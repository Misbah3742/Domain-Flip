"""
api – FastAPI application for inspecting and managing monitored domains.

Endpoints
---------
GET  /health                   Liveness probe.
GET  /domains                  List tracked domains (paginated).
GET  /domains/{name}           Get a single domain record.
POST /domains                  Add a domain to the tracking list.
DELETE /domains/{name}         Remove a domain from tracking.
POST /domains/{name}/snipe     Manually trigger a snipe for a domain.
GET  /metrics/{name}           Retrieve valuation metrics for a domain.
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(
    title="Domain-Flip API",
    description="Inspect and manage the domain monitoring & sniping system.",
    version="0.1.0",
)


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health", tags=["system"])
def health() -> dict:
    """Liveness probe – returns 200 OK when the service is running."""
    return {"status": "ok"}


# ─── Domains ──────────────────────────────────────────────────────────────────

@app.get("/domains", tags=["domains"])
def list_domains(skip: int = 0, limit: int = 50) -> dict:
    """
    Return a paginated list of all tracked domains.

    Query Parameters
    ----------------
    skip : int
        Number of records to skip (for pagination).
    limit : int
        Maximum number of records to return (max 200).
    """
    # TODO: query PostgreSQL via SQLAlchemy
    return {"domains": [], "skip": skip, "limit": limit}


@app.get("/domains/{name}", tags=["domains"])
def get_domain(name: str) -> dict:
    """Retrieve a single domain record by fully-qualified name."""
    # TODO: query PostgreSQL
    return {"name": name}


@app.post("/domains", tags=["domains"], status_code=201)
def add_domain(name: str) -> dict:
    """
    Add *name* to the monitoring list.

    The domain is first checked against the :mod:`~app.trademark_filter`
    before being persisted.  Flagged domains are rejected with HTTP 409.
    """
    # TODO: trademark check → DB insert → enqueue for monitoring
    return {"name": name, "queued": True}


@app.delete("/domains/{name}", tags=["domains"])
def remove_domain(name: str) -> dict:
    """Remove *name* from the monitoring list."""
    # TODO: DB delete
    return {"deleted": name}


@app.post("/domains/{name}/snipe", tags=["domains"])
def manual_snipe(name: str) -> dict:
    """Manually trigger a snipe attempt for *name*."""
    # TODO: enqueue_snipe(name)
    return {"name": name, "sniped": True}


# ─── Metrics ──────────────────────────────────────────────────────────────────

@app.get("/metrics/{name}", tags=["metrics"])
def get_metrics(name: str) -> dict:
    """Retrieve the latest valuation metrics for *name*."""
    # TODO: query domain_metrics table
    return {"name": name, "metrics": {}}
