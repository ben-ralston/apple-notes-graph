#!/usr/bin/env python3
"""Capture table CRDT data from Apple Notes for test fixtures.

Usage:
    uv run python scripts/capture_table_crdt.py "Note Name" [output_prefix]

Finds all table attachments in the named note, saves raw ZMERGEABLEDATA1 bytes
to tests/fixtures/ as .bin files, and pretty-prints the decoded protobuf
structure for analysis.

Requires Full Disk Access for the running terminal.
"""

import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from apple_notes.database import NotesDatabase

try:
    import blackboxprotobuf
except ImportError:
    print("ERROR: bbpb is required. Install with: uv add bbpb")
    sys.exit(1)

import gzip


FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures"


def capture_table_crdt(note_name: str, output_prefix: str | None = None) -> None:
    """Find table attachments for a note and save their CRDT data."""
    prefix = output_prefix or note_name.lower().replace(" ", "_")

    with NotesDatabase() as db:
        # Find the note
        notes = db.get_all_notes()
        matching = [n for n in notes if n.name == note_name]
        if not matching:
            # Try case-insensitive
            matching = [n for n in notes if n.name.lower() == note_name.lower()]
        if not matching:
            print(f"Note '{note_name}' not found. Available notes:")
            for n in sorted(notes, key=lambda x: x.name)[:20]:
                print(f"  - {n.name}")
            sys.exit(1)

        note = matching[0]
        print(f"Found note: {note.name} (pk={note.pk}, folder={note.folder})")

        # Find table attachments from note's protobuf data
        from apple_notes.protobuf_parser import NoteProtobufParser

        proto = db.get_note_data(note.pk)
        parser = NoteProtobufParser()
        text, raw_runs = parser.parse(proto)
        infos = parser.extract_attachment_infos(raw_runs)
        table_infos = [
            info for info in infos if info.type_uti == "com.apple.notes.table"
        ]

        if not table_infos:
            print("No table attachments found in this note.")
            print("\nTip: Create a table in Notes.app and try again.")
            sys.exit(1)

        print(f"Found {len(table_infos)} table attachment(s)")

        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

        for i, info in enumerate(table_infos):
            uuid_val = info.identifier
            crdt_data = db.get_table_data(uuid_val)
            if crdt_data is None:
                print(f"\n  [{i}] {uuid_val}: No CRDT data found")
                continue

            # Save raw bytes
            filename = f"table_crdt_{prefix}_{i}.bin"
            filepath = FIXTURES_DIR / filename
            filepath.write_bytes(crdt_data)
            print(f"\nSaved: {filepath} ({len(crdt_data)} bytes)")

            # Decode and pretty-print
            try:
                decompressed = gzip.decompress(crdt_data)
                message, typedef = blackboxprotobuf.decode_message(decompressed)

                print(f"  UUID: {uuid_val}")
                print(f"  Decompressed: {len(decompressed)} bytes")

                # Navigate to operations array
                field_2 = message.get("2", {})
                if isinstance(field_2, dict):
                    field_2_3 = field_2.get("3", {})
                    if isinstance(field_2_3, dict):
                        ops = field_2_3.get("3", [])
                        if not isinstance(ops, list):
                            ops = [ops]

                        cell_ops = [
                            op for op in ops if isinstance(op, dict) and "10" in op
                        ]
                        ordering_ops = [
                            op for op in ops if isinstance(op, dict) and "6" in op
                        ]

                        print(f"  Total ops: {len(ops)}")
                        print(f"  Cell ops (field 10): {len(cell_ops)}")
                        print(f"  Ordering ops (field 6): {len(ordering_ops)}")

                        # Print cell text
                        print("\n  Cell texts (raw order):")
                        for j, op in enumerate(cell_ops):
                            cell_data = op.get("10", {})
                            text = (
                                cell_data.get("2", "")
                                if isinstance(cell_data, dict)
                                else ""
                            )
                            if isinstance(text, bytes):
                                text = text.decode("utf-8", errors="replace")
                            print(f"    [{j}] {repr(text)}")

                        # Print ordering ops detail
                        print("\n  Ordering ops detail:")
                        for j, op in enumerate(ordering_ops):
                            field_6 = op.get("6", {})
                            print(f"    Ordering op {j}:")
                            if isinstance(field_6, dict):
                                entries = field_6.get("1", [])
                                if not isinstance(entries, list):
                                    entries = [entries]
                                print(f"      Entries (field 6.1): {len(entries)}")
                                for k, entry in enumerate(entries):
                                    if isinstance(entry, dict):
                                        print(
                                            f"        [{k}] {json.dumps(_serialize_entry(entry), indent=8)}"
                                        )
                                    else:
                                        print(f"        [{k}] {repr(entry)}")
                            else:
                                print(f"      Raw: {repr(field_6)}")

                # Save decoded JSON for analysis
                json_filename = f"table_crdt_{prefix}_{i}_decoded.json"
                json_filepath = FIXTURES_DIR / json_filename
                json_filepath.write_text(
                    json.dumps(_make_serializable(message), indent=2)
                )
                print(f"\n  Decoded JSON: {json_filepath}")

            except Exception as e:
                print(f"  Decode error: {e}")


def _serialize_entry(entry: dict) -> dict:
    """Make a protobuf entry JSON-serializable."""
    result = {}
    for k, v in entry.items():
        if isinstance(v, bytes):
            result[k] = v.hex()
        elif isinstance(v, dict):
            result[k] = _serialize_entry(v)
        elif isinstance(v, list):
            result[k] = [
                _serialize_entry(x)
                if isinstance(x, dict)
                else (x.hex() if isinstance(x, bytes) else x)
                for x in v
            ]
        else:
            result[k] = v
    return result


def _make_serializable(obj):
    """Recursively make a protobuf message JSON-serializable."""
    if isinstance(obj, dict):
        return {k: _make_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_make_serializable(x) for x in obj]
    elif isinstance(obj, bytes):
        return obj.hex()
    return obj


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    note_name = sys.argv[1]
    output_prefix = sys.argv[2] if len(sys.argv) > 2 else None
    capture_table_crdt(note_name, output_prefix)
