"""Accès à PostgreSQL.

Une connexion courte par requête : simple et robuste face aux coupures
(une connexion cassée n'est jamais réutilisée).
"""

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import CONFIG


@contextmanager
def connexion() -> Iterator[psycopg.Connection]:
    """Connexion en mode transaction : commit si tout va bien, rollback sinon."""
    with psycopg.connect(CONFIG.database_url, row_factory=dict_row, connect_timeout=5) as conn:
        yield conn
