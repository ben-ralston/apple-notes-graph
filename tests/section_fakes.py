"""Test doubles for sections / section-edit tests (issue #184)."""

import base64
import gzip
import json
from pathlib import Path

import blackboxprotobuf

FIXTURE = Path(__file__).parent / "fixtures" / "section_spike_note.json"
LINK_UTI = "com.apple.notes.inlinetextattachment.link"
TABLE_UTI = "com.apple.notes.table"

_STYLE_TD = {
    "1": {"type": "int", "name": ""},
    "3": {"type": "int", "name": ""},
    "4": {"type": "int", "name": ""},
    "8": {"type": "int", "name": ""},
    "9": {"type": "bytes", "name": ""},
    "5": {
        "type": "message",
        "message_typedef": {
            "1": {"type": "bytes", "name": ""},
            "2": {"type": "int", "name": ""},
        },
        "name": "",
    },
}
_RUN_TD = {
    "1": {"type": "int", "name": ""},
    "2": {"type": "message", "message_typedef": _STYLE_TD, "name": ""},
    "12": {
        "type": "message",
        "message_typedef": {
            "1": {"type": "string", "name": ""},
            "2": {"type": "string", "name": ""},
        },
        "name": "",
    },
    "13": {"type": "int", "name": ""},
}
_TYPEDEF = {
    "2": {
        "type": "message",
        "message_typedef": {
            "3": {
                "type": "message",
                "message_typedef": {
                    "2": {"type": "bytes", "name": ""},
                    "5": {"type": "message", "message_typedef": _RUN_TD, "name": ""},
                },
                "name": "",
            },
        },
        "name": "",
    },
}


def para(
    text,
    ptype=None,
    *,
    ts=1,
    uuid=b"u",
    atts=(),
    checked=None,
    indent=None,
    quote=False,
    no_style=False,
):
    """One paragraph spec.  ``text`` has no trailing newline; U+FFFC marks each
    attachment in ``atts`` ((identifier, uti) pairs, in order).  ``no_style``
    writes runs with no field 2 (otherwise field 2 carries at least the uuid)."""
    return dict(
        text=text, ptype=ptype, ts=ts, uuid=uuid, atts=list(atts),
        checked=checked, indent=indent, quote=quote, no_style=no_style,
    )  # fmt: skip


def encode_note(paragraphs) -> bytes:
    """ZDATA for ``paragraphs`` (see ``para``), one newline-joined text, with
    field-2 paragraph styles (type, indent, checked, quote, uuid) and field 13."""
    pieces, runs = [], []
    for n, p in enumerate(paragraphs):
        piece = p["text"] + ("\n" if n < len(paragraphs) - 1 else "")
        pieces.append(piece)
        style = {"9": p["uuid"]}
        if p["ptype"] is not None:
            style["1"] = p["ptype"]
        if p["checked"] is not None:
            style["5"] = {"1": b"c", "2": int(p["checked"])}
        if p["indent"]:
            style["4"] = p["indent"]
        if p["quote"]:
            style["8"] = 1
        styled = {} if p.get("no_style") else {"2": style}
        atts = iter(p["atts"])
        segment = ""
        for ch in piece:
            if ch == "￼":
                if segment:
                    runs.append({"1": len(segment), **styled, "13": p["ts"]})
                    segment = ""
                ident, uti = next(atts)
                runs.append({
                    "1": 1, **styled, "12": {"1": ident, "2": uti}, "13": p["ts"],
                })  # fmt: skip
            else:
                segment += ch
        if segment:
            runs.append({"1": len(segment), **styled, "13": p["ts"]})
    message = {"2": {"3": {"2": "".join(pieces).encode("utf-8"), "5": runs}}}
    return gzip.compress(blackboxprotobuf.encode_message(message, _TYPEDEF))


class FakeDB:
    """The NotesDatabase reads the section path uses."""

    def __init__(
        self, zdata, attachments=None, tables=None, fingerprint="fp", block_sizes=None
    ):
        self.zdata = zdata
        self.block_sizes = block_sizes or {}
        self.attachments = attachments or {}
        self.tables = tables or {}
        self.fingerprint = fingerprint

    @classmethod
    def from_fixture(cls):
        raw = json.loads(FIXTURE.read_text())
        return cls(
            base64.b64decode(raw["zdata_b64"]),
            raw["attachments"],
            {k: base64.b64decode(v) for k, v in raw["tables"].items()},
        )

    @staticmethod
    def link_target():
        return json.loads(FIXTURE.read_text())["link_target"]

    def get_note_data(self, pk):
        return self.zdata

    def get_attachments_for_note(self, pk):
        return self.attachments

    def get_block_attachment_sizes(self, pk):
        return self.block_sizes

    def get_table_data(self, identifier):
        return self.tables.get(identifier)

    def note_fingerprint(self, pk):
        return self.fingerprint
