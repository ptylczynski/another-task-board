"""Entry point that also accepts OIDC settings from environment variables and serves the MCP server.

Run with `streamlit run asgi_app.py`. The TASKBOARD_OIDC_* variables (see auth_gate.OIDC_ENV) are
passed to Streamlit as the [auth] secrets section, overriding [auth] in .streamlit/secrets.toml.
They can also be set in a .env file next to this one. The MCP server (see mcp_server) is served at
/mcp, next to the app.
"""

from contextlib import asynccontextmanager
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from starlette.routing import Route

from auth_gate import oidc_env_secrets
from mcp_server import MCP_PATH, mcp, mcp_app

# Loaded here as well as in streamlit_app.py, because the OIDC variables are read before it runs.
load_dotenv(Path(__file__).with_name(".env"))


@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


oidc = oidc_env_secrets()
app = st.App(
    "streamlit_app.py",
    secrets={"auth": oidc} if oidc else None,
    lifespan=lifespan,
    # The MCP app is passed as is, so it sees the request's full path and applies its own middleware.
    routes=[Route(MCP_PATH, mcp_app())],
)
