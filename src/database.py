"""Thin wrapper around psycopg2 connections."""

from contextlib import contextmanager

import psycopg2

from .settings import settings


@contextmanager
def get_db_connection():
    """Yield a psycopg2 connection; closes automatically on exit."""
    conn = psycopg2.connect(settings.database_url)
    try:
        yield conn
    finally:
        conn.close()
