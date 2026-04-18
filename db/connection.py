"""SQLAlchemy engine factory for the Olist Postgres database.

The engine is created lazily on first use and cached at module level so
that every caller within a single process shares the same connection pool.
"""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine

load_dotenv()


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a process-wide SQLAlchemy engine backed by DATABASE_URL.

    Pool sizing is tuned for a small app: five persistent connections,
    plus up to five overflow, with a 30-second timeout to fail fast
    when the DB is unreachable.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in."
        )
    return create_engine(
        url,
        pool_size=5,
        max_overflow=5,
        pool_timeout=30,
        pool_pre_ping=True,
        future=True,
    )
