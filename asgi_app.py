"""Entry point that also accepts OIDC settings from environment variables.

Run with `streamlit run asgi_app.py`. The TASKBOARD_OIDC_* variables (see auth_gate.OIDC_ENV) are
passed to Streamlit as the [auth] secrets section, overriding [auth] in .streamlit/secrets.toml.
They can also be set in a .env file next to this one.
"""

from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from auth_gate import oidc_env_secrets

# Loaded here as well as in streamlit_app.py, because the OIDC variables are read before it runs.
load_dotenv(Path(__file__).with_name(".env"))

oidc = oidc_env_secrets()
app = st.App("streamlit_app.py", secrets={"auth": oidc} if oidc else None)
