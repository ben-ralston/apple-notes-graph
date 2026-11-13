"""Tests for graph traversal and formatting in the apple_notes library."""
import pytest

from apple_notes import (
    Note,
    GraphLink,
    NoteNotFoundError,
    InvalidGraphParameterError,
    get_graph_data,
)
from apple_notes import _bfs_subgraph, _format_graph_output


# Sample test data for BFS tests
def make_notes():
    """Create sample notes for testing.

    Graph structure:
        A -- B -- C -- D
        |         |
        E         F

        G (isolated)
    """
    return [
        Note(name="A", uuid="a", folder="Notes", pk=1),
        Note(name="B", uuid="b", folder="Notes", pk=2),
        Note(name="C", uuid="c", folder="Notes", pk=3),
        Note(name="D", uuid="d", folder="Notes", pk=4),
        Note(name="E", uuid="e", folder="Notes", pk=5),
        Note(name="F", uuid="f", folder="Work", pk=6),
        Note(name="G", uuid="g", folder="Notes", pk=7),  # Isolated
    ]


def make_links():
    """Create sample links for testing."""
    return [
        GraphLink(source_name="A", target_name="B", source_pk=1, target_pk=2),
        GraphLink(source_name="B", target_name="C", source_pk=2, target_pk=3),
        GraphLink(source_name="C", target_name="D", source_pk=3, target_pk=4),
        GraphLink(source_name="A", target_name="E", source_pk=1, target_pk=5),
        GraphLink(source_name="C", target_name="F", source_pk=3, target_pk=6),
    ]


class TestBFSSubgraph:
    """Tests for BFS graph traversal logic."""

    def test_depth_1_returns_direct_connections(self):
        """Depth 1 should return only direct neighbors."""
        notes = make_notes()
        links = make_links()

        result_notes, result_links = _bfs_subgraph(notes, links, "A", max_depth=1)

        result_names = {n.name for n in result_notes}
        assert result_names == {"A", "B", "E"}

        # Should only include links within the subgraph
        assert len(result_links) == 2  # A-B and A-E

    def test_depth_2_returns_two_hops(self):
        """Depth 2 should return nodes within 2 hops."""
        notes = make_notes()
        links = make_links()

        result_notes, result_links = _bfs_subgraph(notes, links, "A", max_depth=2)

        result_names = {n.name for n in result_notes}
        # From A: depth 1 = B, E; depth 2 = C (via B)
        assert result_names == {"A", "B", "E", "C"}

    def test_unlimited_depth_returns_connected_component(self):
        """No depth limit should return entire connected component."""
        notes = make_notes()
        links = make_links()

        result_notes, result_links = _bfs_subgraph(notes, links, "A", max_depth=None)

        result_names = {n.name for n in result_notes}
        # Should get entire connected component (A, B, C, D, E, F) but not G (isolated)
        assert result_names == {"A", "B", "C", "D", "E", "F"}

    def test_start_from_leaf_node(self):
        """Starting from leaf should work correctly."""
        notes = make_notes()
        links = make_links()

        result_notes, result_links = _bfs_subgraph(notes, links, "D", max_depth=1)

        result_names = {n.name for n in result_notes}
        # D only connects to C
        assert result_names == {"D", "C"}

    def test_start_note_not_found(self):
        """Should raise NoteNotFoundError for invalid start note."""
        notes = make_notes()
        links = make_links()

        with pytest.raises(NoteNotFoundError) as exc_info:
            _bfs_subgraph(notes, links, "Nonexistent", max_depth=1)

        assert "Nonexistent" in str(exc_info.value)

    def test_isolated_node(self):
        """Should return just the node if it has no connections."""
        notes = make_notes()
        links = make_links()

        result_notes, result_links = _bfs_subgraph(notes, links, "G", max_depth=5)

        result_names = {n.name for n in result_notes}
        assert result_names == {"G"}
        assert len(result_links) == 0

    def test_links_filtered_to_subgraph(self):
        """Links should only include edges within the subgraph."""
        notes = make_notes()
        links = make_links()

        result_notes, result_links = _bfs_subgraph(notes, links, "A", max_depth=1)

        # Only A-B and A-E should be included, not B-C
        link_pairs = {(l.source_name, l.target_name) for l in result_links}
        assert ("A", "B") in link_pairs
        assert ("A", "E") in link_pairs
        assert ("B", "C") not in link_pairs

    def test_bidirectional_traversal(self):
        """BFS should treat links as undirected (follow in both directions)."""
        notes = make_notes()
        links = make_links()

        # Start from D and go backwards through the link C->D
        result_notes, result_links = _bfs_subgraph(notes, links, "D", max_depth=2)

        result_names = {n.name for n in result_notes}
        # D -> C (depth 1), C -> B, F (depth 2)
        assert result_names == {"D", "C", "B", "F"}

    def test_preserves_note_order(self):
        """Result should preserve original note order."""
        notes = make_notes()
        links = make_links()

        result_notes, result_links = _bfs_subgraph(notes, links, "A", max_depth=None)

        # Should be in original order: A, B, C, D, E, F (not G)
        result_names = [n.name for n in result_notes]
        expected_order = ["A", "B", "C", "D", "E", "F"]
        assert result_names == expected_order


class TestFormatGraphOutput:
    """Tests for output format transformations."""

    def test_full_format(self):
        """Full format should match original output structure."""
        notes = [
            Note(name="Note A", uuid="a", folder="Work", pk=1),
            Note(name="Note B", uuid="b", folder="Personal", pk=2),
        ]
        links = [
            GraphLink(source_name="Note A", target_name="Note B", source_pk=1, target_pk=2),
        ]

        result = _format_graph_output(notes, links, "full")

        assert result["notes"] == [
            {"name": "Note A", "folder": "Work"},
            {"name": "Note B", "folder": "Personal"},
        ]
        assert result["links"] == [
            {"source": "Note A", "target": "Note B"},
        ]
        assert result["stats"] == {"total_notes": 2, "total_links": 1}

    def test_links_only_format(self):
        """Links only format should omit note details."""
        notes = [
            Note(name="Note A", uuid="a", folder="Work", pk=1),
            Note(name="Note B", uuid="b", folder="Personal", pk=2),
        ]
        links = [
            GraphLink(source_name="Note A", target_name="Note B", source_pk=1, target_pk=2),
        ]

        result = _format_graph_output(notes, links, "links_only")

        assert "notes" not in result
        assert "folders" not in result
        assert result["links"] == [{"source": "Note A", "target": "Note B"}]
        assert result["stats"] == {"total_links": 1}

    def test_empty_graph(self):
        """Should handle empty graph gracefully."""
        result = _format_graph_output([], [], "full")

        assert result["notes"] == []
        assert result["links"] == []
        assert result["stats"] == {"total_notes": 0, "total_links": 0}

    def test_invalid_format_raises_error(self):
        """Should raise InvalidGraphParameterError for invalid format."""
        notes = [Note(name="A", uuid="a", folder="Notes", pk=1)]
        links = []

        with pytest.raises(InvalidGraphParameterError) as exc_info:
            _format_graph_output(notes, links, "invalid")

        assert "invalid" in str(exc_info.value).lower()


class TestGetGraphDataValidation:
    """Tests for get_graph_data parameter validation."""

    def test_depth_without_start_note_raises_error(self):
        """Should raise InvalidGraphParameterError when depth is given without start_note."""
        with pytest.raises(InvalidGraphParameterError) as exc_info:
            # Use a mock db_path that won't be reached due to validation
            get_graph_data(depth=2, db_path="/nonexistent")

        assert "start_note" in str(exc_info.value)
