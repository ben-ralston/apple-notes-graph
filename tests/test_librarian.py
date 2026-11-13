"""Tests for the notes-librarian analytics in apple_notes.librarian."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from apple_notes import (
    Note,
    NoteContent,
    GraphLink,
    NotesDatabase,
    RawNoteLink,
    BrokenLink,
    EncryptedNoteError,
    NoteNotFoundError,
)
from apple_notes.librarian import (
    assign_cluster_ids,
    build_candidates,
    compute_connection_counts,
    find_clusters,
    find_duplicate_names,
    find_orphan_notes,
    find_stale_notes,
    get_broken_links,
    get_health_report,
    get_link_suggestions,
    get_orphans,
    is_mention_eligible,
    find_mention_snippets,
    title_word_overlap,
    resolve_note_links,
)


def make_notes():
    """Sample notes for link resolution tests."""
    return [
        Note(name="Alpha", uuid="AAAA-1111", folder="Notes", pk=1),
        Note(name="Beta", uuid="BBBB-2222", folder="Work", pk=2),
        Note(name="Gamma", uuid="CCCC-3333", folder="Notes", pk=3),
    ]


def raw(source_pk, uuid_part, alt_text=None):
    """Build a RawNoteLink with an applenotes: URL for the given uuid."""
    return RawNoteLink(
        source_pk=source_pk,
        target_url=f"applenotes:note/{uuid_part}?ownerIdentifier=x",
        alt_text=alt_text,
    )


class TestResolveNoteLinks:
    """Tests for classifying raw link rows into resolved and broken links."""

    def test_resolved_link_becomes_graphlink(self):
        links, broken = resolve_note_links([raw(1, "bbbb-2222")], make_notes())

        assert broken == []
        assert links == [
            GraphLink(source_name="Alpha", target_name="Beta", source_pk=1, target_pk=2)
        ]

    def test_uuid_matching_is_case_insensitive(self):
        links_lower, _ = resolve_note_links([raw(1, "bbbb-2222")], make_notes())
        links_upper, _ = resolve_note_links([raw(1, "BBBB-2222")], make_notes())

        assert links_lower == links_upper
        assert len(links_lower) == 1

    def test_unknown_target_uuid_becomes_broken_link(self):
        links, broken = resolve_note_links(
            [raw(1, "dead-beef", alt_text="Old Meeting Notes")], make_notes()
        )

        assert links == []
        assert broken == [
            BrokenLink(
                source_name="Alpha",
                source_folder="Notes",
                target_uuid="DEAD-BEEF",
                intended_target_title="Old Meeting Notes",
                occurrences=1,
                source_pk=1,
            )
        ]

    def test_broken_link_alt_text_may_be_null(self):
        _, broken = resolve_note_links([raw(2, "dead-beef")], make_notes())

        assert broken[0].intended_target_title is None
        assert broken[0].source_name == "Beta"
        assert broken[0].source_folder == "Work"

    def test_duplicate_broken_pairs_dedupe_with_occurrences(self):
        _, broken = resolve_note_links(
            [
                raw(1, "dead-beef", "X"),
                raw(1, "DEAD-BEEF", "X"),
                raw(2, "dead-beef", "X"),
            ],
            make_notes(),
        )

        assert len(broken) == 2
        by_source = {b.source_name: b for b in broken}
        assert by_source["Alpha"].occurrences == 2
        assert by_source["Beta"].occurrences == 1

    def test_non_applenotes_url_is_dropped(self):
        rows = [RawNoteLink(source_pk=1, target_url="https://example.com/x")]
        links, broken = resolve_note_links(rows, make_notes())

        assert links == []
        assert broken == []

    def test_unknown_source_pk_is_dropped(self):
        links, broken = resolve_note_links([raw(99, "bbbb-2222")], make_notes())

        assert links == []
        assert broken == []


class TestGetAllLinksDelegation:
    """get_all_links must delegate to get_raw_note_links + resolve_note_links."""

    def test_get_all_links_resolves_raw_rows_and_drops_broken(self):
        db = NotesDatabase(db_path="/nonexistent/NoteStore.sqlite")
        db.get_all_notes = MagicMock(return_value=make_notes())
        db.get_raw_note_links = MagicMock(
            return_value=[raw(1, "bbbb-2222"), raw(1, "dead-beef", "Gone")]
        )

        notes, links = db.get_all_links()

        assert notes == make_notes()
        assert links == [
            GraphLink(source_name="Alpha", target_name="Beta", source_pk=1, target_pk=2)
        ]


def link(source, target, source_pk, target_pk):
    return GraphLink(
        source_name=source,
        target_name=target,
        source_pk=source_pk,
        target_pk=target_pk,
    )


class TestFindOrphanNotes:
    """Orphan = zero incoming AND zero outgoing resolved links."""

    def test_note_with_no_links_is_orphan(self):
        notes = make_notes()
        links = [link("Alpha", "Beta", 1, 2)]

        orphans = find_orphan_notes(notes, links)

        assert [n.name for n in orphans] == ["Gamma"]

    def test_link_source_and_target_are_not_orphans(self):
        notes = make_notes()
        links = [link("Alpha", "Beta", 1, 2)]

        orphan_names = {n.name for n in find_orphan_notes(notes, links)}

        assert "Alpha" not in orphan_names
        assert "Beta" not in orphan_names

    def test_no_links_means_all_notes_are_orphans(self):
        notes = make_notes()

        orphans = find_orphan_notes(notes, [])

        assert orphans == notes


class TestFindStaleNotes:
    """Stale = modified older than now - threshold_days; unknown counts as stale."""

    NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)

    def note(self, name, modified):
        return Note(
            name=name,
            uuid=name,
            folder="Notes",
            pk=hash(name) % 1000,
            modified=modified,
        )

    def test_note_older_than_threshold_is_stale(self):
        old = self.note("Old", "2026-01-01T12:00:00Z")  # 182 days before NOW

        stale = find_stale_notes([old], threshold_days=180, now=self.NOW)

        assert stale == [old]

    def test_note_within_threshold_is_not_stale(self):
        recent = self.note("Recent", "2026-01-05T12:00:00Z")  # 178 days before NOW

        stale = find_stale_notes([recent], threshold_days=180, now=self.NOW)

        assert stale == []

    def test_unknown_modified_is_stale_and_sorts_first(self):
        unknown = self.note("Unknown", None)
        old = self.note("Old", "2026-01-01T12:00:00Z")

        stale = find_stale_notes([old, unknown], threshold_days=180, now=self.NOW)

        assert [n.name for n in stale] == ["Unknown", "Old"]

    def test_stale_notes_sorted_oldest_first(self):
        older = self.note("Older", "2025-06-01T12:00:00Z")
        old = self.note("Old", "2026-01-01T12:00:00Z")

        stale = find_stale_notes([old, older], threshold_days=180, now=self.NOW)

        assert [n.name for n in stale] == ["Older", "Old"]


class TestComputeConnectionCounts:
    """Degree = distinct undirected neighbors, self-links ignored."""

    def test_double_links_between_same_pair_count_once(self):
        notes = make_notes()
        links = [link("Alpha", "Beta", 1, 2), link("Alpha", "Beta", 1, 2)]

        counts = compute_connection_counts(notes, links)

        assert counts[1] == 1
        assert counts[2] == 1

    def test_reciprocal_links_count_once(self):
        notes = make_notes()
        links = [link("Alpha", "Beta", 1, 2), link("Beta", "Alpha", 2, 1)]

        counts = compute_connection_counts(notes, links)

        assert counts[1] == 1
        assert counts[2] == 1

    def test_self_links_are_ignored(self):
        notes = make_notes()
        links = [link("Alpha", "Alpha", 1, 1)]

        counts = compute_connection_counts(notes, links)

        assert counts[1] == 0

    def test_unlinked_notes_have_zero_count(self):
        counts = compute_connection_counts(make_notes(), [])

        assert counts == {1: 0, 2: 0, 3: 0}


class TestFindClusters:
    """Clusters = connected components (size >= 2); hub = max-degree member."""

    def cluster_fixture(self):
        notes = [
            Note(name=n, uuid=n.lower(), folder="Notes", pk=i)
            for i, n in enumerate(["A", "B", "C", "D", "E", "F"], start=1)
        ]
        links = [
            link("A", "B", 1, 2),
            link("B", "C", 2, 3),
            link("D", "E", 4, 5),
        ]
        return notes, links

    def test_components_found_with_hub_and_size(self):
        notes, links = self.cluster_fixture()

        clusters = find_clusters(notes, links)

        assert clusters == [
            {"hub": "B", "size": 3},
            {"hub": "D", "size": 2},
        ]

    def test_singletons_are_omitted(self):
        notes, links = self.cluster_fixture()

        clusters = find_clusters(notes, links)

        assert all(c["size"] >= 2 for c in clusters)

    def test_hub_tie_breaks_alphabetically(self):
        notes = [
            Note(name="Zed", uuid="z", folder="Notes", pk=1),
            Note(name="Ann", uuid="a", folder="Notes", pk=2),
        ]
        links = [link("Zed", "Ann", 1, 2)]

        clusters = find_clusters(notes, links)

        assert clusters == [{"hub": "Ann", "size": 2}]


def _community_notes(pks):
    return [
        Note(name=f"N{pk:02d}", uuid=f"uuid-{pk}", folder="Notes", pk=pk) for pk in pks
    ]


def _plink(a, b):
    return link(f"N{a:02d}", f"N{b:02d}", a, b)


def _ring_with_chords(pks):
    """Dense group: ring plus +2 chords → every member has degree 4."""
    links = []
    n = len(pks)
    for i in range(n):
        links.append(_plink(pks[i], pks[(i + 1) % n]))
        links.append(_plink(pks[i], pks[(i + 2) % n]))
    return links


class TestCommunityDetection:
    """Components larger than CLUSTER_SPLIT_THRESHOLD are subdivided into
    communities via deterministic greedy modularity (issue #131)."""

    def bridged_groups_fixture(self, group_size=16):
        group1 = list(range(1, group_size + 1))
        group2 = list(range(group_size + 1, 2 * group_size + 1))
        notes = _community_notes(group1 + group2)
        links = _ring_with_chords(group1) + _ring_with_chords(group2)
        links.append(_plink(group1[-1], group2[0]))  # single bridge edge
        return notes, links, set(group1), set(group2)

    def test_large_component_splits_without_crossing_bridge(self):
        notes, links, group1, group2 = self.bridged_groups_fixture(16)  # 32 > 30

        ids = assign_cluster_ids(notes, links)

        communities = {}
        for pk, cid in ids.items():
            communities.setdefault(cid, set()).add(pk)
        assert len(communities) > 1  # the component was subdivided
        for members in communities.values():
            assert members <= group1 or members <= group2  # no cross-bridge merge

    def test_component_at_threshold_stays_whole(self):
        notes, links, _, _ = self.bridged_groups_fixture(15)  # 30 <= threshold

        ids = assign_cluster_ids(notes, links)

        assert set(ids.values()) == {0}

    def test_determinism(self):
        notes, links, _, _ = self.bridged_groups_fixture(16)

        assert find_clusters(notes, links) == find_clusters(notes, links)
        assert assign_cluster_ids(notes, links) == assign_cluster_ids(notes, links)

    def test_hub_star_stays_one_community(self):
        pks = list(range(1, 36))  # 35-node star: hub pk=1
        notes = _community_notes(pks)
        links = [_plink(1, spoke) for spoke in pks[1:]]

        ids = assign_cluster_ids(notes, links)

        assert set(ids.values()) == {0}

    def test_hub_has_max_intra_cluster_degree(self):
        # In a subdivided component, hub rank must come from links INSIDE the
        # community — a boundary node's cross-community links don't count.
        notes, links, _, _ = self.bridged_groups_fixture(16)

        ids = assign_cluster_ids(notes, links)
        clusters = find_clusters(notes, links)

        adjacency = {n.pk: set() for n in notes}
        for l in links:
            adjacency[l.source_pk].add(l.target_pk)
            adjacency[l.target_pk].add(l.source_pk)
        pk_by_name = {n.name: n.pk for n in notes}
        members_by_id = {}
        for pk, cid in ids.items():
            members_by_id.setdefault(cid, set()).add(pk)

        for cluster in clusters:
            hub_pk = pk_by_name[cluster["hub"]]
            members = members_by_id[ids[hub_pk]]
            intra = {pk: len(adjacency[pk] & members) for pk in members}
            assert intra[hub_pk] == max(intra.values())

    def test_hub_with_three_dense_groups_splits(self):
        groups = [
            list(range(2, 14)),
            list(range(14, 26)),
            list(range(26, 38)),
        ]
        notes = _community_notes([1] + [pk for g in groups for pk in g])
        links = []
        for g in groups:
            links.extend(_ring_with_chords(g))
        for pk in range(2, 38):
            links.append(_plink(1, pk))  # hub links to every note

        ids = assign_cluster_ids(notes, links)

        communities = {}
        for pk, cid in ids.items():
            communities.setdefault(cid, set()).add(pk)
        assert len(communities) == 3
        for g in groups:
            group_ids = {ids[pk] for pk in g}
            assert len(group_ids) == 1  # each dense group lands intact


class TestFindDuplicateNames:
    """Duplicate names group case-insensitively with uuids for disambiguation."""

    def test_case_insensitive_duplicates_grouped(self):
        notes = [
            Note(name="Groceries", uuid="u1", folder="Personal", pk=1),
            Note(name="groceries", uuid="u2", folder="Work", pk=2),
            Note(name="Unique", uuid="u3", folder="Notes", pk=3),
        ]

        duplicates = find_duplicate_names(notes)

        assert duplicates == [
            {
                "name": "groceries",
                "count": 2,
                "notes": [
                    {"name": "Groceries", "folder": "Personal", "uuid": "u1"},
                    {"name": "groceries", "folder": "Work", "uuid": "u2"},
                ],
            }
        ]

    def test_no_duplicates_returns_empty(self):
        assert find_duplicate_names(make_notes()) == []


def make_corpus():
    """Notes/links fixture for orchestrator tests.

    Hub -> Spoke1, Hub -> Spoke2 (resolved), Hub -> DEAD-BEEF (broken),
    ArchLinked -> Archived (resolved, but Archived filtered out by "Archive"),
    Archived -> DEAD-BEEF (broken, source filtered out).
    Lonely has no links at all.
    """
    notes = [
        Note(
            name="Hub",
            uuid="AA-1",
            folder="Notes",
            pk=1,
            modified="2026-06-30T10:00:00Z",
        ),
        Note(
            name="Spoke1",
            uuid="BB-1",
            folder="Notes",
            pk=2,
            modified="2026-06-01T10:00:00Z",
        ),
        Note(
            name="Spoke2",
            uuid="BB-2",
            folder="Notes",
            pk=3,
            modified="2026-05-01T10:00:00Z",
        ),
        Note(
            name="Lonely",
            uuid="CC-1",
            folder="Notes",
            pk=4,
            modified="2025-01-01T10:00:00Z",
        ),
        Note(
            name="Archived",
            uuid="DD-1",
            folder="Other/Archive",
            pk=5,
            modified="2026-06-20T10:00:00Z",
        ),
        Note(name="ArchLinked", uuid="EE-1", folder="Notes", pk=6, modified=None),
    ]
    raw_links = [
        RawNoteLink(1, "applenotes:note/bb-1", None),
        RawNoteLink(1, "applenotes:note/bb-2", None),
        RawNoteLink(1, "applenotes:note/dead-beef", "Ghost Note"),
        RawNoteLink(6, "applenotes:note/dd-1", None),
        RawNoteLink(5, "applenotes:note/dead-beef", "Ghost Note"),
    ]
    return notes, raw_links


def patch_librarian_db(monkeypatch, notes, raw_links):
    """Patch NotesDatabase as used inside apple_notes.librarian."""
    import apple_notes.librarian as librarian_module

    mock_cls = MagicMock()
    mock_db = mock_cls.return_value.__enter__.return_value
    mock_db.get_all_notes.return_value = notes
    mock_db.get_raw_note_links.return_value = raw_links
    monkeypatch.setattr(librarian_module, "NotesDatabase", mock_cls)
    return mock_db


class TestGetOrphans:
    def test_orphans_relative_to_filtered_view(self, monkeypatch):
        notes, raw_links = make_corpus()
        patch_librarian_db(monkeypatch, notes, raw_links)

        result = get_orphans(exclude_folders=["Archive"])

        assert result == {
            "orphans": [
                {
                    "name": "Lonely",
                    "folder": "Notes",
                    "modified": "2025-01-01T10:00:00Z",
                },
                {"name": "ArchLinked", "folder": "Notes", "modified": None},
            ],
            "count": 2,
            "total_notes": 5,
        }

    def test_no_filters_keeps_archived_link_alive(self, monkeypatch):
        notes, raw_links = make_corpus()
        patch_librarian_db(monkeypatch, notes, raw_links)

        result = get_orphans()

        assert [o["name"] for o in result["orphans"]] == ["Lonely"]
        assert result["total_notes"] == 6


class TestGetBrokenLinks:
    def test_broken_links_reported_with_intended_title(self, monkeypatch):
        notes, raw_links = make_corpus()
        patch_librarian_db(monkeypatch, notes, raw_links)

        result = get_broken_links(exclude_folders=["Archive"])

        assert result == {
            "broken_links": [
                {
                    "source_note": "Hub",
                    "source_folder": "Notes",
                    "intended_target_title": "Ghost Note",
                    "target_uuid": "DEAD-BEEF",
                    "occurrences": 1,
                },
            ],
            "count": 1,
        }

    def test_filtered_out_source_is_dropped(self, monkeypatch):
        notes, raw_links = make_corpus()
        patch_librarian_db(monkeypatch, notes, raw_links)

        result = get_broken_links()

        assert {b["source_note"] for b in result["broken_links"]} == {"Hub", "Archived"}


class TestGetHealthReport:
    NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)

    def test_full_report_shape(self, monkeypatch):
        notes, raw_links = make_corpus()
        patch_librarian_db(monkeypatch, notes, raw_links)

        report = get_health_report(exclude_folders=["Archive"], now=self.NOW)

        assert report["total_notes"] == 5
        assert report["total_links"] == 2  # Hub->Spoke1, Hub->Spoke2
        assert report["orphan_notes"] == {"count": 2, "notes": ["Lonely", "ArchLinked"]}
        assert report["broken_links"]["count"] == 1
        assert report["broken_links"]["links"][0]["source_note"] == "Hub"
        assert report["stale_notes"]["threshold_days"] == 180
        # ArchLinked (modified unknown) sorts first, then Lonely
        assert [n["name"] for n in report["stale_notes"]["notes"]] == [
            "ArchLinked",
            "Lonely",
        ]
        assert report["stale_notes"]["count"] == 2
        assert report["top_connected"] == [
            {"name": "Hub", "connections": 2},
            {"name": "Spoke1", "connections": 1},
            {"name": "Spoke2", "connections": 1},
        ]
        assert report["clusters"] == [{"hub": "Hub", "size": 3}]
        assert report["duplicate_names"] == []

    def test_top_n_caps_lists(self, monkeypatch):
        notes, raw_links = make_corpus()
        patch_librarian_db(monkeypatch, notes, raw_links)

        report = get_health_report(exclude_folders=["Archive"], top_n=1, now=self.NOW)

        assert len(report["top_connected"]) == 1
        assert len(report["stale_notes"]["notes"]) == 1
        assert report["stale_notes"]["count"] == 2  # full count preserved

    def test_duplicate_names_surface(self, monkeypatch):
        notes = [
            Note(name="Todo", uuid="t1", folder="Notes", pk=1),
            Note(name="todo", uuid="t2", folder="Work", pk=2),
        ]
        patch_librarian_db(monkeypatch, notes, [])

        report = get_health_report(now=self.NOW)

        assert report["duplicate_names"][0]["name"] == "todo"
        assert report["duplicate_names"][0]["count"] == 2


class TestMentionEligibility:
    """A title participates in mention matching only if specific enough."""

    def test_long_single_token_is_eligible(self):
        assert is_mention_eligible("Kubernetes") is True

    def test_multi_token_title_is_eligible(self):
        assert is_mention_eligible("Grocery List") is True

    def test_short_single_token_is_not_eligible(self):
        assert is_mention_eligible("Home") is False

    def test_stopword_title_is_not_eligible(self):
        assert is_mention_eligible("The") is False

    def test_stopwords_do_not_count_toward_token_minimum(self):
        assert is_mention_eligible("The Log") is False


class TestFindMentionSnippets:
    def test_match_respects_word_boundaries(self):
        assert find_mention_snippets("Docker", "my Dockerfile is here") == []

    def test_match_is_case_insensitive(self):
        snippets = find_mention_snippets("Docker", "we use docker daily")

        assert len(snippets) == 1
        assert "docker" in snippets[0]

    def test_regex_special_chars_in_title_are_literal(self):
        snippets = find_mention_snippets("C++ Tips", "read C++ Tips today")

        assert len(snippets) == 1

    def test_snippet_window_is_bounded_with_ellipses(self):
        body = "x" * 200 + " Kubernetes " + "y" * 200

        snippets = find_mention_snippets("Kubernetes", body)

        assert len(snippets) == 1
        assert snippets[0].startswith("...")
        assert snippets[0].endswith("...")
        assert len(snippets[0]) < 200

    def test_at_most_max_snippets_returned(self):
        body = "Kubernetes here. Kubernetes there. Kubernetes everywhere."

        assert len(find_mention_snippets("Kubernetes", body)) == 2


class TestTitleWordOverlap:
    def test_shared_non_stopword_tokens_reported(self):
        assert title_word_overlap("Helm Charts", "Kubernetes Charts") == ["charts"]

    def test_stopwords_never_overlap(self):
        assert title_word_overlap("The Plan", "The Idea") == []

    def test_only_short_fragments_do_not_fire(self):
        assert title_word_overlap("Plan B", "Option B") == []

    def test_short_fragment_reported_alongside_real_word(self):
        assert title_word_overlap("Plan B", "B Plan") == ["b", "plan"]


class TestBuildCandidates:
    """Candidate recall signals for suggest_links; reasons are transparent."""

    def fixture(self):
        subject = Note(
            name="Kubernetes Setup",
            uuid="AA-1",
            folder="Work",
            pk=1,
            modified="2026-06-01T10:00:00Z",
        )
        others = [
            Note(
                name="Helm Charts",
                uuid="BB-1",
                folder="Work",
                pk=2,
                modified="2026-05-01T10:00:00Z",
            ),
            Note(
                name="Docker Notes",
                uuid="CC-1",
                folder="Personal",
                pk=3,
                modified="2026-04-01T10:00:00Z",
            ),
            Note(
                name="Setup Guide",
                uuid="DD-1",
                folder="Personal",
                pk=4,
                modified="2026-03-01T10:00:00Z",
            ),
            Note(
                name="Homelab",
                uuid="EE-1",
                folder="Work",
                pk=5,
                modified="2026-02-01T10:00:00Z",
            ),
            Note(
                name="Random",
                uuid="FF-1",
                folder="Work",
                pk=6,
                modified="2026-01-01T10:00:00Z",
            ),
            Note(
                name="Zebra",
                uuid="00-1",
                folder="Personal",
                pk=7,
                modified="2026-01-15T10:00:00Z",
            ),
        ]
        notes = [subject] + others
        links = [
            link("Kubernetes Setup", "Homelab", 1, 5),
            link("Kubernetes Setup", "Docker Notes", 1, 3),
            link("Helm Charts", "Homelab", 2, 5),
            link("Helm Charts", "Kubernetes Setup", 2, 1),
        ]
        subject_text = "Deploy with Helm Charts once the cluster is ready."
        note_texts = {
            2: "See Kubernetes Setup for the base cluster config.",
            4: "Nothing relevant here.",
        }
        return subject, notes, links, subject_text, note_texts

    def build(self, max_candidates=25):
        subject, notes, links, subject_text, note_texts = self.fixture()
        return build_candidates(
            subject,
            subject_text,
            notes,
            links,
            note_texts,
            max_candidates=max_candidates,
        )

    def test_all_signals_fire_for_strong_candidate(self):
        candidates = self.build()

        helm = candidates[0]
        assert helm["name"] == "Helm Charts"
        assert helm["folder"] == "Work"
        types = [r["type"] for r in helm["reasons"]]
        assert types == [
            "forward_mention",
            "reverse_mention",
            "co_citation",
            "same_folder",
            "backlink_not_reciprocated",
        ]
        forward = helm["reasons"][0]
        assert "Helm Charts" in forward["snippets"][0]
        reverse = helm["reasons"][1]
        assert "Kubernetes Setup" in reverse["snippets"][0]
        co_citation = helm["reasons"][2]
        assert co_citation["shared_neighbors"] == ["Homelab"]

    def test_subject_and_already_linked_notes_are_excluded(self):
        names = {c["name"] for c in self.build()}

        assert "Kubernetes Setup" not in names
        assert "Docker Notes" not in names
        assert "Homelab" not in names

    def test_title_overlap_reason(self):
        candidates = self.build()

        setup_guide = next(c for c in candidates if c["name"] == "Setup Guide")
        assert setup_guide["reasons"] == [
            {"type": "title_overlap", "shared_words": ["setup"]}
        ]

    def test_no_signal_notes_are_not_candidates(self):
        assert "Zebra" not in {c["name"] for c in self.build()}

    def test_same_folder_only_candidates_sort_last(self):
        names = [c["name"] for c in self.build()]

        assert names == ["Helm Charts", "Setup Guide", "Random"]

    def test_max_candidates_caps_list(self):
        assert len(self.build(max_candidates=2)) == 2

    def test_ineligible_candidate_title_never_forward_matches(self):
        subject, notes, links, _, note_texts = self.fixture()
        notes = notes + [Note(name="Logs", uuid="11-1", folder="Personal", pk=8)]

        candidates = build_candidates(
            subject,
            "check the cluster logs daily.",
            notes,
            links,
            note_texts,
            max_candidates=25,
        )

        assert "Logs" not in {c["name"] for c in candidates}


def make_note_content(name, uuid, folder, text):
    return NoteContent(
        name=name, uuid=uuid, folder=folder, raw_text=text, resolved_text=text
    )


class TestGetLinkSuggestions:
    def suggestion_fixture(
        self, monkeypatch, subject_text="Deploy with Helm Charts soon."
    ):
        import apple_notes.librarian as librarian_module

        notes = [
            Note(
                name="Kubernetes Setup",
                uuid="AA-1",
                folder="Work",
                pk=1,
                modified="2026-06-01T10:00:00Z",
            ),
            Note(
                name="Helm Charts",
                uuid="BB-1",
                folder="Work",
                pk=2,
                modified="2026-05-01T10:00:00Z",
            ),
            Note(name="Secret", uuid="CC-1", folder="Personal", pk=3),
        ]
        patch_librarian_db(monkeypatch, notes, [])

        texts = {
            "Kubernetes Setup": subject_text,
            "Helm Charts": "See Kubernetes Setup for cluster config.",
        }

        def fake_get_note_content_for_note(note):
            if note.name == "Secret":
                raise EncryptedNoteError("encrypted")
            return make_note_content(
                note.name, note.uuid, note.folder, texts[note.name]
            )

        mock_reader_cls = MagicMock()
        mock_reader_cls.return_value.get_note_content_for_note.side_effect = (
            fake_get_note_content_for_note
        )
        monkeypatch.setattr(librarian_module, "NoteContentReader", mock_reader_cls)
        return notes

    def test_returns_subject_content_and_candidates(self, monkeypatch):
        self.suggestion_fixture(monkeypatch)

        result = get_link_suggestions("Kubernetes Setup")

        assert result["note"]["name"] == "Kubernetes Setup"
        assert result["note"]["content"] == "Deploy with Helm Charts soon."
        assert result["note"]["content_truncated"] is False
        assert result["note"]["existing_outgoing_links"] == []
        assert result["note"]["existing_backlinks"] == []

        helm = next(c for c in result["candidates"] if c["name"] == "Helm Charts")
        types = {r["type"] for r in helm["reasons"]}
        assert {"forward_mention", "reverse_mention", "same_folder"} <= types
        assert result["candidate_count"] == len(result["candidates"])

    def test_encrypted_candidates_are_counted_as_skipped(self, monkeypatch):
        self.suggestion_fixture(monkeypatch)

        result = get_link_suggestions("Kubernetes Setup")

        assert result["skipped_notes"] == {
            "count": 1,
            "reason": "encrypted_or_unparseable",
        }

    def test_subject_content_is_truncated_at_cap(self, monkeypatch):
        self.suggestion_fixture(monkeypatch, subject_text="x" * 12_000)

        result = get_link_suggestions("Kubernetes Setup")

        assert len(result["note"]["content"]) == 10_000
        assert result["note"]["content_truncated"] is True

    def test_unknown_note_raises(self, monkeypatch):
        self.suggestion_fixture(monkeypatch)

        with pytest.raises(NoteNotFoundError):
            get_link_suggestions("No Such Note")

    def test_subject_lookup_is_case_insensitive(self, monkeypatch):
        self.suggestion_fixture(monkeypatch)

        result = get_link_suggestions("kubernetes setup")

        assert result["note"]["name"] == "Kubernetes Setup"


class TestGetLinkSuggestionsByPk:
    """note_pk addressing resolves the right subject among duplicate titles."""

    def fixture(self, monkeypatch):
        import apple_notes.librarian as librarian_module

        notes = [
            Note(name="Dup", uuid="AA-1", folder="One", pk=1),
            Note(name="Dup", uuid="BB-1", folder="Two", pk=2),
        ]
        patch_librarian_db(monkeypatch, notes, [])

        def fake_get_note_content_for_note(note):
            return make_note_content(
                note.name, note.uuid, note.folder, f"body {note.pk}"
            )

        mock_reader_cls = MagicMock()
        mock_reader_cls.return_value.get_note_content_for_note.side_effect = (
            fake_get_note_content_for_note
        )
        monkeypatch.setattr(librarian_module, "NoteContentReader", mock_reader_cls)
        return notes

    def test_pk_selects_exact_note_among_duplicate_titles(self, monkeypatch):
        self.fixture(monkeypatch)

        result = get_link_suggestions(note_pk=2)

        assert result["note"]["folder"] == "Two"
        assert result["note"]["content"] == "body 2"

    def test_unknown_pk_raises(self, monkeypatch):
        self.fixture(monkeypatch)

        with pytest.raises(NoteNotFoundError):
            get_link_suggestions(note_pk=99)

    def test_neither_name_nor_pk_raises_value_error(self, monkeypatch):
        self.fixture(monkeypatch)

        with pytest.raises(ValueError):
            get_link_suggestions()


class TestGetNoteContentForNote:
    """Pk-addressed content reads must not re-resolve by (ambiguous) title."""

    def test_reads_content_without_name_lookup(self):
        from apple_notes import NoteContentReader

        reader = NoteContentReader.__new__(NoteContentReader)
        note = Note(name="Todo", uuid="AA-1", folder="Work", pk=42)

        mock_db = MagicMock()
        mock_db.get_note_data.return_value = None  # empty note short-circuit
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        reader.db = mock_db

        content = reader.get_note_content_for_note(note)

        assert content.name == "Todo"
        assert content.uuid == "AA-1"
        mock_db.get_note_by_name.assert_not_called()
        mock_db.get_note_data.assert_called_once_with(42)


class TestSuggestLinksDuplicateTitles:
    """Content must be fetched per-note (by pk), not per-title."""

    def test_duplicate_titled_candidates_get_their_own_text(self, monkeypatch):
        import apple_notes.librarian as librarian_module

        notes = [
            Note(name="Kubernetes Setup", uuid="AA-1", folder="Work", pk=1),
            Note(name="Todo", uuid="BB-1", folder="Work", pk=2),
            Note(name="Todo", uuid="CC-1", folder="Personal", pk=3),
        ]
        patch_librarian_db(monkeypatch, notes, [])

        texts_by_pk = {
            1: "Subject body.",
            2: "Mentions Kubernetes Setup right here.",
            3: "Nothing relevant.",
        }

        def fake_for_note(note):
            return make_note_content(
                note.name, note.uuid, note.folder, texts_by_pk[note.pk]
            )

        mock_reader_cls = MagicMock()
        mock_reader_cls.return_value.get_note_content_for_note.side_effect = (
            fake_for_note
        )
        monkeypatch.setattr(librarian_module, "NoteContentReader", mock_reader_cls)

        result = get_link_suggestions("Kubernetes Setup")

        reverse_by_name_pk = {
            (c["name"], c["folder"]): {r["type"] for r in c["reasons"]}
            for c in result["candidates"]
        }
        assert "reverse_mention" in reverse_by_name_pk[("Todo", "Work")]
        # The other Todo (pk 3) has no matching text, so it must not carry a
        # reverse_mention borrowed from its twin.
        personal = reverse_by_name_pk.get(("Todo", "Personal"), set())
        assert "reverse_mention" not in personal


class TestSuggestLinksScanSkipsLinkedNotes:
    """Bodies of already-linked notes are never parsed (they can't be candidates)."""

    def test_outgoing_linked_note_content_not_fetched(self, monkeypatch):
        import apple_notes.librarian as librarian_module

        notes = [
            Note(name="Kubernetes Setup", uuid="AA-1", folder="Work", pk=1),
            Note(name="Docker Notes", uuid="BB-1", folder="Work", pk=2),
            Note(name="Helm Charts", uuid="CC-1", folder="Work", pk=3),
        ]
        raw_links = [RawNoteLink(1, "applenotes:note/bb-1", None)]
        patch_librarian_db(monkeypatch, notes, raw_links)

        fetched = []

        def fake_for_note(note):
            fetched.append(note.name)
            return make_note_content(note.name, note.uuid, note.folder, "text")

        mock_reader_cls = MagicMock()
        mock_reader_cls.return_value.get_note_content_for_note.side_effect = (
            fake_for_note
        )
        monkeypatch.setattr(librarian_module, "NoteContentReader", mock_reader_cls)

        get_link_suggestions("Kubernetes Setup")

        assert "Docker Notes" not in fetched  # already linked -> never a candidate
        assert "Helm Charts" in fetched


class TestFindStaleNotesMalformedTimestamp:
    """A malformed modified timestamp must be skipped, not crash the report."""

    NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)

    def test_unparseable_modified_is_skipped_without_error(self):
        malformed = Note(
            name="Bad", uuid="b", folder="Notes", pk=1, modified="not-a-timestamp"
        )
        old = Note(
            name="Old", uuid="o", folder="Notes", pk=2, modified="2026-01-01T12:00:00Z"
        )

        stale = find_stale_notes([malformed, old], threshold_days=180, now=self.NOW)

        assert [n.name for n in stale] == ["Old"]
