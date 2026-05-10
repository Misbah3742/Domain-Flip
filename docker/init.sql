-- docker/init.sql
-- Executed once when the PostgreSQL container is first created.
-- SQLAlchemy's init_db() will create the tables; this file handles
-- any database-level setup that must precede the ORM bootstrap.

-- Ensure the target database exists (in case the container image creates it
-- from POSTGRES_DB env var, this is a no-op guard).
SELECT 1;
