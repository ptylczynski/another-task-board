"""MCP server that lets AI assistants read and change an account's boards and tasks.

It's served at /mcp by asgi_app.py, next to the app, over Streamable HTTP. Every request needs an
access token, sent as `Authorization: Bearer <token>`; each person creates theirs in the app (see
access_tokens_dialog in streamlit_app.py), and the tools then work on that person's account.

Tasks are named by their id, boards by their id or name. Completed tasks stay in the database (the
board hides them), so they can be listed and reopened.
"""

import secrets
import time
import uuid
from datetime import date
from typing import TypedDict

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.concurrency import run_in_threadpool

import task_store

MCP_PATH = "/mcp"
TOKEN_PREFIX = "atb_"  # makes the tokens recognizable, e.g. to secret scanners
READ_ONLY = ToolAnnotations(read_only_hint=True)


class Board(TypedDict):
    id: str
    name: str
    icon: str
    is_default: bool
    open_tasks: int


class Task(TypedDict):
    id: str
    board: str
    name: str
    description: str
    tags: list[str]
    group: str | None
    due_date: str | None
    done: bool
    files: list[str]


def new_token():
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


class AccessTokenVerifier:
    """Accepts the access tokens stored in the database; the token's account becomes its subject."""

    async def verify_token(self, token):
        if not token.startswith(TOKEN_PREFIX):
            return None
        # The database calls are blocking, so they run off the event loop.
        found = await run_in_threadpool(task_store.use_access_token, token)
        if not found:
            return None
        token_id, user_id = found
        return AccessToken(token=token, client_id=token_id, scopes=[], subject=user_id)


mcp = MCPServer(
    "another-task-board",
    title="Another Task Board",
    instructions=(
        "Tools for the user's task board: boards hold tasks, which can have a description, tags, a "
        "group and a due date. Find task ids with list_tasks before changing a task."
    ),
    token_verifier=AccessTokenVerifier(),
    # Only the bearer token check is used: tokens are issued by the app itself, not by an OAuth server,
    # so the issuer URL is never published or contacted.
    auth=AuthSettings(issuer_url="http://localhost", resource_server_url=None),
)


def _user_id():
    return get_access_token().subject


def _board(user_id, board):
    """The account's board with this id or name (case-insensitively); its default board for None."""
    boards = task_store.load_boards(user_id)
    if board is None:
        return next(b for b in boards if b["is_default"])
    found = next((b for b in boards if b["id"] == board), None) or next(
        (b for b in boards if b["name"].lower() == board.strip().lower()), None
    )
    if not found:
        names = ", ".join(b["name"] for b in boards)
        raise ToolError(f"No board named {board!r}. The boards are: {names}.")
    return found


def _task(user_id, task_id):
    task = next((t for t in task_store.load_tasks(user_id) if t["id"] == task_id), None)
    if not task:
        raise ToolError(f"No task with the id {task_id!r}. Use list_tasks to find task ids.")
    return task


def _group(user_id, board_id, group):
    """The group's name as the board already has it, so a differently cased name joins that group."""
    group = (group or "").strip()
    if not group:
        return None
    existing = (g["name"] for g in task_store.load_groups(user_id) if g["board_id"] == board_id)
    return next((name for name in existing if name.lower() == group.lower()), group)


def _due_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"Invalid due date {value!r}; use YYYY-MM-DD.") from None


def _name(value):
    if not (value := value.strip()):
        raise ToolError("The task name can't be empty.")
    return value


def _task_info(task, boards) -> Task:
    return {
        "id": task["id"],
        "board": boards[task["board_id"]]["name"],
        "name": task["name"],
        "description": task["description"],
        "tags": task["tags"],
        "group": task["group"],
        "due_date": task["due_date"].isoformat() if task["due_date"] else None,
        "done": task["done_at"] is not None,
        "files": [f["name"] for f in task["files"]],
    }


def _info(user_id, task):
    return _task_info(task, {b["id"]: b for b in task_store.load_boards(user_id)})


@mcp.tool(annotations=READ_ONLY)
def list_boards() -> list[Board]:
    """List the user's boards with how many open tasks each has."""
    user_id = _user_id()
    tasks = task_store.load_tasks(user_id)
    return [
        {
            "id": b["id"],
            "name": b["name"],
            "icon": b["icon"],
            "is_default": b["is_default"],
            "open_tasks": sum(t["board_id"] == b["id"] and t["done_at"] is None for t in tasks),
        }
        for b in task_store.load_boards(user_id)
    ]


@mcp.tool(annotations=READ_ONLY)
def list_tasks(board: str | None = None, include_done: bool = False) -> list[Task]:
    """List tasks in board order.

    Args:
        board: A board's name or id; all boards when omitted.
        include_done: Also list completed tasks.
    """
    user_id = _user_id()
    boards = {b["id"]: b for b in task_store.load_boards(user_id)}
    board_id = _board(user_id, board)["id"] if board else None
    return [
        _task_info(t, boards)
        for t in task_store.load_tasks(user_id)
        if (board_id is None or t["board_id"] == board_id) and (include_done or t["done_at"] is None)
    ]


@mcp.tool()
def add_task(
    name: str,
    board: str | None = None,
    description: str = "",
    tags: list[str] | None = None,
    group: str | None = None,
    due_date: str | None = None,
) -> Task:
    """Add a task at the end of a board.

    Args:
        name: What needs to be done.
        board: A board's name or id; the default board when omitted.
        description: Longer notes on the task.
        tags: Labels for the task.
        group: The group to put it in; a new group is created if the board doesn't have it.
        due_date: In YYYY-MM-DD format.
    """
    user_id = _user_id()
    board_id = _board(user_id, board)["id"]
    task = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "board_id": board_id,
        "name": _name(name),
        "description": description.strip(),
        "tags": task_store.clean_tags(tags or []),
        "group": _group(user_id, board_id, group),
        "due_date": _due_date(due_date),
        "done_at": None,
        "files": [],
    }
    task_store.insert_task(task)
    return _info(user_id, task)


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def update_task(
    task_id: str,
    name: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    group: str | None = None,
    due_date: str | None = None,
) -> Task:
    """Change a task. Leave out the fields to keep as they are.

    Args:
        task_id: The task's id, from list_tasks.
        name: The new name.
        description: The new description; an empty string removes it.
        tags: The new tags, replacing the current ones; an empty list removes them.
        group: The group to move it to; an empty string takes it out of its group.
        due_date: In YYYY-MM-DD format; an empty string removes it.
    """
    user_id = _user_id()
    task = _task(user_id, task_id)
    if name is not None:
        task["name"] = _name(name)
    if description is not None:
        task["description"] = description.strip()
    if tags is not None:
        task["tags"] = task_store.clean_tags(tags)
    if group is not None:
        task["group"] = _group(user_id, task["board_id"], group)
    if due_date is not None:
        task["due_date"] = _due_date(due_date)
    task_store.update_task(task_id, task)
    return _info(user_id, task)


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def complete_task(task_id: str) -> Task:
    """Mark a task as done; the board then hides it.

    Args:
        task_id: The task's id, from list_tasks.
    """
    user_id = _user_id()
    task = _task(user_id, task_id)
    if task["done_at"] is None:
        task["done_at"] = time.time()
        task_store.set_done(task_id, task["done_at"])
    return _info(user_id, task)


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def reopen_task(task_id: str) -> Task:
    """Mark a completed task as not done, so it shows on its board again.

    Args:
        task_id: The task's id, from list_tasks with include_done.
    """
    user_id = _user_id()
    task = _task(user_id, task_id)
    if task["done_at"] is not None:
        task["done_at"] = None
        task_store.set_done(task_id, None)
    return _info(user_id, task)


def mcp_app():
    """The ASGI app serving the MCP endpoint at MCP_PATH; run it inside `mcp.session_manager.run()`."""
    return mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        # Each request stands on its own, so nothing is lost when the app restarts.
        stateless_http=True,
        json_response=True,
        # The app is usually reached through a reverse proxy under its own host name. DNS rebinding
        # protection isn't needed: every request carries a token that a rebinding page can't have.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
