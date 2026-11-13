"""Parse Apple Notes table CRDT format into a 2D grid.

Apple Notes stores tables as CRDT data in ZMERGEABLEDATA1 (gzip-compressed
protobuf, a MergeableData object graph). Field paths:

- 2.3.3: the objects (entries); an ObjectID's field 6 indexes into this list
- 2.3.4: key names (``crRows``, ``crColumns``, ``cellColumns``, ``UUIDIndex``)
- 2.3.5: type names (``com.apple.notes.ICTable``, ``com.apple.CRDT.NSUUID``)
- 2.3.6: uuids; a uuid object (field 13, type NSUUID) holds an index here

The ``ICTable`` object (field 13 map) points at ``crRows`` and ``crColumns``,
ordered sets (field 16) whose 16.1.1.2 array lists row/column uuids in visual
order and whose 16.1.2 dictionary maps each to the uuid ``cellColumns`` uses;
``cellColumns`` (field 6 dictionary) maps column uuid -> {row uuid -> cell},
and a cell (field 10) holds its text in 10.2.

Object order is storage order only: a paste rebuilds the table with its objects
reshuffled, so cells read in object order came back scrambled (issue #235).
"""

import copy
import gzip
from typing import Optional

from . import unified_log

try:
    import blackboxprotobuf
except ImportError:
    raise ImportError(
        "bbpb is required (provides the blackboxprotobuf module). "
        "Install with: pip install bbpb"
    )


def _message(fields: dict) -> dict:
    return {"type": "message", "message_typedef": fields, "name": ""}


_BYTES = {"type": "bytes", "name": ""}

# Field 2.3.3.10.2 is a cell's text. Left to guess, an empty one decodes as an
# empty message, and in a repeated field the first entry fixes the type for all:
# when an empty cell came first, every cell read as empty. A paste reshuffles op
# order, so that hit tables at random (issue #240). Same trap as #198. Bytes, not
# string, so a non-UTF-8 cell is replaced on decode rather than failing the table.
# The key, type and uuid lists and an ordered set's uuids (16.1.1.2.2) are pinned
# for the same reason: a random 16-byte uuid or a short name can parse as a
# message, and the walk compares them as bytes (#235).
_TABLE_PIN = {
    "2": _message(
        {
            "3": _message(
                {
                    "3": _message(
                        {
                            "10": _message({"2": _BYTES}),
                            "16": _message(
                                {
                                    "1": _message(
                                        {"1": _message({"2": _message({"2": _BYTES})})}
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


def parse_table_crdt(mergeabledata: bytes) -> list[list[str]] | None:
    """Parse a table CRDT blob into a 2D grid of cell strings.

    Args:
        mergeabledata: Raw bytes from ZMERGEABLEDATA1 column (gzip-compressed)

    Returns:
        List of rows, each row is a list of cell strings.
        First row may be headers. Returns None if parsing fails.
    """
    try:
        decompressed = gzip.decompress(mergeabledata)
        message, _ = blackboxprotobuf.decode_message(
            decompressed, copy.deepcopy(_TABLE_PIN)
        )

        # Navigate to the operations array: field 2.3.3
        field_2 = message.get("2", {})
        if not isinstance(field_2, dict):
            return None

        field_2_3 = field_2.get("3", {})
        if not isinstance(field_2_3, dict):
            return None

        ops = field_2_3.get("3", [])
        if not isinstance(ops, list):
            ops = [ops]

        grid = _grid_from_object_graph(field_2_3, ops)
        if grid is not None:
            return grid or None  # [] = a table with no rows or columns

        # No ICTable root at all: fall back to object order (right for an
        # unedited table, scrambled once a paste reshuffles it).
        # Separate cell-text ops (field 10) and ordering ops (field 6)
        cell_ops = []
        ordering_ops = []

        for op in ops:
            if not isinstance(op, dict):
                continue
            if "10" in op:
                cell_ops.append(op)
            if "6" in op:
                ordering_ops.append(op)

        if not cell_ops:
            return None

        cell_texts = [_cell_text(op) for op in cell_ops]

        # Determine column count from ordering ops
        # The first/smallest ordering op is the column list;
        # its length tells us the column count.
        col_count = _determine_column_count(ordering_ops, len(cell_texts))

        if col_count <= 0:
            return None

        # Arrange cells into rows
        row_count = len(cell_texts) // col_count
        if row_count <= 0:
            return None

        # Cells are stored in column-major order in the CRDT:
        # all cells for column 0 first, then column 1, etc.
        grid: list[list[str]] = []
        for r in range(row_count):
            row = []
            for c in range(col_count):
                idx = c * row_count + r  # column-major indexing
                if idx < len(cell_texts):
                    row.append(cell_texts[idx])
                else:
                    row.append("")
            grid.append(row)

        return grid

    except TableTooLarge:
        # Someone else's bytes (a shared note), not a fault in this reader:
        # reporting it would also use up the once-per-process canary below.
        return None
    except Exception as e:
        # Canary for systemic table-CRDT faults (#147): the constant note_ref
        # dedups this to one event per process, so a format change that
        # breaks EVERY table still surfaces without per-note spam. The
        # caller degrades to a "[table]" placeholder either way.
        unified_log.log_parse_error("parse_table", "unknown", e)
        return None


# Far above any table a person makes in Notes; bounds the grid a blob can ask for.
MAX_TABLE_CELLS = 100_000


class TableTooLarge(ValueError):
    """The ordering lists ask for a grid over MAX_TABLE_CELLS."""


class TableShapeError(ValueError):
    """An ICTable object graph that can't be walked. Raised, not answered with
    object order, which would put cells in the wrong places without a word."""


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _cell_text(entry) -> str:
    cell = entry.get("10") if isinstance(entry, dict) else None
    text = cell.get("2", b"") if isinstance(cell, dict) else b""
    if isinstance(text, bytes):
        return text.decode("utf-8", errors="replace")
    return text if isinstance(text, str) else ""


def _grid_from_object_graph(container: dict, entries: list) -> list[list[str]] | None:
    """Rebuild the grid from crRows / crColumns / cellColumns.

    Returns None when the blob has no ICTable root (the caller then falls back
    to object order) and [] for a table with no rows or columns. Raises
    TableShapeError for a root it can't walk and TableTooLarge over the cap.
    Follows apple_cloud_notes_parser's AppleNotesEmbeddedTable, except that a
    repeated uuid counts once (none of 96 real ordered sets repeats one).
    """
    keys = _as_list(container.get("4"))
    types = _as_list(container.get("5"))
    uuids = _as_list(container.get("6"))
    uuid_at: dict = {}
    for index, uuid in enumerate(uuids):
        if isinstance(uuid, bytes):
            uuid_at.setdefault(uuid, index)
    if b"com.apple.notes.ICTable" not in types:
        return None
    table_type = types.index(b"com.apple.notes.ICTable")

    def entry(oid):
        index = oid.get("6") if isinstance(oid, dict) else None
        if isinstance(index, int) and 0 <= index < len(entries):
            found = entries[index]
            return found if isinstance(found, dict) else None
        return None

    def uuid_index(oid):
        """The uuid-list index a uuid object (field 13 map) holds."""
        obj = entry(oid)
        custom = obj.get("13") if obj else None
        if not isinstance(custom, dict):
            return None
        first = _as_list(custom.get("3"))
        value = first[0].get("2") if first and isinstance(first[0], dict) else None
        index = value.get("2") if isinstance(value, dict) else None
        if not isinstance(index, int) or not 0 <= index < len(uuids):
            return None
        # The first copy of a uuid listed twice, as the ordered sets use
        return uuid_at.get(uuids[index])

    def dictionary_items(dictionary):
        """(key, value) ObjectID pairs of a Dictionary message."""
        if not isinstance(dictionary, dict):
            return []
        return [
            (item.get("1"), item.get("2"))
            for item in _as_list(dictionary.get("1"))
            if isinstance(item, dict)
        ]

    def dict_items(obj):
        """Pairs of an object holding a Dictionary (field 6)."""
        return dictionary_items(obj.get("6") if obj else None)

    def positions(name: bytes, obj) -> dict[int, int]:
        """uuid index -> visual position, from an ordered set (field 16)."""
        ordered = obj.get("16") if obj else None
        ordering = ordered.get("1") if isinstance(ordered, dict) else None
        array = ordering.get("1") if isinstance(ordering, dict) else None
        if not isinstance(array, dict):
            raise TableShapeError(f"{name.decode()} is not an ordered set")
        out: dict[int, int] = {}
        for item in _as_list(array.get("2")):
            index = uuid_at.get(item.get("2") if isinstance(item, dict) else None)
            if index is None:
                raise TableShapeError(f"{name.decode()} names an unknown uuid")
            if index not in out:  # a repeated uuid is still one row/column
                out[index] = len(out)
        for key, value in dictionary_items(ordering.get("2")):
            source, target = uuid_index(key), uuid_index(value)
            if source in out and target is not None:
                out[target] = out[source]
        return out

    root = None
    for candidate in entries:
        custom = candidate.get("13") if isinstance(candidate, dict) else None
        if isinstance(custom, dict) and custom.get("1") == table_type:
            root = custom
            break
    if root is None:
        raise TableShapeError("no ICTable root object")
    parts: dict[bytes, dict] = {}
    for item in _as_list(root.get("3")):
        key = item.get("1") if isinstance(item, dict) else None
        if isinstance(key, int) and 0 <= key < len(keys):
            parts[keys[key]] = entry(item.get("2"))

    row_of = positions(b"crRows", parts.get(b"crRows"))
    col_of = positions(b"crColumns", parts.get(b"crColumns"))
    cell_columns = parts.get(b"cellColumns")
    if cell_columns is None:
        raise TableShapeError("no cellColumns")
    if not row_of or not col_of:
        return []

    row_count = max(row_of.values()) + 1
    col_count = max(col_of.values()) + 1
    if row_count * col_count > MAX_TABLE_CELLS:
        # The ordering lists alone set the grid size, and a shared note's
        # table is someone else's bytes: refuse rather than allocate it.
        raise TableTooLarge(f"table grid {row_count}x{col_count} over the cap")
    grid = [[""] * col_count for _ in range(row_count)]
    for column_key, column_value in dict_items(cell_columns):
        col = col_of.get(uuid_index(column_key))
        if col is None:
            continue
        for row_key, cell in dict_items(entry(column_value)):
            row = row_of.get(uuid_index(row_key))
            if row is not None:
                grid[row][col] = _cell_text(entry(cell))
    return grid


def _determine_column_count(ordering_ops: list[dict], total_cells: int) -> int:
    """Determine column count from ordering operations.

    The ordering ops hold one column list (C entries) plus one row list per
    column (R entries each), where C * R == total_cells. C is therefore the
    op count minus one — validated against the sizes actually present, since
    min(sizes) picks min(C, R) and transposes any table with more columns
    than rows (NTG-016).
    """
    if not ordering_ops:
        # Fallback heuristic: try common column counts
        for cols in range(2, 11):
            if total_cells % cols == 0:
                return cols
        return 0

    # Count elements in each ordering op's field 6 array
    sizes = []
    for op in ordering_ops:
        field_6 = op.get("6", {})
        if isinstance(field_6, dict):
            # Count the entries in the ordering dict
            # Each entry represents an element in the linked list
            entries = field_6.get("1", [])
            if isinstance(entries, list):
                sizes.append(len(entries))
            elif entries:
                sizes.append(1)
        elif isinstance(field_6, list):
            sizes.append(len(field_6))

    if not sizes:
        # Fallback
        for cols in range(2, 11):
            if total_cells % cols == 0:
                return cols
        return 0

    # Expected structure: a column list of size C plus C row lists of size R.
    cols = len(sizes) - 1
    cols_divides = cols > 0 and total_cells % cols == 0
    if cols_divides:
        rows = total_cells // cols
        if sorted(sizes) == sorted([cols] + [rows] * cols):
            return cols

    # Exact sizes didn't match (edited CRDT with tombstone entries). The op
    # count is order-independent — one column list plus one row list per
    # column — so prefer it whenever it still divides the cell count, before
    # guessing from op sizes (a row list's size always divides C * R, so the
    # first-divisor guess transposes when a row list happens to come first).
    if cols_divides:
        return cols
    for size in sizes:
        if size > 0 and total_cells % size == 0:
            return size
    return min(sizes)


def format_table_as_markdown(
    grid: list[list[str]], header_row: Optional[int] = 0
) -> str:
    """Render a 2D grid as a markdown table.

    Args:
        grid: List of rows, each row is a list of cell strings
        header_row: Row index to use as header (default 0, None for no header)

    Returns:
        Markdown table string with | separators and --- separator line
    """
    if not grid or not grid[0]:
        return ""

    col_count = max(len(row) for row in grid)

    # Pad rows to uniform length
    padded = []
    for row in grid:
        padded_row = list(row) + [""] * (col_count - len(row))
        padded.append(padded_row)

    lines = []

    if header_row is not None and 0 <= header_row < len(padded):
        # Header row
        header = padded[header_row]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join("---" for _ in header) + " |")

        # Data rows (skip header)
        for i, row in enumerate(padded):
            if i == header_row:
                continue
            lines.append("| " + " | ".join(row) + " |")
    else:
        # No header — first row is still treated as header for markdown syntax
        lines.append("| " + " | ".join(padded[0]) + " |")
        lines.append("| " + " | ".join("---" for _ in padded[0]) + " |")
        for row in padded[1:]:
            lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)
