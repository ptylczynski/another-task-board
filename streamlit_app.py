import re
import time
import uuid
import zlib
from functools import partial
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

import file_store
import task_store
from admin_panel import ADMIN_URL_PATH, admin_page
from auth_gate import ACCOUNT_CSS, account_sidebar, require_login
from board_icons import EMOJIS, emoji_label, random_emoji
from db import connection_error, database_sidebar, db_configured
from file_store import file_storage_sidebar, s3_configured
from file_store import connection_error as file_storage_error
from task_dblclick import task_dblclick_listener
from task_dnd import task_drag_and_drop
from task_files import (
    TREE_CSS,
    attach_files_button,
    attach_files_form,
    file_details,
    files_tree,
    find_file,
    stored_files,
)

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
DEFAULT_BOARD_ICON = task_store.DEFAULT_BOARD_ICON

st.set_page_config(page_title="Task board", page_icon=":material/checklist:")

# The signed-in account and its boards, tasks and group colors are loaded from the database on every
# full run (see below), so changes made in other sessions show up too. Changes are written through to
# the database.
# None, "add", "new_board", the id of the task being edited, ("board", board id), ("attach", task id),
# or ("file", task id, file id).
st.session_state.setdefault("open_dialog", None)


def find_task(task_id):
    return next((t for t in st.session_state.tasks if t["id"] == task_id), None)


def board_tasks(board_id):
    return [t for t in st.session_state.tasks if t["board_id"] == board_id]


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
    task_store.set_done(task["id"], task["done_at"])


def undo_done(task):
    task["done_at"] = None
    task_store.set_done(task["id"], None)
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


def group_key(task):
    return task["group"].lower() if task.get("group") else None


def group_dom_id(key):
    # crc32 keeps the id stable and safe to use in a widget key.
    return f"{zlib.crc32(key.encode()):08x}"


def board_items(board_id):
    """The board's tasks in order, as a list of (lowercased group name, tasks) items: an ungrouped task
    is an item of its own (None, [task]), a group is one item placed where its first task is."""
    items, groups = [], {}
    for task in board_tasks(board_id):
        if (key := group_key(task)) is None:
            items.append((None, [task]))
        elif key in groups:
            groups[key].append(task)
        else:
            groups[key] = [task]
            items.append((key, groups[key]))
    return items


def save_items(*boards):
    """Store the order of each (board id, items) pair, giving each task that board and group."""
    order = []
    for board_id, items in boards:
        for _, tasks in items:
            for task in tasks:
                task["board_id"] = board_id
                order.append(task)
    task_store.save_order(order)


def find_item(items, ref):
    """The index of the item a dropped {"kind": "task" or "group", "id": ...} refers to, else the end."""
    for i, (key, tasks) in enumerate(items):
        if ref and (
            (ref["kind"] == "group" and key and group_dom_id(key) == ref["id"])
            or (ref["kind"] == "task" and key is None and tasks and tasks[0]["id"] == ref["id"])
        ):
            return i
    return len(items)


def remove_task(items, task):
    """Take the task out of its item. An emptied item stays as a placeholder; it stores nothing."""
    for _, tasks in items:
        if task in tasks:
            tasks.remove(task)


def move_task(task, group_id, before):
    items = board_items(task["board_id"])
    group = next((tasks for key, tasks in items if key and group_dom_id(key) == group_id), None)
    remove_task(items, task)
    if group is not None:
        # Adopt the group's spelling, as groups are matched case-insensitively.
        task["group"] = group[0]["group"] if group else task["group"]
        index = next((i for i, t in enumerate(group) if before and t["id"] == before["id"]), len(group))
        group.insert(index, task)
    else:
        task["group"] = None
        items.insert(find_item(items, before), (None, [task]))
    save_items((task["board_id"], items))


def move_group(board_id, key, before):
    items = board_items(board_id)
    item = next(item for item in items if item[0] == key)
    items.remove(item)
    items.insert(find_item(items, before), item)
    save_items((board_id, items))


def move_to_board(tasks, board_id):
    """Move the tasks to the end of their group on another board, or of that board if they have none."""
    source_id = tasks[0]["board_id"]
    source, target = board_items(source_id), board_items(board_id)
    for task in tasks:
        remove_task(source, task)
        key = group_key(task)
        if group := next((t for k, t in target if key and k == key), None):
            group.append(task)
        else:
            target.append((key, [task]))
    save_items((source_id, source), (board_id, target))


def handle_drop(board_id, drop):
    """Store where a task or group dragged on the board was dropped (see task_drag_and_drop)."""
    if drop["board"] == board_id or (
        drop["board"] and drop["board"] not in {b["id"] for b in st.session_state.boards}
    ):
        return
    if drop["kind"] == "task":
        task = find_task(drop["id"])
        if not task or task["board_id"] != board_id:
            return
        if drop["board"]:
            move_to_board([task], drop["board"])
        else:
            move_task(task, drop["group"], drop["before"])
    elif drop["kind"] == "group":
        items = board_items(board_id)
        key = next((k for k, _ in items if k and group_dom_id(k) == drop["id"]), None)
        if not key:
            return
        if drop["board"]:
            move_to_board(next(tasks for k, tasks in items if k == key), drop["board"])
        else:
            move_group(board_id, key, drop["before"])


def group_color(group):
    # Assigned once per group, picking the least-used color so groups stay distinct.
    colors = st.session_state.group_colors
    if colors.get(group.lower()) not in GROUP_COLORS:
        used = list(colors.values())
        colors[group.lower()] = min(GROUP_COLORS, key=used.count)
        task_store.save_group_color(st.session_state.account["id"], group.lower(), colors[group.lower()])
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


# The open_dialog values that refer to a board rather than a task, e.g. ("board", board id).
BOARD_DIALOG = "board"


def board_url_path(name, board_id=None):
    """A URL path for the board name that no other board of the account uses.

    Pass the board's id when renaming it, so it can keep its own path if the name still fits it.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "board"
    # The admin panel's path is taken for everyone, so no board can hide it.
    taken = {b["url_path"] for b in st.session_state.boards if b["id"] != board_id} | {ADMIN_URL_PATH}
    candidate, n = slug, 2
    while candidate in taken:
        candidate, n = f"{slug}-{n}", n + 1
    return candidate


def board_name_taken(name, board_id=None):
    """The other board of the account with this name (case-insensitively), if any."""
    return next(
        (
            b
            for b in st.session_state.boards
            if b["id"] != board_id and b["name"].lower() == name.lower()
        ),
        None,
    )


def board_form(board=None):
    """Render the board fields; return them once submitted and valid, else None."""
    with st.form(f"board_form_{board['id'] if board else 'new'}", border=False):
        name = st.text_input(
            "Name", value=board["name"] if board else "", placeholder="e.g. Work, Home, Side project"
        )
        current_icon = board["icon"] if board else None
        icon = st.selectbox(
            "Icon" if board else "Icon (optional)",
            options=EMOJIS,
            # A board may carry an icon that isn't on the list (e.g. an older default); then nothing is
            # preselected and leaving it empty keeps that icon.
            index=EMOJIS.index(current_icon) if current_icon in EMOJIS else None,
            format_func=emoji_label,
            filter_mode="contains",
            placeholder="Search by name or pick from the list",
            help=(
                "Leave empty to keep the current icon." if board else "Leave empty to get a random icon."
            ),
        )
        submitted = st.form_submit_button(
            "Save" if board else "Create",
            icon=":material/check:" if board else ":material/add:",
            type="primary",
        )

    if not submitted:
        return None
    name = name.strip()
    if not name:
        st.error("Board name is required.")
        return None
    if existing := board_name_taken(name, board["id"] if board else None):
        st.error(f"A board named **{existing['name']}** already exists.")
        return None
    return {"name": name, "icon": icon}


def close_dialog():
    st.session_state.open_dialog = None
    # A pending "delete this board?" question shouldn't outlive its dialog.
    st.session_state.pop("confirm_delete_board", None)


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
        # Existing tasks manage their files from the task card instead.
        uploads = None if task else st.file_uploader("Files (optional)", accept_multiple_files=True)
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
    fields = {
        "name": name,
        "description": description.strip(),
        "tags": clean_tags(tags),
        "group": (group or "").strip() or None,
        "due_date": due_date,
    }
    if not task:
        fields["uploads"] = uploads
    return fields


# Dialogs are opened via session state so they survive full reruns triggered by the task timer.
@st.dialog("Add task", on_dismiss=close_dialog)
def add_task_dialog(board_id):
    if fields := task_form(board_id):
        uploads = fields.pop("uploads")
        task = {
            "id": uuid.uuid4().hex,
            "user_id": st.session_state.account["id"],
            "board_id": board_id,
            **fields,
            "done_at": None,
        }
        task["files"] = stored_files(task, uploads)
        task_store.insert_task(task)
        st.session_state.tasks.append(task)
        close_dialog()
        st.rerun()


@st.dialog("Edit task", on_dismiss=close_dialog)
def edit_task_dialog(task):
    if fields := task_form(task["board_id"], task):
        task_store.update_task(task["id"], fields)
        task.update(fields)
        close_dialog()
        st.rerun()


@st.dialog("Attach files", on_dismiss=close_dialog)
def attach_files_dialog(task):
    st.caption(task["name"])
    if attach_files_form(task):
        close_dialog()
        st.rerun()


@st.dialog("Attachment", width="medium", on_dismiss=close_dialog)
def file_dialog(task, file):
    if file_details(task, file):
        close_dialog()
        st.rerun()


@st.dialog("New board", on_dismiss=close_dialog)
def new_board_dialog():
    if fields := board_form():
        board = {
            "id": uuid.uuid4().hex,
            "name": fields["name"],
            "icon": fields["icon"] or random_emoji(),
            "url_path": board_url_path(fields["name"]),
        }
        task_store.insert_board(st.session_state.account["id"], board)
        st.session_state.boards.append(board)
        # The new board's page only exists from the next run on, so switch to it there.
        st.session_state.switch_to_board = board["id"]
        close_dialog()
        st.rerun()


def delete_board(board):
    """Delete the board's files, then the board with its tasks and file records."""
    user_id = st.session_state.account["id"]
    # As stored, not as loaded: another session may have added tasks since.
    prefixes = [
        file_store.task_prefix(user_id, task_id) for task_id in task_store.board_task_ids(board["id"])
    ]
    # Files go first: if that fails, the board is still there and can be deleted again.
    for prefix in prefixes:
        file_store.delete_prefix(prefix)
    task_store.delete_board(board["id"])
    # Catches files uploaded to the board by another session while it was being deleted.
    for prefix in prefixes:
        file_store.delete_prefix(prefix)


def _set_confirm_delete(value):
    st.session_state.confirm_delete_board = value


@st.dialog("Edit board", on_dismiss=close_dialog)
def edit_board_dialog(board):
    if fields := board_form(board):
        icon = fields["icon"] or board["icon"]
        # The default board is served at the app's root, whatever its path; the others follow their name.
        url_path = board["url_path"]
        if not board["is_default"] and fields["name"] != board["name"]:
            url_path = board_url_path(fields["name"], board["id"])
        task_store.update_board(board["id"], fields["name"], icon, url_path)
        if url_path != board["url_path"]:
            # The browser is still at the old path; the page at the new one exists from the next run on.
            st.session_state.switch_to_board = board["id"]
        board.update(name=fields["name"], icon=icon, url_path=url_path)
        close_dialog()
        st.rerun()

    st.divider()
    if board["is_default"]:
        st.caption(
            "This is your default board, the one you land on after signing in. It can't be deleted."
        )
        return
    tasks = board_tasks(board["id"])
    files = sum(len(t.get("files", [])) for t in tasks)
    with st.container(horizontal=True, vertical_alignment="center"):
        if not st.session_state.get("confirm_delete_board"):
            st.caption("Deleting the board also deletes its tasks and their files.")
            st.space("stretch")
            st.button(
                "Delete board", icon=":material/delete:", on_click=_set_confirm_delete, args=(True,)
            )
            return
        st.markdown(
            f"Delete **{board['name']}** with its {len(tasks)} tasks and {files} files? "
            "It can't be undone."
        )
        st.button("Cancel", on_click=_set_confirm_delete, args=(False,))
        if st.button(
            "Delete", icon=":material/delete:", type="primary", key=f"board_delete_{board['id']}"
        ):
            with st.spinner("Deleting files and tasks..."):
                delete_board(board)
            st.session_state.boards.remove(board)
            st.session_state.deleted_board = board["name"]
            # The deleted board's page is gone on the next run, so land on the default board.
            st.session_state.switch_to_board = next(
                b["id"] for b in st.session_state.boards if b["is_default"]
            )
            close_dialog()
            st.rerun()


def open_edit_dialog(task_id):
    if find_task(task_id):
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
                if file_id := files_tree(task):
                    st.session_state.open_dialog = ("file", task["id"], file_id)
                    st.rerun()
        if task["due_date"]:
            st.caption(f":material/event: {task['due_date']:%Y-%m-%d}", width="content")
        if attach_files_button(task):
            st.session_state.open_dialog = ("attach", task["id"])
            st.rerun()
        if st.button(":material/edit:", key=f"edit_{task['id']}", help="Edit task", type="tertiary"):
            st.session_state.open_dialog = task["id"]
            st.rerun()


def board_page(board_id):
    board = next(b for b in st.session_state.boards if b["id"] == board_id)

    with st.container(horizontal=True, vertical_alignment="center"):
        st.title(board["name"], icon=board.get("icon", DEFAULT_BOARD_ICON))
        st.space("stretch")
        if st.button(
            ":material/edit:",
            key=f"edit_board_{board_id}",
            help="Rename the board, change its icon or delete it",
        ):
            st.session_state.open_dialog = (BOARD_DIALOG, board_id)
        if st.button("Add task", icon=":material/add:", type="primary"):
            st.session_state.open_dialog = "add"

    dialog = st.session_state.open_dialog
    if dialog == "add":
        add_task_dialog(board_id)
    elif dialog == "new_board":
        pass  # Opened below the sidebar, so it works on every page.
    elif isinstance(dialog, tuple) and dialog[0] == BOARD_DIALOG:
        if dialog[1] == board_id:
            edit_board_dialog(board)
        else:
            close_dialog()
    elif isinstance(dialog, tuple):
        task = find_task(dialog[1])
        if dialog[0] == "attach" and task:
            attach_files_dialog(task)
        elif dialog[0] == "file" and task and (file := find_file(task, dialog[2])):
            file_dialog(task, file)
        else:
            close_dialog()
    elif dialog:
        if edited := find_task(dialog):
            edit_task_dialog(edited)
        else:
            close_dialog()

    timers_running = has_timers(board_id)
    open_count = open_task_count(board_id)

    @st.fragment(run_every=1 if timers_running else None)
    def task_list():
        tasks = board_tasks(board_id)
        # Ungrouped tasks and group boxes in board order (see board_items). Completed tasks go to the
        # top of their group, or of the board when ungrouped (most recent first).
        items = []
        for key, item_tasks in board_items(board_id):
            visible = [t for t in item_tasks if (age := done_age(t)) is None or age < HIDE_AFTER]
            done = sorted((t for t in visible if t["done_at"] is not None), key=lambda t: -t["done_at"])
            if visible:
                items.append((key, done + [t for t in visible if t["done_at"] is None]))
        done_ungrouped = sorted(
            (tasks[0] for key, tasks in items if key is None and tasks[0]["done_at"] is not None),
            key=lambda t: -t["done_at"],
        )

        if not items:
            st.info("No tasks yet. Click **Add task** to create one.", icon=":material/inbox:")

        for task in done_ungrouped:
            task_row(task)
        for key, group_tasks in items:
            if key is None:
                if group_tasks[0]["done_at"] is None:
                    task_row(group_tasks[0])
                continue
            group = group_tasks[0]["group"]
            color = group_color(group)
            # The color in the key lets the CSS tint the box; the id keeps keys unique per group.
            with st.container(border=True, key=f"group_{color}_{group_dom_id(key)}"):
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
    # Completed tasks are about to disappear, so only open ones can be dragged.
    task_drag_and_drop(
        board_id,
        [t["id"] for t in board_tasks(board_id) if t["done_at"] is None],
        partial(handle_drop, board_id),
    )


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
{TREE_CSS}
{ACCOUNT_CSS}
/* The sidebar's content fills its height, so st.space("stretch") can push the account section to
   the bottom. */
[data-testid="stSidebarContent"],
[data-testid="stSidebarUserContent"],
[data-testid="stSidebarUserContent"] > div {{
    display: flex;
    flex-direction: column;
}}
[data-testid="stSidebarUserContent"],
[data-testid="stSidebarUserContent"] > div {{
    flex: 1 0 auto;
}}
/* Streamlit's default bottom padding would lift the account section well off the bottom. */
[data-testid="stSidebarUserContent"] {{
    padding-bottom: 1.5rem;
}}
/* Page links pad their content by 0.5rem, leaving room for the current page's highlight. Everything
   else in the sidebar except dividers is indented to match, so it all lines up. */
[data-testid="stSidebarUserContent"] [data-testid="stElementContainer"]:not(:has([data-testid^="stPageLink"], hr)) {{
    padding-left: 0.5rem;
}}
/* Solid task rows so only the group box itself is tinted. */
[class*="st-key-task_row_"] {{
    background-color: {page_background};
}}
/* Task rows and group boxes can be dragged (see task_dnd). */
[class*="st-key-task_row_"],
[class*="st-key-group_"] {{
    cursor: grab;
}}
[class*="st-key-task_row_"] :is(button, input, label, [class*="st-key-file_tree_"]) {{
    cursor: pointer;
}}
{"".join(
    f"""[class*="st-key-group_{name}_"] {{ background-color: rgba({rgb}, 0.1); border-color: rgba({rgb}, 0.4); }}\n"""
    for name, rgb in GROUP_COLORS.items()
)}
</style>
""")


def storage_problems():
    """Why tasks or their files can't be stored right now; empty when everything is ready."""
    problems = []
    if not db_configured():
        problems.append(
            "No database is configured. Set `TASKBOARD_DB_HOST` or `TASKBOARD_DATABASE_URL` "
            "(see `.env.example`)."
        )
    elif error := connection_error():
        problems.append(f"The database can't be reached: `{error}`")
    if not s3_configured():
        problems.append("No file storage is configured. Set `TASKBOARD_S3_BUCKET` (see `.env.example`).")
    elif error := file_storage_error():
        problems.append(f"File storage can't be reached: `{error}`")
    return problems


problems = storage_problems()
# The login page is served at every board's URL (and the admin panel's), so opening a link to one
# while logged out shows the login form and stays on that page afterwards.
account = require_login([] if problems else [*task_store.board_url_paths(), ADMIN_URL_PATH])

if problems:
    with st.sidebar:
        account_sidebar(account)
    for problem in problems:
        st.error(problem, icon=":material/error:")
    if st.button("Try again", icon=":material/refresh:"):
        connection_error.clear()
        file_storage_error.clear()
        st.rerun()
    st.stop()

# Also recreates the account if an administrator deleted it while this session was open.
account["id"] = task_store.ensure_account(account["identity"], account["name"], account["email"])
st.session_state.account = account
st.session_state.boards = task_store.load_boards(account["id"])
st.session_state.tasks = task_store.load_tasks(account["id"])
# Lowercased group name -> color.
st.session_state.group_colors = task_store.load_group_colors(account["id"])

# One page per board. The built-in menu is hidden (it disappears entirely with a single page),
# so the sidebar below renders the board list itself.
pages = {
    board["id"]: st.Page(
        partial(board_page, board["id"]),
        title=board["name"],
        icon=board.get("icon", DEFAULT_BOARD_ICON),
        url_path=board["url_path"],
        default=board["is_default"],
    )
    for board in st.session_state.boards
}
if account["is_admin"]:
    pages["admin"] = st.Page(
        partial(admin_page, account),
        title="Admin panel",
        icon=":material/admin_panel_settings:",
        url_path=ADMIN_URL_PATH,
    )
current_page = st.navigation(list(pages.values()), position="hidden")

with st.sidebar:
    st.subheader("Boards")
    for board in st.session_state.boards:
        # Keyed so tasks and groups can be dragged onto the board.
        with st.container(key=f"board_link_{board['id']}"):
            st.page_link(
                pages[board["id"]],
                label=f"{board['name']} :small[:gray[{open_task_count(board['id'])}]]",
                width="stretch",
            )
    if st.button("New board", icon=":material/add:", type="tertiary"):
        st.session_state.open_dialog = "new_board"
    # Pushes everything below to the bottom of the sidebar.
    st.space("stretch")
    database_sidebar()
    file_storage_sidebar()
    st.divider()
    account_sidebar(account, pages.get("admin"))

# Opened here rather than on the board page, so it also works on the admin panel.
if st.session_state.open_dialog == "new_board":
    new_board_dialog()
if name := st.session_state.pop("deleted_board", None):
    st.toast(f"Deleted the board **{name}**.", icon=":material/delete:")

if switch_to := st.session_state.pop("switch_to_board", None):
    st.switch_page(pages[switch_to])

current_page.run()
