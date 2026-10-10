import re
import time
import uuid
import zlib
from functools import partial
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from auth_gate import account_sidebar, require_login
from board_icons import EMOJIS, emoji_label, random_emoji
from db import database_sidebar
from task_dblclick import task_dblclick_listener

# Variables already set in the environment take precedence over .env.
load_dotenv(Path(__file__).with_name(".env"))

HIDE_AFTER = 3  # seconds a completed task stays visible at the top
UNDO_FOR = 6  # seconds the undo toast stays visible after the task disappears
# Groups and tags use separate hue families so a tag never blends into its group:
# groups get calm cool tints (large areas), tags get warm Streamlit badge colors (small accents).
GROUP_COLORS = {
    "blue": "59, 130, 246",
    "green": "34, 197, 94",
    "violet": "139, 92, 246",
    "teal": "20, 184, 166",
    "slate": "100, 116, 139",
}
TAG_COLORS = ["orange", "red", "yellow", "gray"]
DEFAULT_BOARD_ID = "default"
DEFAULT_BOARD_ICON = "📋"

st.set_page_config(page_title="Task board", page_icon=":material/checklist:")

st.session_state.setdefault("tasks", [])
st.session_state.setdefault(
    "boards",
    [{"id": DEFAULT_BOARD_ID, "name": "Default", "icon": DEFAULT_BOARD_ICON, "url_path": "default"}],
)
st.session_state.setdefault("group_colors", {})  # lowercased group name -> color
# None, "add", "new_board", or the id of the task being edited.
st.session_state.setdefault("open_dialog", None)


def board_tasks(board_id):
    return [t for t in st.session_state.tasks if t.get("board_id", DEFAULT_BOARD_ID) == board_id]


def open_task_count(board_id):
    return sum(t["done_at"] is None for t in board_tasks(board_id))


def done_age(task):
    return None if task["done_at"] is None else time.time() - task["done_at"]


def has_timers(board_id):
    return any(
        (age := done_age(task)) is not None and age < HIDE_AFTER + UNDO_FOR
        for task in board_tasks(board_id)
    )


def toggle_done(task):
    task["done_at"] = time.time() if st.session_state[f"done_{task['id']}"] else None


def undo_done(task):
    task["done_at"] = None
    st.session_state.pop(f"done_{task['id']}", None)


def all_tags():
    # Tags are shared across boards so the same labels can be reused everywhere.
    tags = {}
    for task in st.session_state.tasks:
        for tag in task.get("tags", []):
            tags.setdefault(tag.lower(), tag)
    return sorted(tags.values(), key=str.lower)


def board_groups(board_id):
    groups = {}
    for task in board_tasks(board_id):
        if group := task.get("group"):
            groups.setdefault(group.lower(), group)
    return sorted(groups.values(), key=str.lower)


def group_color(group):
    # Assigned once per group, picking the least-used color so groups stay distinct.
    colors = st.session_state.group_colors
    if colors.get(group.lower()) not in GROUP_COLORS:
        used = list(colors.values())
        colors[group.lower()] = min(GROUP_COLORS, key=used.count)
    return colors[group.lower()]


def tag_badge(tag):
    # crc32 keeps a tag's color stable across reruns and server restarts.
    color = TAG_COLORS[zlib.crc32(tag.lower().encode()) % len(TAG_COLORS)]
    return f":{color}-badge[{tag}]"


def clean_tags(tags):
    cleaned = {}
    for tag in tags:
        # Brackets would break the badge Markdown.
        tag = tag.replace("[", "").replace("]", "").strip()
        if tag:
            cleaned.setdefault(tag.lower(), tag)
    return list(cleaned.values())


def board_url_path(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "board"
    taken = {b["url_path"] for b in st.session_state.boards}
    candidate, n = slug, 2
    while candidate in taken:
        candidate, n = f"{slug}-{n}", n + 1
    return candidate


def close_dialog():
    st.session_state.open_dialog = None


def task_form(board_id, task=None):
    """Render the task fields; return them once submitted and valid, else None."""
    with st.form(f"task_form_{task['id'] if task else 'new'}", border=False):
        name = st.text_input(
            "Name", value=task["name"] if task else "", placeholder="What needs to be done?"
        )
        description = st.text_area(
            "Description (optional)", value=task.get("description", "") if task else ""
        )
        tags = st.multiselect(
            "Tags (optional)",
            options=all_tags(),
            default=task.get("tags", []) if task else [],
            accept_new_options=True,
            placeholder="Choose or type a new tag",
        )
        groups = board_groups(board_id)
        current_group = task.get("group") if task else None
        group = st.selectbox(
            "Group (optional)",
            options=[None, *groups],
            index=groups.index(current_group) + 1 if current_group in groups else 0,
            format_func=lambda g: "No group" if g is None else g,
            accept_new_options=True,
        )
        due_date = st.date_input(
            "Due date (optional)", value=task["due_date"] if task else None, format="YYYY-MM-DD"
        )
        submitted = st.form_submit_button(
            "Save" if task else "Add",
            icon=":material/check:" if task else ":material/add:",
            type="primary",
        )

    if not submitted:
        return None
    name = name.strip()
    if not name:
        st.error("Task name is required.")
        return None
    return {
        "name": name,
        "description": description.strip(),
        "tags": clean_tags(tags),
        "group": (group or "").strip() or None,
        "due_date": due_date,
    }


# Dialogs are opened via session state so they survive full reruns triggered by the task timer.
@st.dialog("Add task", on_dismiss=close_dialog)
def add_task_dialog(board_id):
    if fields := task_form(board_id):
        st.session_state.tasks.append(
            {"id": uuid.uuid4().hex, "board_id": board_id, **fields, "done_at": None}
        )
        close_dialog()
        st.rerun()


@st.dialog("Edit task", on_dismiss=close_dialog)
def edit_task_dialog(task):
    if fields := task_form(task.get("board_id", DEFAULT_BOARD_ID), task):
        task.update(fields)
        close_dialog()
        st.rerun()


@st.dialog("New board", on_dismiss=close_dialog)
def new_board_dialog():
    with st.form("new_board_form", border=False):
        name = st.text_input("Name", placeholder="e.g. Work, Home, Side project")
        icon = st.selectbox(
            "Icon (optional)",
            options=EMOJIS,
            index=None,
            format_func=emoji_label,
            filter_mode="contains",
            placeholder="Search by name or pick from the list",
            help="Leave empty to get a random icon.",
        )
        submitted = st.form_submit_button("Create", icon=":material/add:", type="primary")

    if not submitted:
        return
    name = name.strip()
    if not name:
        st.error("Board name is required.")
        return
    if existing := next((b for b in st.session_state.boards if b["name"].lower() == name.lower()), None):
        st.error(f"A board named **{existing['name']}** already exists.")
        return
    board = {
        "id": uuid.uuid4().hex,
        "name": name,
        "icon": icon or random_emoji(),
        "url_path": board_url_path(name),
    }
    st.session_state.boards.append(board)
    # The new board's page only exists from the next run on, so switch to it there.
    st.session_state.switch_to_board = board["id"]
    close_dialog()
    st.rerun()


def open_edit_dialog(task_id):
    if any(t["id"] == task_id for t in st.session_state.tasks):
        st.session_state.open_dialog = task_id


def task_row(task):
    is_done = task["done_at"] is not None
    with st.container(
        border=True, horizontal=True, vertical_alignment="center", key=f"task_row_{task['id']}"
    ):
        with st.container(horizontal=True, gap="xsmall"):
            # The name is rendered separately (not as the checkbox label) so clicking or
            # double-clicking it doesn't toggle the task.
            st.checkbox(
                task["name"],
                value=is_done,
                key=f"done_{task['id']}",
                on_change=toggle_done,
                args=(task,),
                label_visibility="collapsed",
            )
            with st.container(gap="xxsmall"):
                st.markdown(f"~~{task['name']}~~" if is_done else task["name"])
                if task.get("description"):
                    st.caption(task["description"])
                if task.get("tags"):
                    st.markdown(" ".join(tag_badge(tag) for tag in task["tags"]))
        if task["due_date"]:
            st.caption(f":material/event: {task['due_date']:%Y-%m-%d}", width="content")
        if st.button(":material/edit:", key=f"edit_{task['id']}", help="Edit task", type="tertiary"):
            st.session_state.open_dialog = task["id"]
            st.rerun()


def board_page(board_id):
    board = next(b for b in st.session_state.boards if b["id"] == board_id)

    with st.container(horizontal=True, vertical_alignment="center"):
        st.title(board["name"], icon=board.get("icon", DEFAULT_BOARD_ICON))
        st.space("stretch")
        if st.button("Add task", icon=":material/add:", type="primary"):
            st.session_state.open_dialog = "add"

    dialog = st.session_state.open_dialog
    if dialog == "add":
        add_task_dialog(board_id)
    elif dialog == "new_board":
        new_board_dialog()
    elif dialog:
        edited = next((t for t in st.session_state.tasks if t["id"] == dialog), None)
        if edited:
            edit_task_dialog(edited)
        else:
            close_dialog()

    timers_running = has_timers(board_id)
    open_count = open_task_count(board_id)

    @st.fragment(run_every=1 if timers_running else None)
    def task_list():
        tasks = board_tasks(board_id)
        visible = [t for t in tasks if (age := done_age(t)) is None or age < HIDE_AFTER]
        # Completed tasks go to the top (most recent first), open tasks keep their creation order.
        done = sorted((t for t in visible if t["done_at"] is not None), key=lambda t: -t["done_at"])
        open_ = [t for t in visible if t["done_at"] is None]

        if not visible:
            st.info("No tasks yet. Click **Add task** to create one.", icon=":material/inbox:")

        # Ungrouped tasks first, then one box per group in order of first appearance.
        grouped = {}
        for task in done + open_:
            grouped.setdefault(task["group"].lower() if task.get("group") else None, []).append(task)

        for task in grouped.pop(None, []):
            task_row(task)
        for group_id, group_tasks in grouped.items():
            group = group_tasks[0]["group"]
            color = group_color(group)
            # The color in the key lets the CSS tint the box; the crc keeps keys unique per group.
            with st.container(border=True, key=f"group_{color}_{zlib.crc32(group_id.encode()):08x}"):
                icon = f':color[:material/folder:]{{foreground="rgb({GROUP_COLORS[color]})"}}'
                st.markdown(f"{icon} **{group}** :small[:gray[{len(group_tasks)}]]")
                for task in group_tasks:
                    task_row(task)

        hidden_recently = [
            t for t in tasks
            if (age := done_age(t)) is not None and HIDE_AFTER <= age < HIDE_AFTER + UNDO_FOR
        ]
        if hidden_recently:
            with st.container(key="undo_toasts"):
                for task in hidden_recently:
                    with st.container(
                        border=True,
                        horizontal=True,
                        vertical_alignment="center",
                        key=f"undo_toast_{task['id']}",
                    ):
                        st.markdown(f":material/check_circle: Completed **{task['name']}**")
                        st.space("stretch")
                        st.button("Undo", key=f"undo_{task['id']}", on_click=undo_done, args=(task,))

        # Rerun the whole app to start or stop the 1-second refresh when the pending timers
        # change, and to update the sidebar count when a task is ticked or restored.
        if has_timers(board_id) != timers_running or open_task_count(board_id) != open_count:
            st.rerun()

    task_list()
    task_dblclick_listener(open_edit_dialog)


page_background = "#0e1117" if st.context.theme.type == "dark" else "#ffffff"
st.html(f"""
<style>
.st-key-undo_toasts {{
    position: fixed;
    bottom: 1.5rem;
    right: 1.5rem;
    width: 22rem;
    max-width: calc(100vw - 3rem);
    z-index: 1000;
}}
[class*="st-key-undo_toast_"] {{
    background-color: {page_background};
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
}}
/* Solid task rows so only the group box itself is tinted. */
[class*="st-key-task_row_"] {{
    background-color: {page_background};
}}
{"".join(
    f"""[class*="st-key-group_{name}_"] {{ background-color: rgba({rgb}, 0.1); border-color: rgba({rgb}, 0.4); }}\n"""
    for name, rgb in GROUP_COLORS.items()
)}
</style>
""")

auth_mode = require_login(
    [b["url_path"] for b in st.session_state.boards if b["id"] != DEFAULT_BOARD_ID]
)

# One page per board. The built-in menu is hidden (it disappears entirely with a single page),
# so the sidebar below renders the board list itself.
pages = {
    board["id"]: st.Page(
        partial(board_page, board["id"]),
        title=board["name"],
        icon=board.get("icon", DEFAULT_BOARD_ICON),
        url_path=board["url_path"],
        default=board["id"] == DEFAULT_BOARD_ID,
    )
    for board in st.session_state.boards
}
current_page = st.navigation(list(pages.values()), position="hidden")

with st.sidebar:
    st.subheader("Boards")
    for board in st.session_state.boards:
        st.page_link(
            pages[board["id"]],
            label=f"{board['name']} :small[:gray[{open_task_count(board['id'])}]]",
            width="stretch",
        )
    # Runs before the page, so the board page opens the dialog in this same run.
    if st.button("New board", icon=":material/add:", type="tertiary"):
        st.session_state.open_dialog = "new_board"
    st.divider()
    account_sidebar(auth_mode)
    database_sidebar()

if switch_to := st.session_state.pop("switch_to_board", None):
    st.switch_page(pages[switch_to])

current_page.run()
