"""Invisible component that lets task rows and group boxes be dragged.

Tasks can be dropped between other tasks, into or out of groups (before, between or after them), or
onto another board in the sidebar; groups can be dropped between tasks and other groups, or onto another
board. Nothing moves on the page itself:
the drop is reported to Python, which stores the new order and reruns.

Lives in its own module so the component is registered once, not on every script rerun.
"""

import streamlit as st

# Rows, group boxes and sidebar board links are containers keyed "task_row_<id>",
# "group_<color>_<group id>" and "board_link_<id>", which Streamlit renders with the class
# "st-key-<key>". Document-level listeners cover them all, including ones added later.
_JS = """
const ROW = "st-key-task_row_"
const GROUP = "st-key-group_"
const BOARD = "st-key-board_link_"
const THRESHOLD = 5 // px the pointer travels before a press becomes a drag
const EDGE = 60 // px from the window's edge where dragging scrolls the page
const EDGE_IN = 14 // px at a group box's top and bottom that drop next to the group, not into it
const GAP = 16 // px between rows and boxes

function suffix(el, prefix) {
  const cls = [...el.classList].find((c) => c.startsWith(prefix))
  return cls ? cls.slice(prefix.length) : null
}
const rowId = (row) => suffix(row, ROW)
// The group id is the part after the color.
const groupId = (box) => suffix(box, GROUP).split("_").pop()
const boardId = (link) => suffix(link, BOARD)

// The ghost following the pointer is a copy of the dragged element, keys included, so skip it.
const all = (prefix) =>
  [...document.querySelectorAll(`[class*="${prefix}"]`)].filter((el) => !el.closest("[data-task-dnd-ghost]"))
const inMain = (el) => !el.closest('[data-testid="stSidebar"]')

export default function (component) {
  const { data, parentElement, setTriggerValue } = component
  const state = (window.__taskDnd ??= {})
  state.data = data
  // The theme's variables are set on the component's host; parentElement may be its shadow root.
  const host = parentElement instanceof ShadowRoot ? parentElement.host : parentElement
  state.color = getComputedStyle(host).getPropertyValue("--st-primary-color").trim() || "#ff4b4b"
  state.setTriggerValue = setTriggerValue
  if (state.listeners) {
    state.abort()
    for (const [type, fn, opts] of state.listeners) document.removeEventListener(type, fn, opts)
  }

  let press = null // {x, y, el, kind} after pointerdown, until the drag starts or the pointer is released
  let drag = null // the drag in progress

  const openRows = () => all(ROW).filter((r) => inMain(r) && state.data.open.includes(rowId(r)))
  const groups = () => all(GROUP).filter(inMain)
  const ungroupedRows = () => openRows().filter((r) => !r.closest(`[class*="${GROUP}"]`))

  function onPointerDown(event) {
    if (event.button !== 0 || event.pointerType === "touch") return
    // Controls and file names inside rows keep their normal behavior.
    const t = event.target
    if (t.closest('[data-testid="stCheckbox"], button, a, input, textarea, [role="button"]')) return
    if (t.closest('[class*="st-key-file_tree_"], [role="dialog"]') || !inMain(t)) return
    const row = t.closest(`[class*="${ROW}"]`)
    if (row) {
      if (state.data.open.includes(rowId(row))) press = { x: event.clientX, y: event.clientY, el: row, kind: "task" }
      return
    }
    const box = t.closest(`[class*="${GROUP}"]`)
    if (box) press = { x: event.clientX, y: event.clientY, el: box, kind: "group" }
  }

  function start(event) {
    const { el, kind } = press
    const rect = el.getBoundingClientRect()
    // A copy of the element on the page's background, as group boxes are only tinted.
    const ghost = document.createElement("div")
    const copy = el.cloneNode(true)
    copy.style.margin = "0"
    ghost.append(copy)
    ghost.dataset.taskDndGhost = ""
    const app = document.querySelector('[data-testid="stApp"]') ?? document.body
    Object.assign(ghost.style, {
      background: getComputedStyle(app).backgroundColor,
      borderRadius: getComputedStyle(el).borderRadius,
      position: "fixed",
      left: `${rect.left}px`,
      top: `${rect.top}px`,
      width: `${rect.width}px`,
      maxHeight: "12rem",
      overflow: "hidden",
      margin: "0",
      pointerEvents: "none",
      // Above the sidebar, so the ghost can be dropped onto a board there.
      zIndex: "1000000",
      opacity: "0.9",
      boxShadow: "0 8px 24px rgba(0, 0, 0, 0.25)",
      transform: "rotate(1deg)",
    })
    const line = document.createElement("div")
    Object.assign(line.style, {
      position: "fixed",
      height: "3px",
      borderRadius: "2px",
      background: state.color,
      pointerEvents: "none",
      zIndex: "1000001",
      display: "none",
    })
    document.body.append(ghost, line)
    el.style.opacity = "0.35"
    document.documentElement.style.userSelect = "none"
    document.documentElement.style.cursor = "grabbing"
    window.getSelection()?.removeAllRanges()
    drag = {
      kind,
      el,
      id: kind === "task" ? rowId(el) : groupId(el),
      ghost,
      line,
      dx: press.x - rect.left,
      dy: press.y - rect.top,
      x: event.clientX,
      y: event.clientY,
      target: null,
      board: null,
    }
    drag.origin = placement(drag.kind === "task" ? taskTarget : groupTarget)
    drag.scroll = requestAnimationFrame(autoScroll)
  }

  // Where the dragged item currently sits, to tell a real move from dropping it back in place.
  function placement(targetFn) {
    const rect = drag.el.getBoundingClientRect()
    return targetFn(rect.left + rect.width / 2, rect.top + rect.height / 2, true)
  }

  function showLine(left, width, y) {
    Object.assign(drag.line.style, { display: "block", left: `${left}px`, width: `${width}px`, top: `${y - 1.5}px` })
  }

  // The board's top level: open ungrouped tasks and group boxes, which can come in any order.
  const topItems = () =>
    [...ungroupedRows(), ...groups()]
      .filter((el) => el !== drag.el)
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)
  function ref(el) {
    if (!el) return null
    return el.matches(`[class*="${GROUP}"]`) ? { kind: "group", id: groupId(el) } : { kind: "task", id: rowId(el) }
  }

  // Where on the board's top level the dragged task or group would be dropped: before which item, or
  // null for the end.
  function topTarget(y, quiet) {
    const items = topItems()
    const before = items.find((el) => {
      const r = el.getBoundingClientRect()
      return y < r.top + r.height / 2
    })
    if (!quiet && items.length) {
      if (before) {
        const r = before.getBoundingClientRect()
        showLine(r.left, r.width, r.top - GAP / 2)
      } else {
        const r = items[items.length - 1].getBoundingClientRect()
        showLine(r.left, r.width, r.bottom + GAP / 2)
      }
    }
    return { group: null, before: ref(before) }
  }

  // The group a dragged task would be dropped into (null for the board's top level), and the item it
  // would be dropped before (null for the end).
  function taskTarget(x, y, quiet = false) {
    // A group's top and bottom edges count as outside it, so a task can be dropped before or after
    // the group rather than only into it.
    const box = groups().find((b) => {
      const r = b.getBoundingClientRect()
      return y >= r.top + EDGE_IN && y <= r.bottom - EDGE_IN
    })
    if (!box) return topTarget(y, quiet)
    const rows = openRows().filter((r) => box.contains(r) && r !== drag.el)
    const before = rows.find((r) => {
      const rr = r.getBoundingClientRect()
      return y < rr.top + rr.height / 2
    })
    if (!quiet) {
      if (before) {
        const r = before.getBoundingClientRect()
        showLine(r.left, r.width, r.top - GAP / 2)
      } else if (rows.length) {
        const r = rows[rows.length - 1].getBoundingClientRect()
        showLine(r.left, r.width, r.bottom + GAP / 2)
      } else {
        const r = box.getBoundingClientRect()
        showLine(r.left + 16, r.width - 32, r.bottom - EDGE_IN - 2)
      }
    }
    return { group: groupId(box), before: ref(before) }
  }

  const groupTarget = (x, y, quiet = false) => topTarget(y, quiet)

  function setBoard(link) {
    if (drag.board === link) return
    if (drag.board) Object.assign(drag.board.style, { outline: "", borderRadius: "" })
    if (link) Object.assign(link.style, { outline: `2px solid ${state.color}`, borderRadius: "0.5rem" })
    drag.board = link
  }

  function update() {
    const { x, y } = drag
    const under = document.elementFromPoint(x, y)
    // Over the sidebar the ghost shrinks beside the pointer, so it doesn't hide the boards.
    const aside = Boolean(under?.closest('[data-testid="stSidebar"]'))
    Object.assign(drag.ghost.style, {
      left: `${aside ? x + 12 : x - drag.dx}px`,
      top: `${aside ? y + 12 : y - drag.dy}px`,
      transformOrigin: "top left",
      transform: aside ? "scale(0.5)" : "rotate(1deg)",
    })
    const link = under?.closest(`[class*="${BOARD}"]`)
    if (link && boardId(link) !== state.data.board) {
      setBoard(link)
      drag.line.style.display = "none"
      drag.target = null
      return
    }
    setBoard(null)
    drag.target = (drag.kind === "task" ? taskTarget : groupTarget)(x, y)
  }

  function autoScroll() {
    if (!drag) return
    const speed = drag.y < EDGE ? -(EDGE - drag.y) : drag.y > innerHeight - EDGE ? drag.y - (innerHeight - EDGE) : 0
    if (speed) {
      // The main area scrolls on its own, separately from the sidebar.
      const main = drag.el.closest('[data-testid="stMain"]') ?? document.scrollingElement
      main.scrollBy(0, speed / 3)
      document.scrollingElement.scrollBy(0, speed / 3)
      update()
    }
    drag.scroll = requestAnimationFrame(autoScroll)
  }

  function onPointerMove(event) {
    if (press && !drag) {
      if (Math.hypot(event.clientX - press.x, event.clientY - press.y) < THRESHOLD) return
      start(event)
    }
    if (!drag) return
    event.preventDefault()
    drag.x = event.clientX
    drag.y = event.clientY
    update()
  }

  function finish(drop) {
    press = null
    if (!drag) return
    cancelAnimationFrame(drag.scroll)
    drag.ghost.remove()
    drag.line.remove()
    drag.el.style.opacity = ""
    const { kind, id, target, board, origin } = drag
    setBoard(null)
    document.documentElement.style.userSelect = ""
    document.documentElement.style.cursor = ""
    drag = null
    if (!drop) return
    // The press ends over another element, so swallow the click it would otherwise produce.
    const swallow = (e) => {
      e.stopPropagation()
      e.preventDefault()
    }
    document.addEventListener("click", swallow, { capture: true, once: true })
    setTimeout(() => document.removeEventListener("click", swallow, { capture: true }), 0)
    if (board) {
      state.setTriggerValue("drop", { kind, id, board: boardId(board), group: null, before: null })
    } else if (target && JSON.stringify(target) !== JSON.stringify(origin)) {
      state.setTriggerValue("drop", { kind, id, board: null, ...target })
    }
  }

  const onPointerUp = () => finish(true)
  const onCancel = () => finish(false)
  const onKeyDown = (event) => {
    if (event.key === "Escape" && drag) finish(false)
  }
  const onSelectStart = (event) => {
    if (drag) event.preventDefault()
  }
  state.abort = onCancel

  state.listeners = [
    ["pointerdown", onPointerDown, true],
    ["pointermove", onPointerMove, false],
    ["pointerup", onPointerUp, false],
    ["pointercancel", onCancel, false],
    ["keydown", onKeyDown, false],
    ["selectstart", onSelectStart, false],
  ]
  for (const [type, fn, opts] of state.listeners) document.addEventListener(type, fn, opts)
}
"""

_listener = st.components.v2.component("task_drag_and_drop", js=_JS)


def task_drag_and_drop(board_id, open_task_ids, on_drop, *, key="task_dnd"):
    """Mount the drag handling for the board's open tasks and its groups.

    `on_drop(drop)` runs before the rerun that follows a drop, with a dict of: "kind" ("task" or
    "group"), "id" (the task's id, or the group's id from the box's key), "board" (the id of the board
    it was dropped onto in the sidebar, or None), "group" (for a task, the group id it was dropped into,
    or None for the board's top level), and "before" (what it was dropped in front of, as
    {"kind": "task" or "group", "id": ...}, or None for the end of the group or board).
    """
    _listener(
        key=key,
        data={"board": board_id, "open": list(open_task_ids)},
        on_drop_change=lambda: on_drop(st.session_state[key].drop),
    )
