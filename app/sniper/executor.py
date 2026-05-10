"""
sniper.executor – Alias module so RQ can locate the job function.

RQ resolves job functions by their fully-qualified module path.
Exporting :func:`attempt_registration` here keeps the import path stable
even if the internal layout changes.
"""

from app.sniper import attempt_registration  # noqa: F401
