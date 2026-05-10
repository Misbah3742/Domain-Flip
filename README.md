# Domain-Flip

A Python-based domain monitoring and "sniping" system designed to identify
expiring high-value domains for resale.

---

## Architecture Overview

```
domain-flip/
├── app/
│   ├── config/          # Centralised settings (pydantic-settings)
│   ├── database/        # SQLAlchemy ORM models + session factory
│   ├── queue/           # Redis / RQ queue helpers
│   ├── zone_parser/     # ICANN zone-file parser + WhoisXML client
│   ├── monitor/         # Multi-threaded domain status checker
│   ├── sniper/          # Dynadot / Namejet registration executor
│   ├── trademark_filter/# Brand-name safety filter (UDRP guard)
│   └── api/             # FastAPI REST interface (inspection / admin)
├── tests/               # Pytest test suite
├── docker/              # PostgreSQL init SQL
├── docker-compose.yml   # Full stack definition
├── Dockerfile           # Python application image
├── requirements.txt     # Python dependencies
└── .env.example         # Environment variable template
```

### Services (docker-compose)

| Service       | Purpose                                                    |
|---------------|------------------------------------------------------------|
| `postgres`    | Stores domain metadata, metrics, and snipe attempt logs    |
| `redis`       | Job queue backend (via RQ)                                 |
| `zone-parser` | Discovers expiring domains from ICANN zone files / WhoisXML|
| `monitor`     | Multi-threaded checker; tracks Redemption / Pending Delete |
| `sniper`      | Executes Register calls the millisecond a domain drops     |
| `api`         | FastAPI dashboard / REST API for inspection and management |

---

## Modules

### `app/config`
Pydantic-settings model loaded from environment variables / `.env` file.
All other modules import `settings` from here.

### `app/database`
SQLAlchemy ORM models:
- **`Domain`** - core record (name, TLD, expiry date, lifecycle status, trademark flag)
- **`DomainMetrics`** - SEO / valuation snapshots (Domain Authority, estimated value, backlinks)
- **`SnipeAttempt`** - audit log of every registration attempt

### `app/queue`
RQ-backed queue management:
- `zone_parser_q` - zone-file parsing jobs
- `monitor_q` - per-domain WHOIS check jobs
- `sniper_q` - high-priority registration jobs

### `app/zone_parser`
- **`ZoneFileParser`** - reads gzipped ICANN zone files and yields domain names found in NS records
- **`WhoisXMLClient`** - thin synchronous wrapper around the WhoisXML Domain Research API

### `app/monitor`
- **`RateLimiter`** - thread-safe token-bucket algorithm to avoid IP bans
- **`check_domain()`** - RQ job: performs WHOIS lookup, updates DB status, enqueues snipe if needed
- **`DomainChecker`** - thread-pool manager that continuously drains the monitor queue

### `app/sniper`
- **`RegistrarClient`** - abstract base class for registrar adapters
- **`DynadotClient`** - Dynadot REST API integration (register command)
- **`NamejetClient`** - Namejet back-order API integration
- **`attempt_registration()`** - RQ job: polls and retries registration until success or failure

### `app/trademark_filter`
- **`TrademarkFilter`** - checks domain SLDs against a curated set of trademarked terms using a compiled regex
- Supports `strict_mode` (substring match for cybersquatting patterns) and `extra_terms` extensibility
- Prevents UDRP legal risk by flagging domains before they are queued for sniping

### `app/api`
FastAPI application exposing:
- `GET /health` - liveness probe
- `GET /domains` - paginated domain list
- `POST /domains` - add a domain (trademark-checked first)
- `POST /domains/{name}/snipe` - manual snipe trigger
- `GET /metrics/{name}` - valuation metrics

---

## Quick Start

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in real API keys and passwords
```

### 2. Launch the full stack

```bash
docker compose up --build
```

### 3. Access the API

```
http://localhost:8000/docs
```

---

## Running Tests

```bash
pip install -r requirements.txt pytest
pytest tests/ -v
```

---

## Environment Variables

| Variable                         | Default      | Description                                   |
|----------------------------------|--------------|-----------------------------------------------|
| POSTGRES_HOST                    | postgres     | PostgreSQL hostname                           |
| POSTGRES_DB                      | domainflip   | Database name                                 |
| POSTGRES_USER                    | domainflip   | Database user                                 |
| POSTGRES_PASSWORD                | changeme     | Database password                             |
| REDIS_HOST                       | redis        | Redis hostname                                |
| WHOISXML_API_KEY                 | -            | WhoisXML API key                              |
| DYNADOT_API_KEY                  | -            | Dynadot API key                               |
| NAMEJET_API_KEY                  | -            | Namejet API key                               |
| NAMEJET_API_SECRET               | -            | Namejet API secret                            |
| MONITOR_CHECK_INTERVAL_SECONDS   | 30           | Seconds between WHOIS re-checks               |
| MONITOR_MAX_WORKERS              | 10           | Concurrent checker threads                    |
| SNIPER_LEAD_TIME_SECONDS         | 120          | Seconds before predicted drop to start polling|
| SNIPER_POLL_INTERVAL_MS          | 500          | Polling interval during final countdown (ms)  |

---

## Legal Notice

This tool includes a Trademark Filter to flag domains containing registered
brand names. Always review flagged results before attempting registration.
Registering trademarked domains may violate ICANN's Uniform Domain-Name
Dispute-Resolution Policy (UDRP) and applicable trademark laws.
