"""Admin panel: every account with how much it stores, and deleting accounts with all their data."""

from datetime import datetime

import streamlit as st

import file_store
import task_store
from task_files import file_size

ADMIN_URL_PATH = "admin"
SIGN_IN_METHODS = {"password": "Administrator", "oidc": "OIDC", "local": "No login"}


def sign_in_method(account):
    return SIGN_IN_METHODS.get(account["identity"].split(":", 1)[0], "Other")


def delete_account(user_id):
    """Delete the account's files, then the account with its boards, tasks and file records."""
    prefix = file_store.account_prefix(user_id)
    # Files go first: if that fails, the account is still listed and can be deleted again.
    file_store.delete_prefix(prefix)
    task_store.delete_account(user_id)
    # Catches files uploaded by a session of the account while it was being deleted.
    file_store.delete_prefix(prefix)


@st.dialog("Delete account")
def delete_account_dialog(account):
    st.markdown(
        f"This permanently deletes **{account['name']}** with its {account['boards']} boards, "
        f"{account['tasks']} tasks and {account['files']} files ({file_size(account['bytes'])}). "
        "It can't be undone."
    )
    st.caption(
        "If they sign in again, they start over with a new, empty account. Sessions they still have "
        "open do the same."
    )
    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button("Cancel"):
            st.rerun()
        if st.button("Delete account", icon=":material/delete_forever:", type="primary"):
            with st.spinner("Deleting files and tasks..."):
                delete_account(account["id"])
            st.session_state.deleted_account = account["name"]
            st.rerun()


def admin_page(admin):
    st.title("Admin panel", icon=":material/admin_panel_settings:")
    if name := st.session_state.pop("deleted_account", None):
        st.toast(f"Deleted the account of **{name}**.", icon=":material/delete_forever:")

    accounts = task_store.load_accounts()
    total_bytes = sum(a["bytes"] for a in accounts)
    with st.container(horizontal=True):
        st.metric("Accounts", len(accounts), border=True)
        st.metric("Tasks", sum(a["tasks"] for a in accounts), border=True)
        st.metric("Files", sum(a["files"] for a in accounts), border=True)
        st.metric("Storage used", file_size(total_bytes), border=True)

    st.subheader("Accounts")
    st.dataframe(
        [
            {
                "Name": a["name"],
                "Email": a["email"],
                "Sign-in": sign_in_method(a),
                "Boards": a["boards"],
                "Tasks": a["tasks"],
                "Files": a["files"],
                "Storage": a["bytes"],
                "Last seen": datetime.fromtimestamp(a["last_seen_at"]),
                "Created": datetime.fromtimestamp(a["created_at"]),
            }
            for a in accounts
        ],
        hide_index=True,
        column_config={
            "Storage": st.column_config.NumberColumn(format="bytes", help="Size of the account's files"),
            "Last seen": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm"),
            "Created": st.column_config.DatetimeColumn(format="YYYY-MM-DD"),
        },
    )

    st.subheader("Delete an account")
    # The administrator's own account can't be deleted from here.
    others = {a["id"]: a for a in accounts if a["id"] != admin["id"]}
    if not others:
        st.caption("There are no other accounts yet. People get one when they first sign in with OIDC.")
        return
    with st.container(horizontal=True, vertical_alignment="bottom"):
        selected = st.selectbox(
            "Account",
            options=list(others),
            index=None,
            format_func=lambda user_id: (
                f"{others[user_id]['name']} ({others[user_id]['email'] or sign_in_method(others[user_id])}) "
                f"· {file_size(others[user_id]['bytes'])}"
            ),
            placeholder="Choose an account",
        )
        if st.button(
            "Delete account", icon=":material/delete_forever:", disabled=selected is None, width="content"
        ):
            delete_account_dialog(others[selected])
