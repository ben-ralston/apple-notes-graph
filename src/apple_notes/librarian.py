"""Notes-librarian analytics: orphans, broken links, health report, link suggestions.

Pure functions in this module take pre-fetched notes/links and do no I/O;
the get_* orchestrators own database access and return MCP-ready dicts.
"""

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from . import unified_log
from .database import NotesDatabase
from .exceptions import (
    EncryptedNoteError,
    NoteNotFoundError,
    ProtobufParseError,
)
from .filters import filter_notes, matches_folder_filter
from .link_tokens import note_link_uuid
from .models import BrokenLink, GraphLink, Note, RawNoteLink
from .note_content import NoteContentReader
from .profiling import span

# Common words ignored for title tokenization; includes "note"/"notes"
# because nearly every title in a notes app could contain them.
STOPWORDS = frozenset(
    """
    a an and are as at be but by for from has have he her his i if in into is
    it its me my not of on or our she so that the their them they this to was
    we were will with you your note notes
    """.split()
)

# Snippet context returned around a mention match, in characters.
SNIPPET_CONTEXT_CHARS = 80
MAX_SNIPPETS_PER_REASON = 2


def tokenize_title(title: str) -> List[str]:
    """Lowercase alphanumeric tokens of a title, stopwords included."""
    return re.findall(r"[a-z0-9']+", title.lower())


def is_mention_eligible(title: str) -> bool:
    """
    Whether a title is specific enough for mention matching.

    After stopword removal it needs at least 2 tokens, or a single token of
    length >= 5 — so "Home" or "The Log" never generate mention noise while
    "Kubernetes" and "Grocery List" do.
    """
    tokens = [t for t in tokenize_title(title) if t not in STOPWORDS]
    return len(tokens) >= 2 or (len(tokens) == 1 and len(tokens[0]) >= 5)


def find_mention_snippets(
    title: str, body: str, max_snippets: int = MAX_SNIPPETS_PER_REASON
) -> List[str]:
    """
    Case-insensitive word-boundary occurrences of title in body, as snippets
    of +/- SNIPPET_CONTEXT_CHARS characters with "..." ellipses.
    """
    pattern = re.compile(r"(?<!\w)" + re.escape(title) + r"(?!\w)", re.IGNORECASE)

    snippets = []
    for match in pattern.finditer(body):
        start = max(0, match.start() - SNIPPET_CONTEXT_CHARS)
        end = min(len(body), match.end() + SNIPPET_CONTEXT_CHARS)
        snippet = body[start:end].replace("\n", " ").strip()
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(body) else ""
        snippets.append(f"{prefix}{snippet}{suffix}")
        if len(snippets) >= max_snippets:
            break
    return snippets


def title_word_overlap(title_a: str, title_b: str) -> List[str]:
    """
    Shared non-stopword tokens between two titles, sorted. Fires only when
    at least one shared token has length >= 3 (kills fragment-only matches);
    when it fires, all shared tokens are reported.
    """
    tokens_a = {t for t in tokenize_title(title_a) if t not in STOPWORDS}
    tokens_b = {t for t in tokenize_title(title_b) if t not in STOPWORDS}
    shared = tokens_a & tokens_b
    if not any(len(t) >= 3 for t in shared):
        return []
    return sorted(shared)


def resolve_note_links(
    raw_links: List[RawNoteLink], notes: List[Note]
) -> Tuple[List[GraphLink], List[BrokenLink]]:
    """
    Classify raw link rows into resolved GraphLinks and BrokenLinks.

    Rows are dropped (not reported) when the source note is unknown
    (deleted or in another account) or the URL is not an applenotes: note
    link. Broken links are deduplicated per (source, target uuid) with an
    occurrence count.
    """
    note_by_pk: Dict[int, Note] = {n.pk: n for n in notes}
    note_by_uuid: Dict[str, Note] = {n.uuid.upper(): n for n in notes if n.uuid}

    links: List[GraphLink] = []
    broken_by_key: Dict[Tuple[int, str], BrokenLink] = {}

    for row in raw_links:
        source = note_by_pk.get(row.source_pk)
        if source is None:
            continue

        target_uuid = note_link_uuid(row.target_url)
        if target_uuid is None:
            continue

        target = note_by_uuid.get(target_uuid)

        if target is not None:
            links.append(
                GraphLink(
                    source_name=source.name,
                    target_name=target.name,
                    source_pk=source.pk,
                    target_pk=target.pk,
                )
            )
            continue

        key = (source.pk, target_uuid)
        existing = broken_by_key.get(key)
        if existing is not None:
            existing.occurrences += 1
        else:
            broken_by_key[key] = BrokenLink(
                source_name=source.name,
                source_folder=source.folder,
                target_uuid=target_uuid,
                intended_target_title=row.alt_text,
                source_pk=source.pk,
            )

    return links, list(broken_by_key.values())


def find_orphan_notes(notes: List[Note], links: List[GraphLink]) -> List[Note]:
    """Return notes with zero incoming and zero outgoing resolved links."""
    linked_pks = set()
    for graph_link in links:
        linked_pks.add(graph_link.source_pk)
        linked_pks.add(graph_link.target_pk)
    return [n for n in notes if n.pk not in linked_pks]


def find_stale_notes(
    notes: List[Note], threshold_days: int, now: datetime
) -> List[Note]:
    """
    Return notes whose modified timestamp is older than now - threshold_days,
    sorted oldest first. Notes with no modified timestamp count as stale
    (recency can't be proven) and sort first.
    """
    cutoff = now - timedelta(days=threshold_days)

    stale = []
    for note in notes:
        if note.modified is None:
            stale.append((datetime.min.replace(tzinfo=timezone.utc), note))
            continue
        try:
            modified = datetime.fromisoformat(note.modified.replace("Z", "+00:00"))
        except ValueError:
            # Malformed timestamp: skip rather than crash the whole report.
            continue
        if modified < cutoff:
            stale.append((modified, note))

    stale.sort(key=lambda pair: pair[0])
    return [note for _, note in stale]


# Components larger than this are subdivided into communities for cluster
# naming/coloring (issue #131); real islands at or under it keep their identity.
CLUSTER_SPLIT_THRESHOLD = 30


def _build_adjacency(notes: List[Note], links: List[GraphLink]) -> Dict[int, set]:
    """Undirected adjacency over note pks; self-links ignored."""
    adjacency: Dict[int, set] = {n.pk: set() for n in notes}
    for graph_link in links:
        if graph_link.source_pk == graph_link.target_pk:
            continue
        adjacency.setdefault(graph_link.source_pk, set()).add(graph_link.target_pk)
        adjacency.setdefault(graph_link.target_pk, set()).add(graph_link.source_pk)
    return adjacency


def _connected_components(
    notes: List[Note], adjacency: Dict[int, set]
) -> List[List[int]]:
    """Connected components (as member-pk lists) of the linked subgraph;
    unlinked notes are excluded."""
    note_by_pk = {n.pk: n for n in notes}
    components: List[List[int]] = []
    visited: set = set()
    for note in notes:
        if note.pk in visited or not adjacency[note.pk]:
            continue
        component = []
        stack = [note.pk]
        visited.add(note.pk)
        while stack:
            pk = stack.pop()
            component.append(pk)
            for neighbor_pk in adjacency.get(pk, ()):
                if neighbor_pk not in visited and neighbor_pk in note_by_pk:
                    visited.add(neighbor_pk)
                    stack.append(neighbor_pk)
        components.append(component)
    return components


def _greedy_modularity_communities(
    member_pks: List[int], adjacency: Dict[int, set]
) -> List[List[int]]:
    """Split one component into communities via deterministic CNM-style
    greedy modularity.

    Merge score for communities a, b is the integer 2*m*e_ab - d_a*d_b
    (proportional to the modularity gain, so sign-exact with no float
    noise). Each round merges the highest-scoring positive pair, ties
    broken by smallest (min_id, max_id) community-id pair; community id =
    smallest member pk. Stops when no merge has positive score. Fully
    deterministic: the renderer's cluster-name cache keys on member sets,
    and nondeterministic membership would re-trigger LLM naming every run.
    """
    members = sorted(member_pks)
    member_set = set(members)
    degree = {pk: sum(1 for nb in adjacency[pk] if nb in member_set) for pk in members}
    m = sum(degree.values()) // 2
    if m == 0:
        return [members]

    comm_of = {pk: pk for pk in members}
    communities: Dict[int, set] = {pk: {pk} for pk in members}

    while True:
        between: Counter = Counter()
        for a in members:
            for b in adjacency[a]:
                if b in member_set and a < b and comm_of[a] != comm_of[b]:
                    ca, cb = comm_of[a], comm_of[b]
                    between[(ca, cb) if ca < cb else (cb, ca)] += 1
        if not between:
            break
        degree_sum = {
            cid: sum(degree[pk] for pk in pks) for cid, pks in communities.items()
        }
        best_pair = None
        best_score = 0
        for pair in sorted(between):
            score = 2 * m * between[pair] - degree_sum[pair[0]] * degree_sum[pair[1]]
            if score > best_score:
                best_pair, best_score = pair, score
        if best_pair is None:
            break
        keep, absorb = best_pair
        communities[keep] |= communities.pop(absorb)
        for pk in communities[keep]:
            comm_of[pk] = keep

    return sorted(sorted(pks) for pks in communities.values())


def _partition_graph(notes: List[Note], adjacency: Dict[int, set]) -> List[List[int]]:
    """Member-pk lists of the graph's clusters: connected components, with
    components larger than CLUSTER_SPLIT_THRESHOLD subdivided into
    greedy-modularity communities (issue #131)."""
    clusters: List[List[int]] = []
    for component in _connected_components(notes, adjacency):
        if len(component) <= CLUSTER_SPLIT_THRESHOLD:
            clusters.append(component)
        else:
            clusters.extend(_greedy_modularity_communities(component, adjacency))
    return clusters


def _clusters_with_hubs(
    notes: List[Note], links: List[GraphLink]
) -> List[Tuple[List[int], Note]]:
    """(member pks, hub note) per cluster, sorted by (-size, hub name).
    Hub = the member with the highest intra-cluster degree, ties
    alphabetical — cross-community links must not crown a boundary node."""
    note_by_pk = {n.pk: n for n in notes}
    adjacency = _build_adjacency(notes, links)
    clusters = []
    for member_pks in _partition_graph(notes, adjacency):
        member_set = set(member_pks)
        hub = min(
            (note_by_pk[pk] for pk in member_pks),
            key=lambda n: (-len(adjacency[n.pk] & member_set), n.name),
        )
        clusters.append((member_pks, hub))
    clusters.sort(key=lambda pair: (-len(pair[0]), pair[1].name))
    return clusters


def find_clusters(notes: List[Note], links: List[GraphLink]) -> List[dict]:
    """
    Return the graph's clusters as [{"hub": name, "size": n}], size >= 2
    only (singletons are orphans), sorted by size descending. Clusters are
    connected components, except components larger than
    CLUSTER_SPLIT_THRESHOLD are subdivided into communities by
    deterministic greedy modularity (issue #131). Hub = the member with
    the highest intra-cluster degree, ties broken alphabetically.
    """
    return [
        {"hub": hub.name, "size": len(member_pks)}
        for member_pks, hub in _clusters_with_hubs(notes, links)
        if len(member_pks) >= 2
    ]


def assign_cluster_ids(notes: List[Note], links: List[GraphLink]) -> Dict[int, int]:
    """
    Return note pk -> cluster id for graph coloring.

    Clusters are connected components, with components larger than
    CLUSTER_SPLIT_THRESHOLD subdivided into greedy-modularity communities
    (issue #131). Ids are dense indexes ordered by cluster size descending
    (ties by hub name, matching find_clusters ordering) and may renumber
    across runs. Notes with no links get -1. Self-links are ignored.
    """
    ids: Dict[int, int] = {n.pk: -1 for n in notes}
    for cluster_id, (member_pks, _hub) in enumerate(_clusters_with_hubs(notes, links)):
        for pk in member_pks:
            ids[pk] = cluster_id
    return ids


def find_duplicate_names(notes: List[Note]) -> List[dict]:
    """
    Return case-insensitive duplicate title groups as
    [{"name": lowercased title, "count": n, "notes": [{name, folder, uuid}]}].
    Duplicates are a hazard: name-based lookups resolve one arbitrarily.
    """
    by_name: Dict[str, List[Note]] = {}
    for note in notes:
        by_name.setdefault(note.name.lower(), []).append(note)

    return [
        {
            "name": lowered,
            "count": len(group),
            "notes": [
                {"name": n.name, "folder": n.folder, "uuid": n.uuid} for n in group
            ],
        }
        for lowered, group in sorted(by_name.items())
        if len(group) > 1
    ]


def compute_connection_counts(
    notes: List[Note], links: List[GraphLink]
) -> Dict[int, int]:
    """
    Return note pk -> number of distinct undirected neighbors.

    Multiple links between the same pair count once; self-links are ignored.
    """
    neighbors: Dict[int, set] = {n.pk: set() for n in notes}
    for graph_link in links:
        if graph_link.source_pk == graph_link.target_pk:
            continue
        neighbors.setdefault(graph_link.source_pk, set()).add(graph_link.target_pk)
        neighbors.setdefault(graph_link.target_pk, set()).add(graph_link.source_pk)
    return {n.pk: len(neighbors[n.pk]) for n in notes}


def _fetch_filtered_graph(
    db_path: Optional[str],
    exclude_folders: Optional[List[str]],
    include_folders: Optional[List[str]],
) -> Tuple[List[Note], List[GraphLink], List[BrokenLink]]:
    """
    Fetch notes + links and apply folder filters.

    Links survive only when both endpoints survive filtering, so analytics
    are relative to the filtered view (matching the graph UI). Broken links
    are filtered by their source note's folder (the target is gone).
    """
    with span("fetch"):
        with NotesDatabase(db_path) as db:
            notes = db.get_all_notes()
            raw_links = db.get_raw_note_links()

    links, broken = resolve_note_links(raw_links, notes)

    filtered_notes = filter_notes(notes, exclude_folders, include_folders)
    filtered_pks = {n.pk for n in filtered_notes}
    filtered_links = [
        l for l in links if l.source_pk in filtered_pks and l.target_pk in filtered_pks
    ]

    filtered_broken = list(broken)
    if include_folders:
        filtered_broken = [
            b
            for b in filtered_broken
            if matches_folder_filter(b.source_folder, include_folders)
        ]
    if exclude_folders:
        filtered_broken = [
            b
            for b in filtered_broken
            if not matches_folder_filter(b.source_folder, exclude_folders)
        ]

    return filtered_notes, filtered_links, filtered_broken


def _broken_link_dict(broken: BrokenLink) -> dict:
    return {
        "source_note": broken.source_name,
        "source_folder": broken.source_folder,
        "intended_target_title": broken.intended_target_title,
        "target_uuid": broken.target_uuid,
        "occurrences": broken.occurrences,
    }


def get_orphans(
    db_path: Optional[str] = None,
    exclude_folders: Optional[List[str]] = None,
    include_folders: Optional[List[str]] = None,
) -> dict:
    """Find notes with no incoming or outgoing links (in the filtered view)."""
    notes, links, _broken = _fetch_filtered_graph(
        db_path, exclude_folders, include_folders
    )

    orphans = find_orphan_notes(notes, links)
    orphans.sort(key=lambda n: n.modified or "", reverse=True)

    return {
        "orphans": [
            {"name": n.name, "folder": n.folder, "modified": n.modified}
            for n in orphans
        ],
        "count": len(orphans),
        "total_notes": len(notes),
    }


def get_broken_links(
    db_path: Optional[str] = None,
    exclude_folders: Optional[List[str]] = None,
    include_folders: Optional[List[str]] = None,
) -> dict:
    """Find note links whose target note no longer exists."""
    _notes, _links, broken = _fetch_filtered_graph(
        db_path, exclude_folders, include_folders
    )

    broken.sort(key=lambda b: b.source_name)

    return {
        "broken_links": [_broken_link_dict(b) for b in broken],
        "count": len(broken),
    }


def get_health_report(
    stale_threshold_days: int = 180,
    top_n: int = 10,
    db_path: Optional[str] = None,
    exclude_folders: Optional[List[str]] = None,
    include_folders: Optional[List[str]] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Build the comprehensive notes-graph health report (issue #77)."""
    if now is None:
        now = datetime.now(timezone.utc)

    notes, links, broken = _fetch_filtered_graph(
        db_path, exclude_folders, include_folders
    )

    orphans = find_orphan_notes(notes, links)
    stale = find_stale_notes(notes, stale_threshold_days, now)
    counts = compute_connection_counts(notes, links)
    broken.sort(key=lambda b: b.source_name)

    connected = [n for n in notes if counts[n.pk] > 0]
    connected.sort(key=lambda n: (-counts[n.pk], n.name))

    return {
        "total_notes": len(notes),
        "total_links": len(links),
        "orphan_notes": {
            "count": len(orphans),
            "notes": [n.name for n in orphans],
        },
        "broken_links": {
            "count": len(broken),
            "links": [_broken_link_dict(b) for b in broken],
        },
        "stale_notes": {
            "count": len(stale),
            "threshold_days": stale_threshold_days,
            "notes": [
                {"name": n.name, "folder": n.folder, "modified": n.modified}
                for n in stale[:top_n]
            ],
        },
        "top_connected": [
            {"name": n.name, "connections": counts[n.pk]} for n in connected[:top_n]
        ],
        "clusters": find_clusters(notes, links)[:top_n],
        "duplicate_names": find_duplicate_names(notes),
    }


def build_candidates(
    subject: Note,
    subject_text: str,
    notes: List[Note],
    links: List[GraphLink],
    note_texts: Dict[int, str],
    max_candidates: int,
) -> List[dict]:
    """
    Surface link candidates for a note via transparent recall signals.

    Each candidate carries a `reasons` list naming every signal that fired;
    there is no scoring — semantic ranking is the caller's (the LLM client's)
    job. Notes the subject already links to, and the subject itself, are
    excluded. Candidates with reasons are ordered by reason count then name;
    candidates whose only reason is same_folder go last (newest first). The
    list is capped at max_candidates.
    """
    note_by_pk = {n.pk: n for n in notes}

    neighbors: Dict[int, set] = {}
    for graph_link in links:
        if graph_link.source_pk == graph_link.target_pk:
            continue
        neighbors.setdefault(graph_link.source_pk, set()).add(graph_link.target_pk)
        neighbors.setdefault(graph_link.target_pk, set()).add(graph_link.source_pk)

    outgoing_pks = {l.target_pk for l in links if l.source_pk == subject.pk}
    incoming_pks = {l.source_pk for l in links if l.target_pk == subject.pk}
    subject_neighbors = neighbors.get(subject.pk, set())
    subject_eligible = is_mention_eligible(subject.name)

    candidates = []
    for note in notes:
        if note.pk == subject.pk or note.pk in outgoing_pks:
            continue

        reasons = []

        if is_mention_eligible(note.name):
            snippets = find_mention_snippets(note.name, subject_text)
            if snippets:
                reasons.append({"type": "forward_mention", "snippets": snippets})

        if subject_eligible and note.pk in note_texts:
            snippets = find_mention_snippets(subject.name, note_texts[note.pk])
            if snippets:
                reasons.append({"type": "reverse_mention", "snippets": snippets})

        shared_pks = (subject_neighbors & neighbors.get(note.pk, set())) - {
            subject.pk,
            note.pk,
        }
        if shared_pks:
            reasons.append(
                {
                    "type": "co_citation",
                    "shared_neighbors": sorted(
                        note_by_pk[pk].name for pk in shared_pks
                    ),
                }
            )

        shared_words = title_word_overlap(subject.name, note.name)
        if shared_words:
            reasons.append({"type": "title_overlap", "shared_words": shared_words})

        if note.folder == subject.folder:
            reasons.append({"type": "same_folder", "folder": note.folder})

        if note.pk in incoming_pks:
            reasons.append({"type": "backlink_not_reciprocated"})

        if reasons:
            candidates.append(
                {
                    "name": note.name,
                    "folder": note.folder,
                    "modified": note.modified,
                    "reasons": reasons,
                }
            )

    def is_same_folder_only(candidate: dict) -> bool:
        return [r["type"] for r in candidate["reasons"]] == ["same_folder"]

    primary = [c for c in candidates if not is_same_folder_only(c)]
    folder_only = [c for c in candidates if is_same_folder_only(c)]
    primary.sort(key=lambda c: (-len(c["reasons"]), c["name"]))
    folder_only.sort(key=lambda c: c["modified"] or "", reverse=True)

    return (primary + folder_only)[:max_candidates]


# Subject content returned by get_link_suggestions is capped so a huge note
# can't blow up the client's context.
MAX_SUBJECT_CONTENT_CHARS = 10_000


def get_link_suggestions(
    note_name: Optional[str] = None,
    max_candidates: int = 25,
    db_path: Optional[str] = None,
    exclude_folders: Optional[List[str]] = None,
    include_folders: Optional[List[str]] = None,
    note_pk: Optional[int] = None,
) -> dict:
    """
    Gather context for suggesting links from a note: the note's content plus
    candidate notes surfaced by transparent recall signals (see
    build_candidates). Semantic ranking is left to the caller.

    The subject is addressed by note_pk when given (exact, duplicate-title
    safe — the Electron UI path), otherwise looked up case-insensitively by
    note_name (the MCP path; duplicate-name subjects resolve to one note
    arbitrarily). Folder filters never hide the subject; the candidate pool
    is the filtered note set. This is the only librarian call that parses
    note content — every candidate note's body is scanned for reverse
    mentions (same cost profile as search_notes with search_content=True).
    Encrypted/unparseable candidates are skipped and counted.

    Raises:
        NoteNotFoundError: subject doesn't exist
        ValueError: neither note_name nor note_pk given
        EncryptedNoteError / ProtobufParseError: subject itself unreadable
    """
    with span("fetch_links"):
        with NotesDatabase(db_path) as db:
            notes = db.get_all_notes()
            raw_links = db.get_raw_note_links()
    links, _broken = resolve_note_links(raw_links, notes)

    if note_pk is not None:
        subject = next((n for n in notes if n.pk == note_pk), None)
        if subject is None:
            raise NoteNotFoundError(f"Note with pk {note_pk} not found")
    else:
        if note_name is None:
            raise ValueError("either note_name or note_pk is required")
        subject = next((n for n in notes if n.name.lower() == note_name.lower()), None)
        if subject is None:
            raise NoteNotFoundError(f"Note '{note_name}' not found")

    reader = NoteContentReader(db_path)
    with span("read_subject"):
        # pk-addressed read: safe when several notes share the subject's title
        subject_content = reader.get_note_content_for_note(subject)
    subject_text = subject_content.resolved_text

    filtered_notes = filter_notes(notes, exclude_folders, include_folders)

    # Notes the subject already links to can never be candidates, so don't
    # pay the protobuf parse for their bodies.
    outgoing_pks = {l.target_pk for l in links if l.source_pk == subject.pk}

    note_texts: Dict[int, str] = {}
    skipped = 0
    with span("scan_content"):
        for note in filtered_notes:
            if note.pk == subject.pk or note.pk in outgoing_pks:
                continue
            try:
                note_texts[note.pk] = reader.get_note_content_for_note(
                    note
                ).resolved_text
            except (EncryptedNoteError, ProtobufParseError) as e:
                unified_log.log_parse_error("suggest_links", note.pk, e)
                skipped += 1

    with span("assemble"):
        # Restrict the graph to the filtered view plus the subject so every
        # pk that signals reference (co-citation neighbors, backlinks) is
        # resolvable to a visible note.
        allowed_pks = {n.pk for n in filtered_notes} | {subject.pk}
        graph_links = [
            l
            for l in links
            if l.source_pk in allowed_pks and l.target_pk in allowed_pks
        ]
        pool = filtered_notes
        if all(n.pk != subject.pk for n in pool):
            pool = pool + [subject]

        candidates = build_candidates(
            subject,
            subject_text,
            pool,
            graph_links,
            note_texts,
            max_candidates=max_candidates,
        )

        outgoing = sorted(
            {
                l.target_name
                for l in links
                if l.source_pk == subject.pk and l.target_pk != subject.pk
            }
        )
        backlinks = sorted(
            {
                l.source_name
                for l in links
                if l.target_pk == subject.pk and l.source_pk != subject.pk
            }
        )

    truncated = len(subject_text) > MAX_SUBJECT_CONTENT_CHARS
    return {
        "note": {
            "name": subject.name,
            "folder": subject.folder,
            "content": subject_text[:MAX_SUBJECT_CONTENT_CHARS],
            "content_truncated": truncated,
            "existing_outgoing_links": outgoing,
            "existing_backlinks": backlinks,
        },
        "candidates": candidates,
        "candidate_count": len(candidates),
        "skipped_notes": {
            "count": skipped,
            "reason": "encrypted_or_unparseable",
        },
    }
