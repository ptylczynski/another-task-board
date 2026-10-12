"""Accounts, boards, tasks and attachment metadata in PostgreSQL. File contents live in S3 (see
file_store).

Every board belongs to one account, and tasks and their files belong to a board, so deleting an
account deletes everything it owns. Rows are returned as the dicts the app works with: times as
epoch seconds (like time.time()), due dates as datetime.date, and each task's attachments under
"files". The tables are created on first use.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
import streamlit as st

from db import connection

DEFAULT_BOARD_ICON = "📋"
DEFAULT_BOARD_URL_PATH = "default"

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id text PRIMARY KEY,
        -- How the person signs in, e.g. "password:<login>" or "oidc:<issuer>:<subject>".
        identity text NOT NULL UNIQUE,
        name text NOT NULL,
        email text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS boards (
        id text PRIMARY KEY,
        user_id text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        name text NOT NULL,
        icon text NOT NULL,
        url_path text NOT NULL,
        is_default boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (user_id, url_path)
    )
    """,
    # Each account has exactly one default board, its landing page.
    "CREATE UNIQUE INDEX IF NOT EXISTS boards_one_default ON boards (user_id) WHERE is_default",
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
    # Where the task sits on its board once it has been dragged; new tasks (NULL) go after the rest.
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS position integer",
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
        user_id text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        group_key text NOT NULL,
        color text NOT NULL,
        PRIMARY KEY (user_id, group_key)
    )
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


def ensure_account(identity, name, email):
    """Create the account on first sign-in, or refresh its details; returns the account's id.

    Also creates the account's default board if it doesn't have one yet.
    """
    ensure_schema()
    with connection().engine.begin() as conn:
        user_id = conn.execute(
            sa.text(
                """
                INSERT INTO users (id, identity, name, email) VALUES (:id, :identity, :name, :email)
                ON CONFLICT (identity) DO UPDATE
                SET name = EXCLUDED.name, email = EXCLUDED.email, last_seen_at = clock_timestamp()
                RETURNING id
                """
            ),
            {"id": uuid.uuid4().hex, "identity": identity, "name": name, "email": email},
        ).scalar_one()
        conn.execute(
            sa.text(
                """
                INSERT INTO boards (id, user_id, name, icon, url_path, is_default)
                VALUES (:id, :user_id, 'Default', :icon, :url_path, true)
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "id": uuid.uuid4().hex,
                "user_id": user_id,
                "icon": DEFAULT_BOARD_ICON,
                "url_path": DEFAULT_BOARD_URL_PATH,
            },
        )
    return user_id


def board_url_paths():
    """The URL paths of every account's boards other than the default ones, for routing at login."""
    ensure_schema()
    return [row["url_path"] for row in _rows("SELECT DISTINCT url_path FROM boards WHERE NOT is_default")]


def load_boards(user_id):
    return [
        dict(row)
        for row in _rows(
            """
            SELECT id, name, icon, url_path, is_default FROM boards
            WHERE user_id = :user_id ORDER BY created_at, id
            """,
            {"user_id": user_id},
        )
    ]


def insert_board(user_id, board):
    _execute(
        """
        INSERT INTO boards (id, user_id, name, icon, url_path)
        VALUES (:id, :user_id, :name, :icon, :url_path)
        """,
        {"user_id": user_id, **board},
    )


def update_board(board_id, name, icon, url_path):
    _execute(
        "UPDATE boards SET name = :name, icon = :icon, url_path = :url_path WHERE id = :id",
        {"id": board_id, "name": name, "icon": icon, "url_path": url_path},
    )


def board_task_ids(board_id):
    """The ids of the board's tasks as stored, including ones added by other sessions."""
    rows = _rows("SELECT id FROM tasks WHERE board_id = :board_id", {"board_id": board_id})
    return [row["id"] for row in rows]


def delete_board(board_id):
    """Delete the board with its tasks and file records (not the files' contents).

    The default board is every account's landing page, so it's left alone; returns whether the board
    was deleted.
    """
    result = _execute("DELETE FROM boards WHERE id = :id AND NOT is_default", {"id": board_id})
    return result.rowcount == 1


def _file(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "size": row["size"],
        "key": row["object_key"],
        "updated_at": _to_seconds(row["updated_at"]),
    }


def load_tasks(user_id):
    """The account's tasks in board order (dragged ones first, then the rest in creation order), each
    with its files."""
    files = {}
    for row in _rows(
        """
        SELECT f.* FROM task_files f
        JOIN tasks t ON t.id = f.task_id JOIN boards b ON b.id = t.board_id
        WHERE b.user_id = :user_id ORDER BY f.created_at, f.id
        """,
        {"user_id": user_id},
    ):
        files.setdefault(row["task_id"], []).append(_file(row))
    return [
        {
            "id": row["id"],
            "user_id": user_id,
            "board_id": row["board_id"],
            "name": row["name"],
            "description": row["description"],
            "tags": list(row["tags"]),
            "group": row["group_name"],
            "due_date": row["due_date"],
            "done_at": _to_seconds(row["done_at"]),
            "files": files.get(row["id"], []),
        }
        for row in _rows(
            """
            SELECT t.* FROM tasks t JOIN boards b ON b.id = t.board_id
            WHERE b.user_id = :user_id ORDER BY t.position NULLS LAST, t.created_at, t.id
            """,
            {"user_id": user_id},
        )
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


def save_order(tasks):
    """Store each task's board, group and position, as given by its index in `tasks`."""
    if tasks:
        _execute(
            """
            UPDATE tasks SET board_id = :board_id, group_name = :group_name, position = :position
            WHERE id = :id
            """,
            [
                {"id": t["id"], "board_id": t["board_id"], "group_name": t["group"], "position": i}
                for i, t in enumerate(tasks)
            ],
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


def load_group_colors(user_id):
    return {
        row["group_key"]: row["color"]
        for row in _rows("SELECT * FROM group_colors WHERE user_id = :user_id", {"user_id": user_id})
    }


def save_group_color(user_id, group_key, color):
    _execute(
        """
        INSERT INTO group_colors (user_id, group_key, color) VALUES (:user_id, :group_key, :color)
        ON CONFLICT (user_id, group_key) DO UPDATE SET color = EXCLUDED.color
        """,
        {"user_id": user_id, "group_key": group_key, "color": color},
    )


def load_accounts():
    """Every account with how much it stores, the largest storage users first."""
    return [
        {
            **row,
            "created_at": _to_seconds(row["created_at"]),
            "last_seen_at": _to_seconds(row["last_seen_at"]),
        }
        for row in _rows(
            """
            SELECT u.id, u.identity, u.name, u.email, u.created_at, u.last_seen_at,
                   count(DISTINCT b.id) AS boards, count(DISTINCT t.id) AS tasks,
                   count(f.id) AS files, coalesce(sum(f.size), 0)::bigint AS bytes
            FROM users u
            LEFT JOIN boards b ON b.user_id = u.id
            LEFT JOIN tasks t ON t.board_id = b.id
            LEFT JOIN task_files f ON f.task_id = t.id
            GROUP BY u.id
            ORDER BY bytes DESC, u.created_at
            """
        )
    ]


def delete_account(user_id):
    """Delete the account with all its boards, tasks and file records (not the files' contents)."""
    _execute("DELETE FROM users WHERE id = :id", {"id": user_id})
