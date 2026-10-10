"""Login gate with two methods that work side by side: the administrator signs in with the login and
password from environment variables, and everyone else through OIDC via st.login.

Each person gets their own account: the administrator one, and one per OIDC user, created on their
first sign-in. Only the administrator can open the admin panel. Setting TASKBOARD_AUTH_DISABLED
skips authentication entirely: everyone shares one anonymous account and there's no administrator.

OIDC settings come from `[auth]` in `.streamlit/secrets.toml`, or from TASKBOARD_OIDC_* environment
variables when the app is started through `asgi_app.py`, which passes them in as secrets.
"""

import hmac
import os
import time

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

LOGIN_ENV = "TASKBOARD_LOGIN"
PASSWORD_ENV = "TASKBOARD_PASSWORD"
AUTH_DISABLED_ENV = "TASKBOARD_AUTH_DISABLED"
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"", "0", "false", "no", "off"}
FAILED_LOGIN_DELAY = 1  # seconds; slows down password guessing
# A line under the user's name sets it apart from the links below it in the sidebar.
ACCOUNT_CSS = """
.st-key-sidebar_user {
    padding-bottom: 0.5rem;
    border-bottom: 1px solid color-mix(in srgb, currentColor 20%, transparent);
}
"""
# [auth] secret key -> environment variable.
OIDC_ENV = {
    "client_id": "TASKBOARD_OIDC_CLIENT_ID",
    "client_secret": "TASKBOARD_OIDC_CLIENT_SECRET",
    "server_metadata_url": "TASKBOARD_OIDC_SERVER_METADATA_URL",
    "redirect_uri": "TASKBOARD_OIDC_REDIRECT_URI",
    "cookie_secret": "TASKBOARD_OIDC_COOKIE_SECRET",
}


def auth_disabled():
    # Anything that isn't clearly "on" keeps authentication enabled.
    return os.environ.get(AUTH_DISABLED_ENV, "").strip().lower() in TRUE_VALUES


def password_credentials():
    """The (login, password) pair from the environment, or None unless both are set."""
    login, password = os.environ.get(LOGIN_ENV, ""), os.environ.get(PASSWORD_ENV, "")
    return (login, password) if login and password else None


def oidc_env_secrets():
    """The [auth] secrets section built from TASKBOARD_OIDC_* variables, or None unless all are set."""
    values = {key: os.environ.get(env, "") for key, env in OIDC_ENV.items()}
    return values if all(values.values()) else None


def _oidc_env_problem():
    """Why the TASKBOARD_OIDC_* variables aren't in effect, or None if they're unused or working."""
    missing = [env for env in OIDC_ENV.values() if not os.environ.get(env)]
    if len(missing) == len(OIDC_ENV):
        return None
    if missing:
        return f"OIDC environment variables are incomplete, so they're ignored. Missing: {', '.join(f'`{m}`' for m in missing)}."
    try:
        secrets_client_id = (st.secrets.get("auth") or {}).get("client_id")
    except StreamlitSecretNotFoundError:
        secrets_client_id = None
    if secrets_client_id != os.environ[OIDC_ENV["client_id"]]:
        return (
            "OIDC environment variables are set but not in effect. Start the app with "
            "`streamlit run asgi_app.py` so they're passed to Streamlit's login."
        )
    return None


def oidc_providers():
    """Provider names configured under [auth] in secrets.toml; None stands for the unnamed provider."""
    try:
        auth = st.secrets.get("auth")
    except StreamlitSecretNotFoundError:
        return []
    if not auth:
        return []
    providers = [None] if "client_id" in auth else []
    providers += [name for name, section in auth.items() if hasattr(section, "get") and "client_id" in section]
    return providers


def current_account():
    """The signed-in person, or None.

    A dict with "identity" (stable across sign-ins and unique among accounts), "name", "email",
    "method" ("disabled", "password" or "oidc") and "is_admin".
    """
    if auth_disabled():
        return {"identity": "local", "name": "Anonymous", "email": None, "method": "disabled", "is_admin": False}
    credentials = password_credentials()
    login = st.session_state.get("auth_user")
    # Checked against the current login, so changing it in the environment signs the old one out.
    if credentials and login == credentials[0]:
        return {"identity": f"password:{login}", "name": login, "email": None, "method": "password", "is_admin": True}
    if oidc_providers() and st.user.get("is_logged_in", False):
        # The issuer and subject together identify a person across OIDC providers.
        return {
            "identity": f"oidc:{st.user.get('iss')}:{st.user.get('sub')}",
            "name": st.user.get("name") or st.user.get("email") or "Signed in",
            "email": st.user.get("email"),
            "method": "oidc",
            "is_admin": False,
        }
    return None


def _check_password(login, password):
    expected_login, expected_password = password_credentials()
    # Compare both in constant time and only combine at the end, so timing doesn't reveal which was wrong.
    login_ok = hmac.compare_digest(login.encode(), expected_login.encode())
    password_ok = hmac.compare_digest(password.encode(), expected_password.encode())
    return login_ok and password_ok


def _password_form():
    with st.form("login_form", border=False):
        login = st.text_input("Login", autocomplete="username")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Log in", icon=":material/login:", type="primary")
    if submitted:
        if _check_password(login, password):
            st.session_state.auth_user = login
            st.rerun()
        time.sleep(FAILED_LOGIN_DELAY)
        st.error("Invalid login or password.")


def login_page():
    with st.container(horizontal_alignment="center"):
        with st.container(border=True, width=420):
            st.subheader("Log in", anchor=False)

            disabled_value = os.environ.get(AUTH_DISABLED_ENV, "").strip()
            if disabled_value.lower() not in TRUE_VALUES | FALSE_VALUES:
                st.warning(
                    f"`{AUTH_DISABLED_ENV}` is set to an unrecognized value (`{disabled_value}`), so "
                    f"authentication stays on. Use one of: {', '.join(sorted(TRUE_VALUES))}.",
                    icon=":material/warning:",
                )
            if problem := _oidc_env_problem():
                st.warning(problem, icon=":material/warning:")
            partial = [name for name in (LOGIN_ENV, PASSWORD_ENV) if os.environ.get(name)]
            if len(partial) == 1:
                st.warning(
                    f"Only `{partial[0]}` is set. Password login needs both `{LOGIN_ENV}` and "
                    f"`{PASSWORD_ENV}`, so it's disabled.",
                    icon=":material/warning:",
                )

            providers = oidc_providers()
            for provider in providers:
                label = f"Log in with {provider.capitalize()}" if provider else "Log in"
                if st.button(label, key=f"oidc_{provider}", icon=":material/login:", type="primary"):
                    st.login(provider)
            if password_credentials():
                if providers:
                    # Users sign in with OIDC above; the password is the administrator's.
                    with st.expander("Administrator login", icon=":material/admin_panel_settings:"):
                        _password_form()
                else:
                    _password_form()
            elif not providers:
                st.error(
                    "No login method is configured, so access is blocked. Set the "
                    f"`{LOGIN_ENV}` and `{PASSWORD_ENV}` environment variables for the administrator, "
                    "and configure OIDC for everyone else with the `TASKBOARD_OIDC_*` variables (run "
                    "`asgi_app.py`) or under `[auth]` in `.streamlit/secrets.toml`. To run without login, set "
                    f"`{AUTH_DISABLED_ENV}=1`.",
                    icon=":material/block:",
                )


def require_login(url_paths=()):
    """Show the login page and stop the script unless someone is signed in. Returns their account
    (see current_account).

    The login page is also served at each of `url_paths`, so opening (or logging out on) one of the
    app's pages shows the login form instead of "Page not found", and logging in stays on that page.
    """
    account = current_account()
    if not account:
        pages = [st.Page(login_page, title="Log in", icon=":material/lock:", default=True)]
        pages += [
            st.Page(login_page, title="Log in", icon=":material/lock:", url_path=path)
            for path in url_paths
        ]
        st.navigation(pages, position="hidden").run()
        st.stop()
    return account


def account_sidebar(account, admin_page=None):
    """The signed-in user, set apart from the links below it: `admin_page` (if given) and log out."""
    if account["method"] == "disabled":
        st.caption(
            f":material/lock_open: Authentication is disabled (`{AUTH_DISABLED_ENV}`).",
            help="Anyone who can reach this app can use it. Unset the variable to require login.",
        )
        return
    with st.container(key="sidebar_account", gap="xsmall"):
        role = " :small[:gray[Administrator]]" if account["is_admin"] else ""
        with st.container(key="sidebar_user"):
            st.markdown(f":material/account_circle: **{account['name']}**{role}")
        if admin_page:
            st.page_link(admin_page, width="stretch")
        if st.button("Log out", icon=":material/logout:", type="tertiary"):
            if account["method"] == "oidc":
                st.logout()
            else:
                st.session_state.pop("auth_user", None)
                st.rerun()
