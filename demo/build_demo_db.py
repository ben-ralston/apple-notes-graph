#!/usr/bin/env python3
"""Build a synthetic Apple Notes database for demos and screenshots.

Writes a ``NoteStore.sqlite`` in the on-disk format the real Notes app uses
(the subset of it this project reads): notes and folders as rows of
``ZICCLOUDSYNCINGOBJECT``, each note body as a gzip-compressed protobuf in
``ZICNOTEDATA.ZDATA``, note links and tags as inline-attachment rows, and
tables as MergeableData object graphs in ``ZMERGEABLEDATA1``.

Every note is invented. Nothing is read from a real Notes library.

The file lands where the library looks for it relative to ``$HOME``::

    <home>/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite

so running any entry point with ``HOME=<home>`` exercises the unmodified
database reader, and keeps config, caches and logs inside ``<home>`` too.

Usage:
    uv run python demo/build_demo_db.py            # writes demo/home/...
    uv run python demo/build_demo_db.py --home /tmp/somewhere
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import blackboxprotobuf

from demo_content import FOLDERS, NOTES, DANGLING_TITLE

LINK_UTI = "com.apple.notes.inlinetextattachment.link"
TAG_UTI = "com.apple.notes.inlinetextattachment.hashtag"
TABLE_UTI = "com.apple.notes.table"
OWNER = "_demo0000000000000000000000000000"
NAMESPACE = uuid.UUID("6f1d2c1e-5a4b-4c3d-9e8f-0a1b2c3d4e5f")
COCOA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
# "Now" for the demo library. Dates are fixed so rebuilds are identical.
DEMO_NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
OBJ = "￼"  # U+FFFC OBJECT REPLACEMENT CHARACTER marks an attachment

# Paragraph style types (protobuf field 2.1), as Notes stores them.
TITLE, HEADING, SUBHEADING, MONO = 0, 1, 2, 4
BULLET, DASH, NUMBERED, CHECK = 100, 101, 102, 103
COLORS = {"purple": 1, "pink": 2, "orange": 3, "green": 4, "blue": 5}


def new_uuid(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, "/".join(parts))).upper()


def cocoa(moment: datetime) -> float:
    return (moment - COCOA_EPOCH).total_seconds()


# --- note body: markup -> paragraphs -> protobuf -------------------------------


@dataclass
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    color: int | None = None
    url: str | None = None
    attachment: tuple[str, str] | None = None  # (identifier, uti)


@dataclass
class Paragraph:
    spans: list[Span]
    kind: int | None = None
    indent: int = 0
    checked: bool | None = None
    quote: bool = False


@dataclass
class Built:
    """One note's encoded body plus the attachment rows it needs."""

    zdata: bytes
    links: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # id, target title, alt
    tags: list[tuple[str, str]] = field(default_factory=list)  # id, tag
    tables: list[tuple[str, list[list[str]]]] = field(default_factory=list)


INLINE = re.compile(
    r"\[\[(?P<link>[^\]]+)\]\]"
    r"|\{#(?P<tag>[\w-]+)\}"
    r"|\{(?P<cname>purple|pink|orange|green|blue)\|(?P<ctext>[^}]+)\}"
    r"|\[(?P<utext>[^\]]+)\]\((?P<url>https?://[^)]+)\)"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|~~(?P<strike>.+?)~~"
    r"|__(?P<under>.+?)__"
    r"|\*(?P<italic>[^*]+?)\*"
)


def parse_inline(text: str, note_key: str, built: Built) -> list[Span]:
    spans, pos = [], 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            spans.append(Span(text[pos : m.start()]))
        if m["link"]:
            ident = new_uuid(note_key, "link", str(len(built.links)))
            built.links.append((ident, m["link"], m["link"]))
            spans.append(Span(OBJ, attachment=(ident, LINK_UTI)))
        elif m["tag"]:
            ident = new_uuid(note_key, "tag", str(len(built.tags)))
            built.tags.append((ident, m["tag"]))
            spans.append(Span(OBJ, attachment=(ident, TAG_UTI)))
        elif m["cname"]:
            spans.append(Span(m["ctext"], color=COLORS[m["cname"]]))
        elif m["url"]:
            spans.append(Span(m["utext"], url=m["url"]))
        elif m["bold"]:
            spans.append(Span(m["bold"], bold=True))
        elif m["strike"]:
            spans.append(Span(m["strike"], strike=True))
        elif m["under"]:
            spans.append(Span(m["under"], underline=True))
        else:
            spans.append(Span(m["italic"], italic=True))
        pos = m.end()
    if pos < len(text):
        spans.append(Span(text[pos:]))
    return spans or [Span("")]


BLOCKS = [
    (re.compile(r"^# (.*)$"), HEADING),
    (re.compile(r"^## (.*)$"), SUBHEADING),
    (re.compile(r"^\* (.*)$"), BULLET),
    (re.compile(r"^- (.*)$"), DASH),
    (re.compile(r"^\d+\. (.*)$"), NUMBERED),
]


def parse_body(title: str, body: str, note_key: str) -> tuple[list[Paragraph], Built]:
    """The demo markup: a small Markdown-like notation that maps one to one
    onto what Notes stores (see demo/README.md)."""
    built = Built(b"")
    paragraphs = [Paragraph([Span(title)], TITLE)]
    lines = body.strip("\n").split("\n") if body.strip() else []
    in_code, table = False, []

    def flush_table():
        if table:
            ident = new_uuid(note_key, "table", str(len(built.tables)))
            built.tables.append((ident, [row[:] for row in table]))
            paragraphs.append(Paragraph([Span(OBJ, attachment=(ident, TABLE_UTI))]))
            table.clear()

    for raw in lines:
        if raw.strip() == "```":
            in_code = not in_code
            continue
        if in_code:
            paragraphs.append(Paragraph([Span(raw)], MONO))
            continue
        if raw.startswith("|"):
            table.append([cell.strip() for cell in raw.strip().strip("|").split("|")])
            continue
        flush_table()
        stripped = raw.lstrip(" ")
        indent = (len(raw) - len(stripped)) // 2
        check = re.match(r"^\[( |x)\] (.*)$", stripped)
        if check:
            paragraphs.append(
                Paragraph(
                    parse_inline(check[2], note_key, built),
                    CHECK,
                    indent,
                    check[1] == "x",
                )
            )
            continue
        if stripped.startswith("> "):
            paragraphs.append(
                Paragraph(parse_inline(stripped[2:], note_key, built), quote=True)
            )
            continue
        for pattern, kind in BLOCKS:
            m = pattern.match(stripped)
            if m:
                paragraphs.append(
                    Paragraph(parse_inline(m[1], note_key, built), kind, indent)
                )
                break
        else:
            paragraphs.append(Paragraph(parse_inline(stripped, note_key, built)))
    flush_table()
    return paragraphs, built


def _msg(fields: dict) -> dict:
    return {"type": "message", "message_typedef": fields, "name": ""}


_INT = {"type": "int", "name": ""}
_BYTES = {"type": "bytes", "name": ""}
_STR = {"type": "string", "name": ""}
_STYLE = _msg({"1": _INT, "4": _INT, "5": _msg({"1": _BYTES, "2": _INT}), "8": _INT})
_RUN = _msg(
    {"1": _INT, "2": _STYLE, "5": _INT, "6": _INT, "7": _INT, "9": _STR,
     "12": _msg({"1": _STR, "2": _STR}), "14": _INT}
)  # fmt: skip
NOTE_TYPEDEF = {"2": _msg({"3": _msg({"2": _BYTES, "5": _RUN})})}


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def encode_note(paragraphs: list[Paragraph], note_key: str) -> bytes:
    """ZICNOTEDATA.ZDATA: gzip(protobuf{2:{3:{2: text, 5: [attribute runs]}}}).

    One attribute run per span. Each run carries its length in UTF-16 code
    units, its paragraph's style (field 2) and its own character formatting.
    """
    text, runs = [], []
    for index, p in enumerate(paragraphs):
        style: dict = {}
        if p.kind is not None:
            style["1"] = p.kind
        if p.indent:
            style["4"] = p.indent
        if p.checked is not None:
            style["5"] = {
                "1": uuid.UUID(new_uuid(note_key, "check", str(index))).bytes,
                "2": int(p.checked),
            }
        if p.quote:
            style["8"] = 1
        spans = p.spans + ([Span("\n")] if index < len(paragraphs) - 1 else [])
        for span in spans:
            if not span.text:
                continue
            run: dict = {"1": utf16_len(span.text)}
            if style:
                run["2"] = style
            font = (1 if span.bold else 0) | (2 if span.italic else 0)
            if font:
                run["5"] = font
            if span.underline:
                run["6"] = 1
            if span.strike:
                run["7"] = 1
            if span.url:
                run["9"] = span.url
            if span.attachment:
                run["12"] = {"1": span.attachment[0], "2": span.attachment[1]}
            if span.color:
                run["14"] = span.color
            text.append(span.text)
            runs.append(run)
    message = {"2": {"3": {"2": "".join(text).encode("utf-8"), "5": runs}}}
    return gzip.compress(
        blackboxprotobuf.encode_message(message, NOTE_TYPEDEF), mtime=0
    )


# --- tables: a MergeableData object graph --------------------------------------

_OID = _msg({"2": _INT, "6": _INT})
_DICT = _msg({"1": _msg({"1": _OID, "2": _OID})})
TABLE_TYPEDEF = {
    "2": _msg({"3": _msg({
        "3": _msg({
            "6": _DICT,
            "10": _msg({"2": _BYTES}),
            "13": _msg({"1": _INT, "3": _msg({"1": _INT, "2": _OID})}),
            "16": _msg({"1": _msg({
                "1": _msg({"2": _msg({"1": _INT, "2": _BYTES})}),
                "2": _DICT,
            })}),
        }),
        "4": _BYTES, "5": _BYTES, "6": _BYTES,
    })})
}  # fmt: skip
TABLE_KEYS = [
    b"identity",
    b"cellColumns",
    b"UUIDIndex",
    b"crRows",
    b"crColumns",
    b"self",
]
TABLE_TYPES = [
    b"com.apple.CRDT.NSNumber",
    b"com.apple.CRDT.NSUUID",
    b"com.apple.notes.ICTable",
]


def encode_table(grid: list[list[str]], seed: str) -> bytes:
    """ZMERGEABLEDATA1 for a table: an ICTable root pointing at crRows and
    crColumns (ordered uuid sets) and cellColumns (column -> row -> cell)."""
    rng = random.Random(seed)
    rows, cols = len(grid), len(grid[0])
    uuids: list[bytes] = []

    def mint() -> int:
        uuids.append(rng.randbytes(16))
        return len(uuids) - 1

    order, ident = {}, {}
    for slot in [("r", i) for i in range(rows)] + [("c", i) for i in range(cols)]:
        order[slot], ident[slot] = mint(), mint()

    objects: dict[str, dict] = {}

    def ref(name: str) -> dict:
        return {"6": name}

    def uuid_obj(index: int) -> str:
        name = f"u{index}"
        objects[name] = {"13": {"1": 1, "3": [{"1": 2, "2": {"2": index}}]}}
        return name

    def ordered_set(kind: str, count: int) -> dict:
        return {"16": {"1": {
            "1": {"2": [{"1": i, "2": uuids[order[(kind, i)]]} for i in range(count)]},
            "2": {"1": [
                {"1": ref(uuid_obj(order[(kind, i)])), "2": ref(uuid_obj(ident[(kind, i)]))}
                for i in range(count)
            ]},
        }}}  # fmt: skip

    objects["rows"] = ordered_set("r", rows)
    objects["cols"] = ordered_set("c", cols)
    columns = []
    for c in range(cols):
        cells = []
        for r in range(rows):
            objects[f"cell{r},{c}"] = {"10": {"2": grid[r][c].encode("utf-8")}}
            cells.append(
                {"1": ref(uuid_obj(ident[("r", r)])), "2": ref(f"cell{r},{c}")}
            )
        objects[f"col{c}"] = {"6": {"1": cells}}
        columns.append({"1": ref(uuid_obj(ident[("c", c)])), "2": ref(f"col{c}")})
    objects["cellColumns"] = {"6": {"1": columns}}
    objects["root"] = {"13": {"1": 2, "3": [
        {"1": 1, "2": ref("cellColumns")},
        {"1": 3, "2": ref("rows")},
        {"1": 4, "2": ref("cols")},
    ]}}  # fmt: skip

    names = list(objects)
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

    message = {"2": {"3": {
        "3": [resolve(objects[name]) for name in names],
        "4": TABLE_KEYS, "5": TABLE_TYPES, "6": uuids,
    }}}  # fmt: skip
    return gzip.compress(
        blackboxprotobuf.encode_message(message, TABLE_TYPEDEF), mtime=0
    )


# --- database ------------------------------------------------------------------

SCHEMA = """
CREATE TABLE ZICCLOUDSYNCINGOBJECT (
    Z_PK INTEGER PRIMARY KEY,
    ZIDENTIFIER TEXT,
    ZTITLE1 TEXT,              -- note title
    ZTITLE2 TEXT,              -- folder title
    ZFOLDER INTEGER,           -- note -> folder
    ZPARENT INTEGER,           -- folder -> parent folder
    ZFOLDERTYPE INTEGER,       -- 1 = Recently Deleted, 2 = smart folder
    ZCREATIONDATE3 REAL,
    ZMODIFICATIONDATE1 REAL,
    ZLASTOPENEDDATE REAL,
    ZMARKEDFORDELETION INTEGER DEFAULT 0,
    ZISPASSWORDPROTECTED INTEGER DEFAULT 0,
    ZCRYPTOTAG BLOB,
    ZNOTE INTEGER,             -- block attachment (table) -> note
    ZNOTE1 INTEGER,            -- inline attachment (link, tag) -> note
    ZTYPEUTI TEXT,
    ZTYPEUTI1 TEXT,
    ZTOKENCONTENTIDENTIFIER TEXT,
    ZALTTEXT TEXT,
    ZMERGEABLEDATA1 BLOB,
    ZFILESIZE INTEGER,
    ZACCOUNTTYPE INTEGER,
    ZUSERRECORDNAME TEXT,
    ZZONEOWNERNAME TEXT
);
CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZNOTE INTEGER, ZDATA BLOB);
CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID TEXT, Z_PLIST BLOB);
"""


def build(home: Path) -> Path:
    db_path = home / "Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ):
        stale.unlink(missing_ok=True)

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO Z_METADATA VALUES (1, ?, NULL)", (new_uuid("store"),))
    pk = 0

    def next_pk() -> int:
        nonlocal pk
        pk += 1
        return pk

    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZACCOUNTTYPE, ZUSERRECORDNAME)"
        " VALUES (?, ?, 1, ?)",
        (next_pk(), new_uuid("account"), OWNER),
    )

    folder_pk: dict[str, int] = {}
    for path, folder_type in FOLDERS:
        parent, _, name = path.rpartition("/")
        folder_pk[path] = next_pk()
        conn.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE2, ZPARENT, ZFOLDERTYPE)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                folder_pk[path],
                new_uuid("folder", path),
                name,
                folder_pk.get(parent),
                folder_type,
            ),
        )

    titles = [note.title for note in NOTES]
    assert len(titles) == len(set(titles)), "demo note titles must be unique"
    note_uuid = {title: new_uuid("note", title) for title in titles}
    note_uuid[DANGLING_TITLE] = new_uuid(
        "note", "deleted", DANGLING_TITLE
    )  # never inserted
    note_pk = {title: next_pk() for title in titles}

    rng = random.Random("notes-graph-demo")
    counts = {"links": 0, "tags": 0, "tables": 0}
    for note in NOTES:
        key = note.title
        paragraphs, built = parse_body(note.title, note.body, key)
        created = DEMO_NOW - timedelta(days=note.age_days, hours=rng.randint(0, 12))
        modified = DEMO_NOW - timedelta(
            days=note.idle_days, minutes=rng.randint(0, 600)
        )
        conn.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE1, ZFOLDER,"
            " ZCREATIONDATE3, ZMODIFICATIONDATE1, ZLASTOPENEDDATE) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (note_pk[key], note_uuid[key], note.title, folder_pk[note.folder],
             cocoa(created), cocoa(modified), cocoa(modified)),
        )  # fmt: skip
        conn.execute(
            "INSERT INTO ZICNOTEDATA (ZNOTE, ZDATA) VALUES (?, ?)",
            (note_pk[key], encode_note(paragraphs, key)),
        )
        for ident, target, alt in built.links:
            assert target in note_uuid, f"{key!r} links to unknown note {target!r}"
            token = (
                f"applenotes:note/{note_uuid[target].lower()}?ownerIdentifier={OWNER}"
            )
            conn.execute(
                "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZNOTE1, ZTYPEUTI1,"
                " ZTOKENCONTENTIDENTIFIER, ZALTTEXT) VALUES (?, ?, ?, ?, ?, ?)",
                (next_pk(), ident, note_pk[key], LINK_UTI, token, alt),
            )
        for ident, tag in built.tags:
            conn.execute(
                "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZNOTE1, ZTYPEUTI1,"
                " ZTOKENCONTENTIDENTIFIER, ZALTTEXT) VALUES (?, ?, ?, ?, ?, ?)",
                (next_pk(), ident, note_pk[key], TAG_UTI, tag.upper(), f"#{tag}"),
            )
        for ident, grid in built.tables:
            blob = encode_table(grid, ident)
            conn.execute(
                "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZNOTE, ZTYPEUTI,"
                " ZMERGEABLEDATA1, ZFILESIZE) VALUES (?, ?, ?, ?, ?, ?)",
                (next_pk(), ident, note_pk[key], TABLE_UTI, blob, len(blob)),
            )
        counts["links"] += len(built.links)
        counts["tags"] += len(built.tags)
        counts["tables"] += len(built.tables)
    conn.commit()
    conn.close()

    config = home / "Library/Application Support/notes-graph/config.json"
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps({"exclude_folders": [], "include_folders": []}, indent=2) + "\n"
        )

    print(f"Wrote {db_path}")
    print(f"  {len(NOTES)} notes in {len(FOLDERS)} folders, {counts['links']} note links,"
          f" {counts['tags']} tags, {counts['tables']} tables")  # fmt: skip
    return db_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--home", type=Path, default=Path(__file__).parent / "home",
                        help="demo home directory (default: demo/home)")  # fmt: skip
    build(parser.parse_args().home.resolve())


if __name__ == "__main__":
    main()
