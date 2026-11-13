"""Embedding cache build/search with an injected fake embedder (no model)."""

import json
from unittest.mock import MagicMock, patch

import pytest


def fake_embedder(texts):
    # Deterministic 3-dim vectors keyed on text shape.
    return [[float(len(t)), float(t.count("a")), 1.0] for t in texts]


def make_note(pk, name, folder="F", modified="2026-01-01"):
    note = MagicMock()
    note.pk = pk
    note.name = name
    note.folder = folder
    note.modified = modified
    return note


def build(tmp_path, notes, cache_path=None, embedder=fake_embedder, bodies=None):
    cache_path = cache_path or tmp_path / "emb.json"
    db = MagicMock()
    db.__enter__ = MagicMock(return_value=db)
    db.__exit__ = MagicMock(return_value=False)
    db.get_all_notes.return_value = notes

    reader = MagicMock()

    def content_for(note):
        content = MagicMock()
        content.resolved_text = (bodies or {}).get(note.pk, f"body of {note.name}")
        return content

    reader.get_note_content_for_note.side_effect = content_for

    with (
        patch("apple_notes.embeddings.NotesDatabase", return_value=db),
        patch("apple_notes.embeddings.NoteContentReader", return_value=reader),
    ):
        from apple_notes.embeddings import build_embeddings

        return build_embeddings(cache_path=cache_path, embedder=embedder), cache_path


class TestBuildEmbeddings:
    def test_fresh_build_embeds_all(self, tmp_path):
        stats, cache_path = build(tmp_path, [make_note(1, "A"), make_note(2, "B")])
        assert stats == {"embedded": 2, "reused": 0, "skipped": 0, "total": 2}
        cache = json.loads(cache_path.read_text())
        assert set(cache["notes"]) == {"1", "2"}
        assert cache["dim"] == 3

    def test_incremental_reembeds_only_changed(self, tmp_path):
        notes = [make_note(1, "A"), make_note(2, "B")]
        _, cache_path = build(tmp_path, notes)
        notes[1].modified = "2026-02-02"
        calls = []

        def counting(texts):
            calls.extend(texts)
            return fake_embedder(texts)

        stats, _ = build(tmp_path, notes, cache_path=cache_path, embedder=counting)
        assert stats["embedded"] == 1 and stats["reused"] == 1
        assert len(calls) == 1

    def test_deleted_notes_pruned(self, tmp_path):
        _, cache_path = build(tmp_path, [make_note(1, "A"), make_note(2, "B")])
        build(tmp_path, [make_note(1, "A")], cache_path=cache_path)
        cache = json.loads(cache_path.read_text())
        assert set(cache["notes"]) == {"1"}

    def test_model_mismatch_full_rebuild(self, tmp_path):
        _, cache_path = build(tmp_path, [make_note(1, "A")])
        cache = json.loads(cache_path.read_text())
        cache["model"] = "old-model"
        cache_path.write_text(json.dumps(cache))
        stats, _ = build(tmp_path, [make_note(1, "A")], cache_path=cache_path)
        assert stats["embedded"] == 1 and stats["reused"] == 0

    def test_embedder_failure_leaves_no_partial_file(self, tmp_path):
        def broken(texts):
            raise RuntimeError("no model")

        with pytest.raises(RuntimeError):
            build(tmp_path, [make_note(1, "A")], embedder=broken)
        assert not (tmp_path / "emb.json").exists()

    def test_unparseable_bodies_skipped(self, tmp_path):
        from apple_notes.exceptions import ProtobufParseError

        db = MagicMock()
        db.__enter__ = MagicMock(return_value=db)
        db.__exit__ = MagicMock(return_value=False)
        db.get_all_notes.return_value = [make_note(1, "A"), make_note(2, "B")]
        reader = MagicMock()

        def content_for(note):
            if note.pk == 2:
                raise ProtobufParseError("bad")
            content = MagicMock()
            content.resolved_text = "ok"
            return content

        reader.get_note_content_for_note.side_effect = content_for
        with (
            patch("apple_notes.embeddings.NotesDatabase", return_value=db),
            patch("apple_notes.embeddings.NoteContentReader", return_value=reader),
        ):
            from apple_notes.embeddings import build_embeddings

            stats = build_embeddings(
                cache_path=tmp_path / "e.json", embedder=fake_embedder
            )
        assert stats["embedded"] == 1 and stats["skipped"] == 1


class TestSemanticSearch:
    def test_ranks_by_cosine_and_respects_top_k(self, tmp_path):
        _, cache_path = build(
            tmp_path,
            [make_note(1, "Aaaa"), make_note(2, "Bb"), make_note(3, "Cccccc")],
        )
        from apple_notes.embeddings import semantic_search

        out = semantic_search(
            "aa", top_k=2, cache_path=cache_path, embedder=fake_embedder
        )
        assert "results" in out and len(out["results"]) == 2
        assert {r["pk"] for r in out["results"]} <= {1, 2, 3}
        assert all("score" in r and "name" in r for r in out["results"])

    def test_missing_cache_is_error(self, tmp_path):
        from apple_notes.embeddings import semantic_search

        out = semantic_search(
            "q", cache_path=tmp_path / "none.json", embedder=fake_embedder
        )
        assert out == {"error": "embeddings not built"}


@pytest.mark.embedding_model
class TestLiveModel:
    def test_real_fastembed_round_trip(self):
        from apple_notes.embeddings import _default_embedder

        vectors = _default_embedder()(["hello world"])
        assert len(vectors) == 1 and len(vectors[0]) == 384
