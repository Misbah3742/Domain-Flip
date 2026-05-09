"""
tests/test_config.py

Unit tests for the config module.
Verifies that Settings builds correct URLs and handles defaults.
"""

import pytest

from app.config import Settings


class TestSettingsDefaults:
    """Default values are sane and URLs are constructed correctly."""

    def setup_method(self):
        # Use a fresh instance without a .env file
        self.s = Settings(
            _env_file=None,
            postgres_host="localhost",
            postgres_port=5432,
            postgres_db="testdb",
            postgres_user="user",
            postgres_password="pass",
            redis_host="localhost",
            redis_port=6379,
            redis_db=0,
        )

    def test_database_url_format(self):
        url = self.s.database_url
        assert url.startswith("postgresql+psycopg2://")
        assert "user:pass@localhost:5432/testdb" in url

    def test_redis_url_format(self):
        url = self.s.redis_url
        assert url == "redis://localhost:6379/0"

    def test_monitor_defaults(self):
        assert self.s.monitor_max_workers >= 1
        assert self.s.monitor_check_interval_seconds >= 1

    def test_sniper_defaults(self):
        assert self.s.sniper_lead_time_seconds >= 0
        assert self.s.sniper_poll_interval_ms >= 1
