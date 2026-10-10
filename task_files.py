"""Files attached to tasks: add, preview, download, rename, replace and delete.

Each task keeps its attachments in `task["files"]`, a list of dicts holding the file's bytes, so they
live in session state alongside the tasks themselves.
"""

import io
import re
import time
import uuid
from datetime import datetime

import streamlit as st
from PIL import Image

PREVIEW_CHARS = 20_000  # text previews are cut off after this many characters
# The tree's connecting lines are drawn with borders rather than "│├└" characters: glyphs don't
# fill the whole line height, so the vertical lines would break between rows.
TREE_CSS = """
.file-tree-guides {
    display: flex;
    height: 1.75rem;
}
.file-tree-guides span {
    position: relative;
    width: 1.25rem;
    opacity: 0.4;
}
.file-tree-guides .pipe::before,
.file-tree-guides .tee::before,
.file-tree-guides .end::before {
    content: "";
    position: absolute;
    left: 0.5rem;
    top: 0;
    bottom: 0;
    border-left: 1px solid currentColor;
}
.file-tree-guides .end::before {
    bottom: 50%;
}
.file-tree-guides .tee::after,
.file-tree-guides .end::after {
    content: "";
    position: absolute;
    left: 0.5rem;
    right: 0.125rem;
    top: 50%;
    border-top: 1px solid currentColor;
}
/* File names in the tree are buttons; keep them as compact as the folder names around them. */
[class*="st-key-file_open_"] button {
    min-height: 0;
    height: 1.5rem;
}
[class*="st-key-file_open_"] button p {
    font-size: 0.875rem;
}
"""
TEXT_TYPES = ("application/json", "application/xml", "application/x-yaml", "application/csv")


def task_files(task):
    return task.setdefault("files", [])


def find_file(task, file_id):
    return next((f for f in task_files(task) if f["id"] == file_id), None)


def stored_file(upload):
    return {
        "id": uuid.uuid4().hex,
        "name": upload.name,
        "type": upload.type or "application/octet-stream",
        "size": upload.size,
        "data": upload.getvalue(),
        "updated_at": time.time(),
    }


def stored_files(uploads):
    return [stored_file(upload) for upload in uploads or []]


def uploader_key(name):
    # File uploaders can't be cleared through session state, so a fresh key replaces the widget
    # with an empty one once its files have been stored.
    return f"{name}_{st.session_state.get('upload_nonce', 0)}"


def reset_uploaders():
    st.session_state.upload_nonce = st.session_state.get("upload_nonce", 0) + 1


def file_size(size):
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def file_icon(file):
    kind = file["type"].split("/")[0]
    if kind == "image":
        return ":material/image:"
    if kind in ("audio", "video"):
        return f":material/{'audio_file' if kind == 'audio' else 'video_file'}:"
    if file["type"] == "application/pdf":
        return ":material/picture_as_pdf:"
    if is_text(file):
        return ":material/description:"
    return ":material/draft:"


def is_text(file):
    return file["type"].startswith("text/") or file["type"] in TEXT_TYPES


def escape_markdown(text):
    # File names often contain underscores, dollar signs or brackets that Markdown would interpret.
    return re.sub(r"([\\`*_{}\[\]()<>#+\-.!|~$:])", r"\\\1", text)


def _tree_node():
    return {"folders": {}, "files": []}


def tree_lines(files):
    """(guides, label, file or None for a folder) triples drawing the files as a tree.

    Guides name the line drawn in each indentation column: "pipe" continues an ancestor's line,
    "blank" leaves the column empty, and the last one is "tee" or "end" (the last entry of a
    folder). Names containing "/" are nested into folders.
    """
    root = _tree_node()
    for file in files:
        *folders, name = [part for part in file["name"].split("/") if part] or [file["name"]]
        node = root
        for folder in folders:
            node = node["folders"].setdefault(folder, _tree_node())
        node["files"].append((name, file))

    lines = []

    def walk(node, guides):
        entries = [
            (f":material/folder: {escape_markdown(name)}", None, child)
            for name, child in sorted(node["folders"].items(), key=lambda item: item[0].lower())
        ] + [(f"{file_icon(file)} {escape_markdown(name)}", file, None) for name, file in node["files"]]
        for i, (label, file, child) in enumerate(entries):
            last = i == len(entries) - 1
            lines.append(([*guides, "end" if last else "tee"], label, file))
            if child:
                walk(child, [*guides, "blank" if last else "pipe"])

    walk(root, [])
    return lines


def file_preview(file):
    if file["type"] == "image/svg+xml":
        st.image(file["data"].decode("utf-8", errors="replace"))
        return
    if file["type"].startswith("image/"):
        # Checked up front: some formats go to the browser as-is and would just show nothing.
        try:
            Image.open(io.BytesIO(file["data"])).verify()
        except Exception:  # noqa: BLE001 - a corrupt or mislabelled upload shouldn't break the board
            st.caption(":material/broken_image: This image can't be shown; download it instead.")
        else:
            st.image(file["data"])
        return
    if not is_text(file):
        st.caption("No preview for this type of file.")
        return
    text = file["data"][:PREVIEW_CHARS].decode("utf-8", errors="replace")
    st.code(text, language=None, wrap_lines=True, height=min(400, 40 + 22 * text.count("\n")))
    if file["size"] > PREVIEW_CHARS:
        st.caption("Preview cut off; download the file to see all of it.")


def attach_files_form(task):
    """An uploader for new attachments; returns True once files have been attached."""
    with st.form(f"attach_files_{task['id']}", border=False):
        uploads = st.file_uploader(
            "Files",
            accept_multiple_files=True,
            key=uploader_key(f"files_add_{task['id']}"),
            help='Files whose names contain "/" are shown in folders; rename them to move them.',
        )
        submitted = st.form_submit_button("Attach", icon=":material/attach_file:", type="primary")
    if not submitted:
        return False
    if not uploads:
        st.error("Choose at least one file.")
        return False
    task_files(task).extend(stored_files(uploads))
    reset_uploaders()
    return True


def _set_confirm_delete(key, value):
    st.session_state[key] = value


def file_details(task, file):
    """Preview, download, rename, replace and delete one file; returns True once it's deleted."""
    fid = file["id"]
    updated = datetime.fromtimestamp(file["updated_at"]).strftime("%Y-%m-%d %H:%M")
    st.markdown(f"**{file_icon(file)} {escape_markdown(file['name'])}**")
    st.caption(f"{file_size(file['size'])} · {file['type']} · updated {updated}")
    file_preview(file)

    with st.form(f"file_edit_{fid}"):
        name = st.text_input("Name", value=file["name"], help='Use "/" to put the file in a folder.')
        upload = st.file_uploader("Replace content (optional)", key=uploader_key(f"file_replace_{fid}"))
        if st.form_submit_button("Save", icon=":material/check:"):
            name = name.strip() or file["name"]
            if upload:
                if name == file["name"]:
                    # Keep the folder, but take the new file's name along with its content.
                    name = name[: name.rfind("/") + 1] + upload.name
                file.update({k: v for k, v in stored_file(upload).items() if k != "id"})
                reset_uploaders()
            if upload or name != file["name"]:
                file["name"] = name
                file["updated_at"] = time.time()
                # A full rerun, so the task card behind the dialog shows the change too.
                st.rerun()

    confirm_key = f"file_confirm_delete_{fid}"
    with st.container(horizontal=True, vertical_alignment="center"):
        st.download_button(
            "Download",
            # A callable defers sending the bytes until the button is clicked.
            data=lambda: file["data"],
            file_name=file["name"].rsplit("/", 1)[-1],
            mime=file["type"],
            on_click="ignore",
            icon=":material/download:",
        )
        st.space("stretch")
        if not st.session_state.get(confirm_key):
            st.button(
                "Delete",
                icon=":material/delete:",
                on_click=_set_confirm_delete,
                args=(confirm_key, True),
            )
            return False
        st.markdown("Delete this file?")
        st.button("Cancel", on_click=_set_confirm_delete, args=(confirm_key, False))
        if st.button("Delete", icon=":material/delete:", type="primary", key=f"file_delete_{fid}"):
            task["files"] = [f for f in task_files(task) if f["id"] != fid]
            del st.session_state[confirm_key]
            return True
    return False


def attach_files_button(task):
    """Shows how many files are attached; returns True when clicked."""
    count = len(task_files(task))
    return st.button(
        f":material/attach_file: {count}" if count else ":material/attach_file:",
        key=f"files_attach_{task['id']}",
        help="Attach files",
        type="tertiary",
    )


def files_tree(task):
    """The task's files as a tree of clickable names; returns the id of the file clicked, if any."""
    files = task_files(task)
    if not files:
        return None
    clicked = None
    # Keyed so the file names can be styled compactly and double-clicks here don't open the task
    # editor.
    with st.container(gap=None, key=f"file_tree_{task['id']}"):
        st.caption(":material/attach_file: Files")
        for guides, label, file in tree_lines(files):
            with st.container(horizontal=True, vertical_alignment="center", gap=None):
                spans = "".join(f'<span class="{guide}"></span>' for guide in guides)
                st.html(f'<div class="file-tree-guides">{spans}</div>', width="content")
                if file is None:
                    st.caption(label, width="content")
                elif st.button(label, key=f"file_open_{file['id']}", type="tertiary"):
                    clicked = file["id"]
                    # Start without a pending delete confirmation from an earlier visit.
                    st.session_state.pop(f"file_confirm_delete_{clicked}", None)
    return clicked
