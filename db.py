"""PostgreSQL connection configured from environment variables.

Either set TASKBOARD_DATABASE_URL to a full SQLAlchemy URL, or set TASKBOARD_DB_HOST plus the optional
TASKBOARD_DB_* parts (see DB_ENV). The URL wins when both are set. Without either, the connection
falls back to `[connections.sql]` in `.streamlit/secrets.toml`, if present.

The defaults match the database service in docker-compose.yml.
"""

import os

import sqlalchemy as sa
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

DATABASE_URL_ENV = "TASKBOARD_DATABASE_URL"
DB_ENV = {
    "host": "TASKBOARD_DB_HOST",
    "port": "TASKBOARD_DB_PORT",
    "database": "TASKBOARD_DB_NAME",
    "username": "TASKBOARD_DB_USER",
    "password": "TASKBOARD_DB_PASSWORD",
}
DB_DEFAULTS = {"port": "5432", "database": "taskboard", "username": "taskboard"}
DRIVER = "postgresql+psycopg"
STATUS_TTL = 30  # seconds between connectivity checks


def database_url():
    """The SQLAlchemy URL from the environment, or None unless it's configured there."""
    if url := os.environ.get(DATABASE_URL_ENV, "").strip():
        return sa.make_url(url)
    if not os.environ.get(DB_ENV["host"]):
        return None
    parts = {key: os.environ.get(env) or DB_DEFAULTS.get(key) for key, env in DB_ENV.items()}
    # URL.create escapes special characters in the password, unlike string formatting.
    return sa.URL.create(DRIVER, **{**parts, "port": int(parts["port"])})


def _secrets_configured():
    try:
        return "sql" in (st.secrets.get("connections") or {})
    except StreamlitSecretNotFoundError:
        return False


def db_configured():
    return database_url() is not None or _secrets_configured()


def connection():
    """The shared SQL connection; st.connection caches it across reruns and sessions."""
    url = database_url()
    # pool_pre_ping replaces connections dropped by a database restart instead of failing on them.
    if url is None:
        return st.connection("sql", pool_pre_ping=True)
    return st.connection("sql", url=url.render_as_string(hide_password=False), pool_pre_ping=True)


@st.cache_data(ttl=STATUS_TTL, show_spinner=False)
def connection_error():
    """Why the database can't be reached, or None if it can. Cached so reruns don't ping it."""
    try:
        with connection().session as session:
            session.execute(sa.text("SELECT 1"))
    except Exception as e:  # noqa: BLE001 - any failure here means "not reachable"
        return f"{type(e).__name__}: {e}".splitlines()[0]
    return None


def database_sidebar():
    """Database status for the sidebar; nothing when no database is configured."""
    if not db_configured():
        return
    if error := connection_error():
        st.caption(":material/database: :red[Database unreachable]", help=error)
    else:
        st.caption(":material/database: Database connected")
