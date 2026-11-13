"""Tests for the export module (issue #79)."""

import csv
import io
import json

from apple_notes import NoteContent
from apple_notes.export import (
    safe_filename,
    note_to_markdown,
    graph_to_json,
    graph_to_dot,
    graph_to_csv,
)


def make_content(
    name="Meeting Notes",
    body="Discuss [[Project Ideas]] and tasks",
    title_line=None,
):
    first_line = title_line if title_line is not None else name
    text = f"{first_line}\n{body}" if body else first_line
    return NoteContent(
        name=name,
        uuid="uuid-1",
        folder="Work",
        raw_text=text,
        resolved_text=text,
        note_links=[],
        external_links=[],
        attachments=[],
        created="2024-01-15T10:30:00Z",
        modified="2025-01-05T14:22:00Z",
        last_opened=None,
    )


SAMPLE_GRAPH = {
    "notes": [
        {"name": "Note A", "folder": "Work"},
        {"name": "Note B", "folder": "Personal"},
    ],
    "links": [
        {"source": "Note A", "target": "Note B"},
    ],
    "stats": {"total_notes": 2, "total_links": 1},
}


class TestSafeFilename:
    def test_plain_name_unchanged(self):
        assert safe_filename("Meeting Notes") == "Meeting Notes"

    def test_slash_replaced(self):
        assert "/" not in safe_filename("Home/Garden Plan")

    def test_colon_replaced(self):
        assert ":" not in safe_filename("Project: Alpha")

    def test_control_chars_removed(self):
        assert safe_filename("Bad\x00Name\x1f") == "BadName"

    def test_empty_name_falls_back_to_untitled(self):
        assert safe_filename("") == "Untitled"

    def test_name_of_only_unsafe_chars_falls_back(self):
        assert safe_filename("...") == "Untitled"

    def test_leading_dot_stripped(self):
        assert not safe_filename(".hidden note").startswith(".")

    def test_long_name_truncated(self):
        assert len(safe_filename("x" * 500)) <= 200


class TestNoteToMarkdown:
    def test_title_becomes_heading(self):
        md = note_to_markdown(make_content())
        assert md.startswith("# Meeting Notes\n")

    def test_body_preserved_with_links(self):
        md = note_to_markdown(make_content())
        assert "Discuss [[Project Ideas]] and tasks" in md

    def test_blank_line_between_title_and_body(self):
        md = note_to_markdown(make_content())
        assert md.startswith("# Meeting Notes\n\nDiscuss")

    def test_ends_with_single_newline(self):
        md = note_to_markdown(make_content())
        assert md.endswith("\n")
        assert not md.endswith("\n\n")

    def test_title_only_note(self):
        md = note_to_markdown(make_content(body=""))
        assert md == "# Meeting Notes\n"

    def test_title_line_already_a_heading_is_not_doubled(self):
        # Live bug: heading-styled titles render as "# Pact" in
        # resolved_text, which produced "# # Pact" on export.
        md = note_to_markdown(
            make_content(name="Pact", body="body text", title_line="# Pact")
        )
        assert md.startswith("# Pact\n\nbody text")
        assert "# #" not in md

    def test_literal_hash_in_note_name_preserved(self):
        md = note_to_markdown(
            make_content(name="#1 Priorities", body="body", title_line="#1 Priorities")
        )
        assert md.startswith("# #1 Priorities\n")


class TestGraphToJson:
    def test_nodes_use_id_key(self):
        data = json.loads(graph_to_json(SAMPLE_GRAPH))
        assert data["nodes"] == [
            {"id": "Note A", "folder": "Work"},
            {"id": "Note B", "folder": "Personal"},
        ]

    def test_links_have_source_and_target(self):
        data = json.loads(graph_to_json(SAMPLE_GRAPH))
        assert data["links"] == [{"source": "Note A", "target": "Note B"}]


class TestGraphToDot:
    def test_digraph_structure(self):
        dot = graph_to_dot(SAMPLE_GRAPH)
        assert dot.startswith("digraph notes {")
        assert dot.rstrip().endswith("}")

    def test_nodes_and_edges_present(self):
        dot = graph_to_dot(SAMPLE_GRAPH)
        assert '"Note A";' in dot
        assert '"Note B";' in dot
        assert '"Note A" -> "Note B";' in dot

    def test_quotes_in_names_escaped(self):
        graph = {
            "notes": [{"name": 'The "Big" Idea', "folder": "Work"}],
            "links": [],
            "stats": {"total_notes": 1, "total_links": 0},
        }
        dot = graph_to_dot(graph)
        assert '"The \\"Big\\" Idea";' in dot


class TestGraphToCsv:
    def test_header_and_edge_rows(self):
        rows = list(csv.reader(io.StringIO(graph_to_csv(SAMPLE_GRAPH))))
        assert rows[0] == ["source", "target"]
        assert rows[1] == ["Note A", "Note B"]

    def test_comma_in_name_is_quoted(self):
        graph = {
            "notes": [],
            "links": [{"source": "Notes, Ideas", "target": "Note B"}],
            "stats": {"total_notes": 0, "total_links": 1},
        }
        rows = list(csv.reader(io.StringIO(graph_to_csv(graph))))
        assert rows[1] == ["Notes, Ideas", "Note B"]
