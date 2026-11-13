"""Tests for Apple Notes table CRDT parser."""

import copy
import gzip
from pathlib import Path

import blackboxprotobuf
import pytest

from apple_notes.table_parser import (
    _determine_column_count,
    format_table_as_markdown,
    parse_table_crdt,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _ordering_op(entry_count: int) -> dict:
    """Build an ordering op (field 6) with the given linked-list entry count."""
    return {"6": {"1": [{"1": i + 1} for i in range(entry_count)]}}


def _encode_table_crdt(cell_texts: list[str], ordering_ops: list[dict]) -> bytes:
    """Build a gzip-compressed table CRDT blob like ZMERGEABLEDATA1."""
    op_td = {
        "10": {
            "type": "message",
            "message_typedef": {"2": {"type": "bytes", "name": ""}},
            "name": "",
        },
        "6": {
            "type": "message",
            "message_typedef": {
                "1": {
                    "type": "message",
                    "message_typedef": {"1": {"type": "int", "name": ""}},
                    "name": "",
                },
            },
            "name": "",
        },
    }
    typedef = {
        "2": {
            "type": "message",
            "message_typedef": {
                "3": {
                    "type": "message",
                    "message_typedef": {
                        "3": {"type": "message", "message_typedef": op_td, "name": ""},
                    },
                    "name": "",
                },
            },
            "name": "",
        },
    }
    ops = list(ordering_ops) + [{"10": {"2": t.encode("utf-8")}} for t in cell_texts]
    message = {"2": {"3": {"3": ops}}}
    return gzip.compress(blackboxprotobuf.encode_message(message, typedef))


class TestFormatTableAsMarkdown:
    """Tests for markdown table rendering."""

    def test_simple_2x2(self) -> None:
        grid = [["A", "B"], ["1", "2"]]
        result = format_table_as_markdown(grid)
        lines = result.split("\n")
        assert lines[0] == "| A | B |"
        assert lines[1] == "| --- | --- |"
        assert lines[2] == "| 1 | 2 |"

    def test_3x3(self) -> None:
        grid = [["Name", "Age", "City"], ["Alice", "30", "NYC"], ["Bob", "25", "LA"]]
        result = format_table_as_markdown(grid)
        lines = result.split("\n")
        assert len(lines) == 4  # header + separator + 2 data rows
        assert "Name" in lines[0]
        assert "---" in lines[1]
        assert "Alice" in lines[2]
        assert "Bob" in lines[3]

    def test_empty_grid(self) -> None:
        assert format_table_as_markdown([]) == ""
        assert format_table_as_markdown([[]]) == ""

    def test_uneven_rows_padded(self) -> None:
        grid = [["A", "B", "C"], ["1", "2"]]
        result = format_table_as_markdown(grid)
        lines = result.split("\n")
        # Data row should have 3 columns (padded with empty)
        assert lines[2].count("|") == 4  # 3 cells = 4 pipes

    def test_no_header_row(self) -> None:
        grid = [["A", "B"], ["1", "2"]]
        result = format_table_as_markdown(grid, header_row=None)
        lines = result.split("\n")
        # Still uses first row as header (markdown requires it)
        assert lines[0] == "| A | B |"
        assert "---" in lines[1]

    def test_single_column(self) -> None:
        grid = [["Header"], ["Value"]]
        result = format_table_as_markdown(grid)
        lines = result.split("\n")
        assert lines[0] == "| Header |"
        assert lines[1] == "| --- |"
        assert lines[2] == "| Value |"


class TestParseTableCrdt:
    """Tests for CRDT blob parsing."""

    def test_none_on_invalid_data(self) -> None:
        assert parse_table_crdt(b"not valid data") is None

    def test_none_on_empty(self) -> None:
        assert parse_table_crdt(b"") is None

    def test_real_fixture_2x2(self) -> None:
        """Committed real-data 2x2 table fixture parses to the right grid."""
        data = (FIXTURES / "table_crdt_test_note_format_0.bin").read_bytes()
        assert parse_table_crdt(data) == [
            ["Table cell 1", "Table cell 2"],
            ["Table cell 3", "Table cell 4"],
        ]

    def test_wide_table_4x2(self) -> None:
        """A 4-column, 2-row table must not come back transposed (NTG-016).

        Ordering ops are one column list (4 entries) plus one row list per
        column (2 entries each); cells are stored column-major.
        """
        ordering = [_ordering_op(4)] + [_ordering_op(2)] * 4
        cells = ["A1", "A2", "B1", "B2", "C1", "C2", "D1", "D2"]
        data = _encode_table_crdt(cells, ordering)

        assert parse_table_crdt(data) == [
            ["A1", "B1", "C1", "D1"],
            ["A2", "B2", "C2", "D2"],
        ]

    def test_tall_table_2x4(self) -> None:
        """A 2-column, 4-row table keeps its shape."""
        ordering = [_ordering_op(2)] + [_ordering_op(4)] * 2
        cells = ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"]
        data = _encode_table_crdt(cells, ordering)

        assert parse_table_crdt(data) == [
            ["A1", "B1"],
            ["A2", "B2"],
            ["A3", "B3"],
            ["A4", "B4"],
        ]

    def test_empty_first_cell_keeps_the_other_cells(self) -> None:
        """An empty field reads as an empty message when types are guessed, and
        the first entry fixes the type for all: every cell came back empty.
        A paste reshuffles op order, so it hit real tables at random (#240)."""
        ordering = [_ordering_op(2)] + [_ordering_op(2)] * 2
        data = _encode_table_crdt(["", "A2", "B1", "B2"], ordering)

        assert parse_table_crdt(data) == [["", "B1"], ["A2", "B2"]]

    def test_non_utf8_cell_is_replaced_not_lost(self) -> None:
        ordering = [_ordering_op(2)] + [_ordering_op(1)] * 2
        op_td = {
            "10": {
                "type": "message",
                "message_typedef": {"2": {"type": "bytes", "name": ""}},
                "name": "",
            },
            "6": {
                "type": "message",
                "message_typedef": {
                    "1": {
                        "type": "message",
                        "message_typedef": {"1": {"type": "int", "name": ""}},
                        "name": "",
                    },
                },
                "name": "",
            },
        }
        typedef = {
            "2": {
                "type": "message",
                "message_typedef": {
                    "3": {
                        "type": "message",
                        "message_typedef": {
                            "3": {
                                "type": "message",
                                "message_typedef": op_td,
                                "name": "",
                            },
                        },
                        "name": "",
                    },
                },
                "name": "",
            },
        }
        ops = ordering + [{"10": {"2": b"\xff\xfe"}}, {"10": {"2": b"ok"}}]
        data = gzip.compress(
            blackboxprotobuf.encode_message({"2": {"3": {"3": ops}}}, typedef)
        )
        grid = parse_table_crdt(data)
        assert grid is not None and grid[0][1] == "ok"


class TestDetermineColumnCount:
    """Column count comes from the column-list op, not min(sizes) (NTG-016)."""

    def test_wide_table_uses_column_list_size(self) -> None:
        # 4 columns x 2 rows: column list of 4, then 4 row lists of 2
        ops = [_ordering_op(4)] + [_ordering_op(2)] * 4
        assert _determine_column_count(ops, total_cells=8) == 4

    def test_tall_table_uses_column_list_size(self) -> None:
        # 2 columns x 4 rows
        ops = [_ordering_op(2)] + [_ordering_op(4)] * 2
        assert _determine_column_count(ops, total_cells=8) == 2

    def test_square_table(self) -> None:
        # 2 columns x 2 rows (shape of the committed fixture)
        ops = [_ordering_op(2)] + [_ordering_op(2)] * 2
        assert _determine_column_count(ops, total_cells=4) == 2

    def test_no_ordering_ops_falls_back_to_divisor(self) -> None:
        assert _determine_column_count([], total_cells=4) == 2

    def test_tombstone_sizes_with_reordered_ops_use_op_count(self) -> None:
        """When exact sizes don't match (edited CRDT), the op count still
        identifies the column count regardless of op order (review finding).

        4x2 table: 5 ops -> 4 columns, even though a row list comes first
        (its size 2 divides 8) and one row list is tombstone-inflated to 3.
        """
        ops = [
            _ordering_op(2),  # row list first — not the column list
            _ordering_op(4),  # column list
            _ordering_op(3),  # row list with a tombstone entry
            _ordering_op(2),
            _ordering_op(2),
        ]
        assert _determine_column_count(ops, total_cells=8) == 4


# --- Real table format (issue #235) -------------------------------------------
#
# A table is a MergeableData object graph: an ICTable root whose map points at
# crRows and crColumns (ordered sets of row/column uuids) and cellColumns
# (column uuid -> row uuid -> cell). Op order is storage order only: a paste
# reshuffles it, and reading cells in op order scrambled fresh tables.

_BYTES = {"type": "bytes", "name": ""}
_INT = {"type": "int", "name": ""}


def _msg(fields: dict) -> dict:
    return {"type": "message", "message_typedef": fields, "name": ""}


_OID = _msg({"2": _INT, "6": _INT})
_DICT = _msg({"1": _msg({"1": _OID, "2": _OID})})
_TABLE_TYPEDEF = {
    "2": _msg(
        {
            "3": _msg(
                {
                    "3": _msg(
                        {
                            "6": _DICT,
                            "10": _msg({"2": _BYTES}),
                            "13": _msg({"1": _INT, "3": _msg({"1": _INT, "2": _OID})}),
                            "16": _msg(
                                {
                                    "1": _msg(
                                        {
                                            "1": _msg(
                                                {"2": _msg({"1": _INT, "2": _BYTES})}
                                            ),
                                            "2": _DICT,
                                        }
                                    )
                                }
                            ),
                        }
                    ),
                    "4": _BYTES,
                    "5": _BYTES,
                    "6": _BYTES,
                }
            )
        }
    )
}

_KEYS = [b"identity", b"cellColumns", b"UUIDIndex", b"crRows", b"crColumns", b"self"]
_TYPES = [
    b"com.apple.CRDT.NSNumber",
    b"com.apple.CRDT.NSUUID",
    b"com.apple.notes.ICTable",
]


def _build_table(grid: list[list[str]], seed: int) -> bytes:
    """Encode ``grid`` (visual rows) in the real table format, with uuids
    minted and objects stored in a shuffled order, as a paste leaves them."""
    import random

    rng = random.Random(seed)
    rows, cols = len(grid), len(grid[0])
    uuids: list[bytes] = []

    def new_uuid() -> int:
        uuids.append(rng.randbytes(16))
        return len(uuids) - 1

    # Each row/column has an ordering uuid (crRows/crColumns) mapped to the
    # identity uuid cellColumns uses. Mint them in a shuffled order so uuid
    # indices don't follow visual order either.
    slots = [("r", i) for i in range(rows)] + [("c", i) for i in range(cols)]
    rng.shuffle(slots)
    order_uuid, ident_uuid = {}, {}
    for slot in slots:
        order_uuid[slot], ident_uuid[slot] = new_uuid(), new_uuid()

    objects: dict[str, dict] = {}

    def ref(name: str) -> dict:
        return {"6": name}

    def uuid_obj(index: int) -> str:
        name = f"u{index}"
        objects[name] = {"13": {"1": 1, "3": [{"1": 2, "2": {"2": index}}]}}
        return name

    def ordered_set(kind: str, count: int) -> dict:
        return {
            "16": {
                "1": {
                    "1": {
                        "2": [
                            {"1": i, "2": uuids[order_uuid[(kind, i)]]}
                            for i in range(count)
                        ]
                    },
                    "2": {
                        "1": [
                            {
                                "1": ref(uuid_obj(order_uuid[(kind, i)])),
                                "2": ref(uuid_obj(ident_uuid[(kind, i)])),
                            }
                            for i in range(count)
                        ]
                    },
                }
            }
        }

    objects["rows"] = ordered_set("r", rows)
    objects["cols"] = ordered_set("c", cols)
    column_entries = []
    for c in range(cols):
        row_entries = []
        for r in range(rows):
            objects[f"cell{r},{c}"] = {"10": {"2": grid[r][c].encode("utf-8")}}
            row_entries.append(
                {"1": ref(uuid_obj(ident_uuid[("r", r)])), "2": ref(f"cell{r},{c}")}
            )
        objects[f"col{c}"] = {"6": {"1": row_entries}}
        column_entries.append(
            {"1": ref(uuid_obj(ident_uuid[("c", c)])), "2": ref(f"col{c}")}
        )
    objects["cellColumns"] = {"6": {"1": column_entries}}
    objects["root"] = {
        "13": {
            "1": 2,
            "3": [
                {"1": 1, "2": ref("cellColumns")},
                {"1": 3, "2": ref("rows")},
                {"1": 4, "2": ref("cols")},
            ],
        }
    }

    names = list(objects)
    rng.shuffle(names)
    index = {name: i for i, name in enumerate(names)}

    def resolve(value):
        if isinstance(value, dict):
            return {
                k: index[v] if k == "6" and isinstance(v, str) else resolve(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [resolve(v) for v in value]
        return value

    entries = [resolve(objects[name]) for name in names]
    message = {"2": {"3": {"3": entries, "4": _KEYS, "5": _TYPES, "6": uuids}}}
    return gzip.compress(blackboxprotobuf.encode_message(message, _TABLE_TYPEDEF))


def _permute_fixture(seed: int) -> bytes:
    """The committed real table with its objects stored in another order."""
    import random

    from apple_notes.table_parser import _TABLE_PIN

    data = gzip.decompress(
        (FIXTURES / "table_crdt_test_note_format_0.bin").read_bytes()
    )
    message, typedef = blackboxprotobuf.decode_message(data, copy.deepcopy(_TABLE_PIN))
    entries = message["2"]["3"]["3"]
    order = list(range(len(entries)))
    random.Random(seed).shuffle(order)
    new_index = {old: new for new, old in enumerate(order)}

    def remap(value):
        if isinstance(value, dict):
            return {
                k: new_index[v] if k == "6" and isinstance(v, int) else remap(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [remap(v) for v in value]
        return value

    message["2"]["3"]["3"] = [remap(entries[old]) for old in order]
    return gzip.compress(blackboxprotobuf.encode_message(message, typedef))


class TestRealTableFormat:
    """Cell positions come from crRows/crColumns/cellColumns (#235)."""

    @pytest.mark.parametrize("seed", range(8))
    def test_real_fixture_in_any_object_order_reads_the_same(self, seed) -> None:
        assert parse_table_crdt(_permute_fixture(seed)) == [
            ["Table cell 1", "Table cell 2"],
            ["Table cell 3", "Table cell 4"],
        ]

    @pytest.mark.parametrize("seed", range(8))
    @pytest.mark.parametrize(
        "grid",
        [
            [["a", "b"], ["1", "2"]],
            [["x", "y", "z"], ["3", "4", "5"]],
            [["h1", "h2"], ["r1a", "r1b"], ["r2a", "r2b"]],
            [["only", "one", "row", "here"]],
            [["solo"], ["col"]],
        ],
    )
    def test_shuffled_table_reads_in_visual_order(self, grid, seed) -> None:
        assert parse_table_crdt(_build_table(grid, seed)) == grid

    def test_empty_cells_keep_their_place(self) -> None:
        grid = [["", "b"], ["c", ""]]
        assert parse_table_crdt(_build_table(grid, seed=3)) == grid


def _ordering_only_table(row_uuids: list[int], col_uuids: list[int], uuid_count: int) -> bytes:
    """A table whose crRows / crColumns list the given uuid indices, with no cells."""

    def ordered_set(indices: list[int]) -> dict:
        return {"16": {"1": {"1": {"2": [{"1": i, "2": uuids[u]} for i, u in enumerate(indices)]}}}}

    uuids = [i.to_bytes(16, "big") for i in range(uuid_count)]
    entries = [
        {"13": {"1": 2, "3": [{"1": 1, "2": {"6": 1}}, {"1": 3, "2": {"6": 2}}, {"1": 4, "2": {"6": 3}}]}},
        {"6": {"1": []}},
        ordered_set(row_uuids),
        ordered_set(col_uuids),
    ]
    message = {"2": {"3": {"3": entries, "4": _KEYS, "5": _TYPES, "6": uuids}}}
    return gzip.compress(blackboxprotobuf.encode_message(message, _TABLE_TYPEDEF))


class TestHostileTableShape:
    """Grid size comes from the ordering lists, which a shared note controls."""

    def test_huge_row_and_column_lists_are_refused(self) -> None:
        n = 3000
        data = _ordering_only_table(list(range(n)), list(range(n, 2 * n)), 2 * n)
        assert parse_table_crdt(data) is None

    def test_a_repeated_uuid_is_one_row(self) -> None:
        data = _ordering_only_table([0, 0, 0], [1], 2)
        assert parse_table_crdt(data) == [[""]]


def _with_unknown_row_uuid(data: bytes) -> bytes:
    """A real-format table whose first ordered set names a uuid not in the list."""
    message, typedef = blackboxprotobuf.decode_message(
        gzip.decompress(data), copy.deepcopy(_TABLE_TYPEDEF)
    )
    for entry in message["2"]["3"]["3"]:
        if "16" in entry:
            first = entry["16"]["1"]["1"]["2"]
            (first[0] if isinstance(first, list) else first)["2"] = b"\x00" * 16
            break
    return gzip.compress(blackboxprotobuf.encode_message(message, typedef))


@pytest.fixture
def parse_errors(monkeypatch):
    from apple_notes import table_parser

    seen = []
    monkeypatch.setattr(
        table_parser.unified_log, "log_parse_error", lambda *args: seen.append(args)
    )
    return seen


class TestRefusals:
    """A known table shape is never read in object order (#235 review)."""

    def test_broken_known_shape_is_refused_and_reported(self, parse_errors) -> None:
        data = _with_unknown_row_uuid(_build_table([["a", "b"], ["1", "2"]], seed=1))
        assert parse_table_crdt(data) is None
        assert len(parse_errors) == 1

    def test_over_cap_table_is_refused_without_a_fault_report(self, parse_errors) -> None:
        n = 3000
        data = _ordering_only_table(list(range(n)), list(range(n, 2 * n)), 2 * n)
        assert parse_table_crdt(data) is None
        assert parse_errors == []

    def test_empty_table_is_none_without_a_fault_report(self, parse_errors) -> None:
        assert parse_table_crdt(_ordering_only_table([], [], 0)) is None
        assert parse_errors == []


def test_uuid_object_pointing_at_a_repeated_list_entry_keeps_its_cells() -> None:
    """The 2.3.6 list may hold a uuid twice; a uuid object naming the second
    copy is the same row or column, not an unknown one (#235 review round 2)."""
    data = _build_table([["a", "b"], ["1", "2"]], seed=2)
    message, typedef = blackboxprotobuf.decode_message(
        gzip.decompress(data), copy.deepcopy(_TABLE_TYPEDEF)
    )
    table = message["2"]["3"]
    uuids = table["6"]
    for entry in table["3"]:
        custom = entry.get("13")
        if custom and custom["1"] == 1:  # every NSUUID object -> a fresh copy
            ref = custom["3"][0]["2"] if isinstance(custom["3"], list) else custom["3"]["2"]
            uuids.append(uuids[ref["2"]])
            ref["2"] = len(uuids) - 1
    data = gzip.compress(blackboxprotobuf.encode_message(message, typedef))
    assert parse_table_crdt(data) == [["a", "b"], ["1", "2"]]
