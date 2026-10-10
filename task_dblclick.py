"""Invisible component that reports double-clicks on task rows.

Lives in its own module so the component is registered once, not on every script rerun.
"""

import streamlit as st

# Task rows are containers keyed "task_row_<id>", which Streamlit renders with the class
# "st-key-task_row_<id>". One document-level listener covers all rows, including ones added later.
_JS = """
export default function (component) {
  const { setTriggerValue } = component
  const prefix = "st-key-task_row_"

  if (window.__taskDblClickHandler) {
    document.removeEventListener("dblclick", window.__taskDblClickHandler)
  }
  window.__taskDblClickHandler = (event) => {
    // Double-clicking controls inside the row keeps their normal behavior, and so does the file
    // tree, whose names open the file instead.
    if (event.target.closest('[data-testid="stCheckbox"], button, input, textarea')) return
    if (event.target.closest('[class*="st-key-file_tree_"]')) return
    const row = event.target.closest(`[class*="${prefix}"]`)
    if (!row) return
    const rowClass = [...row.classList].find((c) => c.startsWith(prefix))
    window.getSelection()?.removeAllRanges()
    setTriggerValue("task_id", rowClass.slice(prefix.length))
  }
  document.addEventListener("dblclick", window.__taskDblClickHandler)
}
"""

_listener = st.components.v2.component("task_dblclick_listener", js=_JS)


def task_dblclick_listener(on_double_click, *, key="task_dblclick"):
    """Mount the listener; `on_double_click(task_id)` runs before the rerun that follows."""
    _listener(
        key=key,
        on_task_id_change=lambda: on_double_click(st.session_state[key].task_id),
    )
