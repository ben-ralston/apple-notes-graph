# Demo library

An invented Apple Notes library for trying the graph UI and the MCP server
without touching real notes. The person, projects, garden and reading list in it
are made up.

```bash
./run demo-build   # write demo/home/.../NoteStore.sqlite
./run demo-ui      # graph UI on the demo library
./run demo-mcp     # stdio MCP server on the demo library
```

## What is generated

`build_demo_db.py` turns the notes in `demo_content.py` into a `NoteStore.sqlite`
in the on-disk format the Notes app uses, limited to the tables and columns this
project reads:

- folders and notes as rows of `ZICCLOUDSYNCINGOBJECT`;
- each note body as a gzip-compressed protobuf in `ZICNOTEDATA.ZDATA` (text plus
  attribute runs carrying paragraph style, indent, checklist state and inline
  formatting);
- note links and tags as inline-attachment rows, referenced from the text by
  U+FFFC placeholders;
- tables as MergeableData object graphs in `ZMERGEABLEDATA1`.

It has 99 notes in 8 folders: hub notes, five clusters, seven orphans, one link
to a note that no longer exists, one note in Recently Deleted, and a few notes
with rich content (`Weather station` has the most).

## How the demo is wired

The library finds the database relative to the home directory:
`~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite`. The demo
commands run with `HOME` set to `demo/home`, where the generated file sits at
that same relative path. No code switch is involved, so the normal database
reader, protobuf parser and table parser all run. Config, caches and Electron's
profile land in `demo/home` too, which is ignored by git.

## Limits

- The generated database is not a full Notes store. The Notes app could not open
  it; it holds only what this project reads.
- Write tools (`add_note`, `update_note_content`, `edit_note_section` and the
  rest) script the Notes app, so they cannot act on the demo library. Their
  behaviour is covered by the test suite.
- Group names in the UI need the Claude Code CLI; semantic search downloads a
  small embedding model on first use.
