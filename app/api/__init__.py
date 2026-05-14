"""
api – FastAPI application for inspecting and managing monitored domains.

Endpoints
---------
GET  /health                   Liveness probe.
GET  /check-domain/{name}      Live WHOIS check for a domain (no DB required).
GET  /domains                  List tracked domains (paginated).
GET  /domains/{name}           Get a single domain record.
POST /domains                  Add a domain to the tracking list.
DELETE /domains/{name}         Remove a domain from tracking.
POST /domains/{name}/snipe     Manually trigger a snipe for a domain.
GET  /metrics/{name}           Retrieve valuation metrics for a domain.
"""

from __future__ import annotations

from typing import Generator, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import Domain, DomainMetrics, DomainStatus, get_session_factory
from app.monitor import check_domain_async
from app.queue import enqueue_domain_check, enqueue_snipe
from app.trademark_filter import TrademarkFilter

app = FastAPI(
    title="Domain-Flip API",
    description="Inspect and manage the domain monitoring & sniping system.",
    version="0.1.0",
)

_trademark_filter = TrademarkFilter()


# ─── DB session dependency ────────────────────────────────────────────────────

def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a SQLAlchemy session."""
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
    finally:
        session.close()


# ─── Request / response models ────────────────────────────────────────────────

class AddDomainRequest(BaseModel):
    name: str


def _domain_to_dict(domain: Domain) -> dict:
    return {
        "name": domain.name,
        "tld": domain.tld,
        "status": domain.status.value if domain.status else None,
        "expires_at": domain.expires_at.isoformat() if domain.expires_at else None,
        "registered_at": domain.registered_at.isoformat() if domain.registered_at else None,
        "trademark_flagged": domain.trademark_flagged,
        "trademark_reason": domain.trademark_reason,
        "created_at": domain.created_at.isoformat() if domain.created_at else None,
        "updated_at": domain.updated_at.isoformat() if domain.updated_at else None,
    }


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health", tags=["system"])
def health() -> dict:
    """Liveness probe – returns 200 OK when the service is running."""
    return {"status": "ok"}


# ─── Live domain check ────────────────────────────────────────────────────────

@app.get("/check-domain/{name}", tags=["domains"])
async def check_domain_live(name: str) -> dict:
    """
    Perform a live WHOIS lookup for *name* and return its current lifecycle status.

    This endpoint calls WhoisXML directly – no database record is required.
    Useful for ad-hoc checks before adding a domain to the watchlist.
    Single-label domains (without a TLD) are rejected with HTTP 400 to ensure
    consistency with the domain registration validation.
    """
    # Validate that the domain has at least one dot (i.e., contains a TLD)
    if "." not in name:
        raise HTTPException(
            status_code=400,
            detail=f"Domain {name!r} must contain a TLD (e.g., 'example.com')",
        )
    
    result = await check_domain_async(name)
    return {
        "name": result.domain_name,
        "status": result.status.value,
        "expires_at": result.expires_at.isoformat() if result.expires_at else None,
        "checked_at": result.checked_at.isoformat() if result.checked_at else None,
    }


# ─── Domains ──────────────────────────────────────────────────────────────────

@app.get("/domains", tags=["domains"])
def list_domains(
    skip: int = 0,
    limit: int = 50,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
) -> dict:
    """
    Return a paginated list of all tracked domains.

    Query Parameters
    ----------------
    skip : int
        Number of records to skip (for pagination).
    limit : int
        Maximum number of records to return (max 200).
    status : str, optional
        Filter by lifecycle status (e.g. ``active``, ``expiring_soon``).
    """
    limit = min(limit, 200)
    query = db.query(Domain)
    if status:
        try:
            query = query.filter(Domain.status == DomainStatus(status))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown status {status!r}. Valid values: "
                + ", ".join(s.value for s in DomainStatus),
            )
    total = query.count()
    domains = query.offset(skip).limit(limit).all()
    return {
        "domains": [_domain_to_dict(d) for d in domains],
        "skip": skip,
        "limit": limit,
        "total": total,
    }


@app.get("/domains/{name}", tags=["domains"])
def get_domain(name: str, db: Session = Depends(get_db)) -> dict:
    """Retrieve a single domain record by fully-qualified name."""
    domain = db.query(Domain).filter(Domain.name == name).first()
    if not domain:
        raise HTTPException(status_code=404, detail=f"Domain {name!r} not found")
    return _domain_to_dict(domain)


@app.post("/domains", tags=["domains"], status_code=201)
def add_domain(req: AddDomainRequest, db: Session = Depends(get_db)) -> dict:
    """
    Add *name* to the monitoring watchlist.

    The domain is first checked against the :mod:`~app.trademark_filter`
    before being persisted.  Flagged domains are rejected with HTTP 409.
    Already-tracked domains are also rejected with HTTP 409.
    Single-label domains (e.g., 'localhost' without a TLD) are rejected
    with HTTP 400.
    """
    # Validate that the domain has at least one dot (i.e., contains a TLD)
    if "." not in req.name:
        raise HTTPException(
            status_code=400,
            detail=f"Domain {req.name!r} must contain a TLD (e.g., 'example.com')",
        )

    tm_result = _trademark_filter.check(req.name)
    if tm_result.is_flagged:
        raise HTTPException(status_code=409, detail=tm_result.reason)

    existing = db.query(Domain).filter(Domain.name == req.name).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Domain {req.name!r} is already being tracked",
        )

    tld = req.name.rsplit(".", 1)[-1]
    domain = Domain(name=req.name, tld=tld, status=DomainStatus.ACTIVE)
    db.add(domain)
    db.commit()
    db.refresh(domain)

    enqueue_domain_check(req.name)

    return {"name": req.name, "queued": True}


@app.delete("/domains/{name}", tags=["domains"])
def remove_domain(name: str, db: Session = Depends(get_db)) -> dict:
    """Remove *name* from the monitoring watchlist."""
    deleted_count = db.query(Domain).filter(Domain.name == name).delete()
    if not deleted_count:
        raise HTTPException(status_code=404, detail=f"Domain {name!r} not found")
    db.commit()
    return {"deleted": name}


@app.post("/domains/{name}/snipe", tags=["domains"])
def manual_snipe(name: str, db: Session = Depends(get_db)) -> dict:
    """Manually trigger a snipe attempt for *name*."""
    domain = db.query(Domain).filter(Domain.name == name).first()
    if not domain:
        raise HTTPException(status_code=404, detail=f"Domain {name!r} not found")
    enqueue_snipe(name)
    return {"name": name, "sniped": True}


# ─── Metrics ──────────────────────────────────────────────────────────────────

@app.get("/metrics/{name}", tags=["metrics"])
def get_metrics(name: str, db: Session = Depends(get_db)) -> dict:
    """Retrieve the latest valuation metrics for *name*."""
    domain = db.query(Domain).filter(Domain.name == name).first()
    if not domain:
        raise HTTPException(status_code=404, detail=f"Domain {name!r} not found")

    metrics = (
        db.query(DomainMetrics)
        .filter(DomainMetrics.domain_id == domain.id)
        .order_by(DomainMetrics.snapshot_at.desc())
        .first()
    )
    if not metrics:
        return {"name": name, "metrics": {}}

    return {
        "name": name,
        "metrics": {
            "domain_authority": metrics.domain_authority,
            "estimated_value_usd": metrics.estimated_value_usd,
            "backlink_count": metrics.backlink_count,
            "referring_domains": metrics.referring_domains,
            "snapshot_at": metrics.snapshot_at.isoformat() if metrics.snapshot_at else None,
        },
    }
