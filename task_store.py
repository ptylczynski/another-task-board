"""Boards, tasks and attachment metadata in PostgreSQL. File contents live in S3 (see file_store).

Rows are returned as the dicts the app works with: times as epoch seconds (like time.time()), due
dates as datetime.date, and each task's attachments under "files". The tables are created on first
use.
"""

from datetime import UTC, datetime

import sqlalchemy as sa
import streamlit as st

from db import connection

DEFAULT_BOARD_ID = "default"
DEFAULT_BOARD_ICON = "📋"

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS boards (
        id text PRIMARY KEY,
        name text NOT NULL,
        icon text NOT NULL,
        url_path text NOT NULL UNIQUE,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id text PRIMARY KEY,
        board_id text NOT NULL REFERENCES boards (id) ON DELETE CASCADE,
        name text NOT NULL,
        description text NOT NULL DEFAULT '',
        tags text[] NOT NULL DEFAULT '{}',
        group_name text,
        due_date date,
        done_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )
    """,
    "CREATE INDEX IF NOT EXISTS tasks_board_id ON tasks (board_id)",
    """
    CREATE TABLE IF NOT EXISTS task_files (
        id text PRIMARY KEY,
        task_id text NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
        name text NOT NULL,
        type text NOT NULL,
        size bigint NOT NULL,
        object_key text NOT NULL,
        updated_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )
    """,
    "CREATE INDEX IF NOT EXISTS task_files_task_id ON task_files (task_id)",
    """
    CREATE TABLE IF NOT EXISTS group_colors (
        group_key text PRIMARY KEY,
        color text NOT NULL
    )
    """,
    # The default board always exists; it's the app's landing page.
    f"""
    INSERT INTO boards (id, name, icon, url_path)
    VALUES ('{DEFAULT_BOARD_ID}', 'Default', '{DEFAULT_BOARD_ICON}', 'default')
    ON CONFLICT DO NOTHING
    """,
]


def _to_datetime(seconds):
    return None if seconds is None else datetime.fromtimestamp(seconds, UTC)


def _to_seconds(moment):
    return None if moment is None else moment.timestamp()


@st.cache_resource(show_spinner=False)
def ensure_schema():
    """Create the tables once per server process; they're left alone when they already exist."""
    with connection().engine.begin() as conn:
        for statement in SCHEMA:
            conn.execute(sa.text(statement))


def _execute(statement, params=None):
    with connection().engine.begin() as conn:
        return conn.execute(sa.text(statement), params or {})


def _rows(statement, params=None):
    with connection().engine.connect() as conn:
        return conn.execute(sa.text(statement), params or {}).mappings().all()


def load_boards():
    ensure_schema()
    return [
        dict(row)
        for row in _rows("SELECT id, name, icon, url_path FROM boards ORDER BY created_at, id")
    ]


def insert_board(board):
    _execute(
        "INSERT INTO boards (id, name, icon, url_path) VALUES (:id, :name, :icon, :url_path)", board
    )


def _file(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "size": row["size"],
        "key": row["object_key"],
        "updated_at": _to_seconds(row["updated_at"]),
    }


def load_tasks():
    """All tasks in creation order, each with its files."""
    ensure_schema()
    files = {}
    for row in _rows("SELECT * FROM task_files ORDER BY created_at, id"):
        files.setdefault(row["task_id"], []).append(_file(row))
    return [
        {
            "id": row["id"],
            "board_id": row["board_id"],
            "name": row["name"],
            "description": row["description"],
            "tags": list(row["tags"]),
            "group": row["group_name"],
            "due_date": row["due_date"],
            "done_at": _to_seconds(row["done_at"]),
            "files": files.get(row["id"], []),
        }
        for row in _rows("SELECT * FROM tasks ORDER BY created_at, id")
    ]


def _task_params(fields):
    return {
        "name": fields["name"],
        "description": fields["description"],
        "tags": fields["tags"],
        "group_name": fields["group"],
        "due_date": fields["due_date"],
    }


def _file_params(task_id, file):
    return {
        "id": file["id"],
        "task_id": task_id,
        "name": file["name"],
        "type": file["type"],
        "size": file["size"],
        "object_key": file["key"],
        "updated_at": _to_datetime(file["updated_at"]),
    }


INSERT_FILE = """
    INSERT INTO task_files (id, task_id, name, type, size, object_key, updated_at)
    VALUES (:id, :task_id, :name, :type, :size, :object_key, :updated_at)
"""


def insert_task(task):
    """Insert the task together with its (already uploaded) files."""
    with connection().engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                INSERT INTO tasks (id, board_id, name, description, tags, group_name, due_date, done_at)
                VALUES (:id, :board_id, :name, :description, :tags, :group_name, :due_date, :done_at)
                """
            ),
            {
                "id": task["id"],
                "board_id": task["board_id"],
                "done_at": _to_datetime(task["done_at"]),
                **_task_params(task),
            },
        )
        if task["files"]:
            conn.execute(sa.text(INSERT_FILE), [_file_params(task["id"], f) for f in task["files"]])


def update_task(task_id, fields):
    _execute(
        """
        UPDATE tasks
        SET name = :name, description = :description, tags = :tags, group_name = :group_name,
            due_date = :due_date
        WHERE id = :id
        """,
        {"id": task_id, **_task_params(fields)},
    )


def set_done(task_id, done_at):
    _execute(
        "UPDATE tasks SET done_at = :done_at WHERE id = :id",
        {"id": task_id, "done_at": _to_datetime(done_at)},
    )


def insert_files(task_id, files):
    if files:
        with connection().engine.begin() as conn:
            conn.execute(sa.text(INSERT_FILE), [_file_params(task_id, f) for f in files])


def update_file(file):
    _execute(
        """
        UPDATE task_files
        SET name = :name, type = :type, size = :size, object_key = :object_key,
            updated_at = :updated_at
        WHERE id = :id
        """,
        _file_params(None, file),
    )


def delete_file(file_id):
    _execute("DELETE FROM task_files WHERE id = :id", {"id": file_id})


def load_group_colors():
    return {row["group_key"]: row["color"] for row in _rows("SELECT * FROM group_colors")}


def save_group_color(group_key, color):
    _execute(
        """
        INSERT INTO group_colors (group_key, color) VALUES (:group_key, :color)
        ON CONFLICT (group_key) DO UPDATE SET color = EXCLUDED.color
        """,
        {"group_key": group_key, "color": color},
    )
