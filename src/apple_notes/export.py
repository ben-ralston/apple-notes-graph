"""
Export helpers for notes and graph data (issue #79).

Pure conversion logic: note content to Markdown, and graph data (as
produced by get_graph_data's "full" format) to JSON, GraphViz DOT, or
CSV edge-list strings. File I/O lives in the MCP server tools.
"""

import csv
import io
import json

from .models import NoteContent

# Filesystem-safe cap; leaves room for a collision suffix and extension
# within common 255-byte filename limits.
MAX_FILENAME_LENGTH = 200

_UNSAFE_CHARS = str.maketrans({"/": "-", ":": "-", "\\": "-"})


def safe_filename(name: str, max_length: int = MAX_FILENAME_LENGTH) -> str:
    """
    Convert a note name into a safe filename stem (no extension).

    Replaces path separators and colons with dashes, drops control
    characters, strips leading dots (hidden files) and surrounding
    whitespace, and truncates to max_length. Falls back to "Untitled"
    when nothing usable remains.
    """
    cleaned = name.translate(_UNSAFE_CHARS)
    cleaned = "".join(c for c in cleaned if ord(c) >= 32)
    cleaned = cleaned.strip().lstrip(".").strip()
    cleaned = cleaned[:max_length].strip()
    return cleaned or "Untitled"


def note_to_markdown(content: NoteContent) -> str:
    """
    Render a note as a Markdown document.

    The note name becomes an H1 heading; the body (already valid
    Markdown with [[links]] resolved) follows after a blank line.
    The first line of resolved_text is the title line — dropped rather
    than echoed, because heading-styled titles render there with a
    leading "# " and would double up (e.g. "# # Pact").
    """
    lines = content.resolved_text.split("\n")
    body = "\n".join(lines[1:]).strip("\n")
    title = content.name
    if not body:
        return f"# {title}\n"
    return f"# {title}\n\n{body}\n"


def graph_to_json(data: dict) -> str:
    """
    Convert graph data to D3/vis.js-compatible JSON ({nodes, links}).

    Nodes use "id" as the identifier key, which both D3's force layout
    and vis.js expect.
    """
    output = {
        "nodes": [{"id": n["name"], "folder": n["folder"]} for n in data["notes"]],
        "links": data["links"],
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


def _dot_quote(name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def graph_to_dot(data: dict) -> str:
    """Convert graph data to GraphViz DOT format (directed graph)."""
    lines = ["digraph notes {"]
    for note in data["notes"]:
        lines.append(f"  {_dot_quote(note['name'])};")
    for link in data["links"]:
        lines.append(f"  {_dot_quote(link['source'])} -> {_dot_quote(link['target'])};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def graph_to_csv(data: dict) -> str:
    """Convert graph links to a CSV edge list with a source,target header."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["source", "target"])
    for link in data["links"]:
        writer.writerow([link["source"], link["target"]])
    return buffer.getvalue()


GRAPH_EXPORTERS = {
    "json": graph_to_json,
    "dot": graph_to_dot,
    "csv": graph_to_csv,
}
