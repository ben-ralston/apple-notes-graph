"""Note embeddings for semantic search (issue #117).

fastembed (ONNX all-MiniLM-L6-v2) embeds title+folder+body-excerpt per note
into a JSON cache under the app config dir. Builds are incremental by the
note's modified timestamp; the cache is written atomically so a crashed
build never leaves a torn file. fastembed is imported lazily so the module
(and everything importing it) works without the model - tests inject a fake
embedder, and CI never downloads anything.
"""

import json
import math
import os
from pathlib import Path
from typing import Callable, List, Optional

from . import unified_log
from .config import CONFIG_DIR
from .database import NotesDatabase
from .exceptions import EncryptedNoteError, ProtobufParseError
from .note_content import NoteContentReader

EMBEDDINGS_PATH = CONFIG_DIR / "embeddings.json"
MODEL_CACHE_DIR = CONFIG_DIR / "fastembed"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
CACHE_VERSION = 1
MAX_EMBED_CHARS = 1500

Embedder = Callable[[List[str]], List[List[float]]]


def _default_embedder() -> Embedder:
    from fastembed import TextEmbedding  # lazy: ~90MB model on first use

    model = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(MODEL_CACHE_DIR))

    def embed(texts: List[str]) -> List[List[float]]:
        return [[float(x) for x in vec] for vec in model.embed(texts)]

    return embed


def _load_cache(cache_path: Path) -> dict:
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if cache.get("version") != CACHE_VERSION or cache.get("model") != MODEL_NAME:
        return {}
    return cache


def build_embeddings(
    db_path: Optional[str] = None,
    cache_path: Optional[str] = None,
    embedder: Optional[Embedder] = None,
    max_chars: int = MAX_EMBED_CHARS,
) -> dict:
    """Build/refresh the embedding cache. Returns counts for logging."""
    cache_path = Path(cache_path or EMBEDDINGS_PATH)
    with NotesDatabase(db_path) as db:
        notes = db.get_all_notes()
    old = _load_cache(cache_path).get("notes", {})

    reader = NoteContentReader(db_path)
    entries: dict = {}
    to_embed: list = []  # (pk_key, text)
    reused = skipped = 0
    for note in notes:
        key = str(note.pk)
        modified = str(note.modified or "")
        prev = old.get(key)
        if prev and prev.get("modified") == modified:
            entries[key] = prev
            reused += 1
            continue
        try:
            body = reader.get_note_content_for_note(note).resolved_text
        except (EncryptedNoteError, ProtobufParseError) as e:
            unified_log.log_parse_error("build_embeddings", note.pk, e)
            skipped += 1
            continue
        entries[key] = {
            "name": note.name,
            "folder": note.folder,
            "modified": modified,
            "vector": None,
        }
        to_embed.append((key, f"{note.name}\n{note.folder or ''}\n{body[:max_chars]}"))

    if to_embed:
        embed = embedder or _default_embedder()
        vectors = embed([text for _, text in to_embed])
        for (key, _), vec in zip(to_embed, vectors):
            entries[key]["vector"] = list(vec)

    dim = next((len(e["vector"]) for e in entries.values() if e.get("vector")), 0)
    payload = {
        "version": CACHE_VERSION,
        "model": MODEL_NAME,
        "dim": dim,
        "notes": entries,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(payload, f)
    os.replace(tmp_path, cache_path)
    return {
        "embedded": len(to_embed),
        "reused": reused,
        "skipped": skipped,
        "total": len(entries),
    }


def semantic_search(
    query: str,
    top_k: int = 8,
    cache_path: Optional[str] = None,
    embedder: Optional[Embedder] = None,
) -> dict:
    """Cosine-rank cached note vectors against the query.

    Pure-python math: ~800x384 floats is well under 50ms, not worth a numpy
    dependency at this layer.
    """
    cache = _load_cache(Path(cache_path or EMBEDDINGS_PATH))
    notes = cache.get("notes") or {}
    if not notes:
        return {"error": "embeddings not built"}

    embed = embedder or _default_embedder()
    qvec = embed([query])[0]
    qnorm = math.sqrt(sum(x * x for x in qvec)) or 1.0

    scored = []
    for pk, entry in notes.items():
        vec = entry.get("vector")
        if not vec:
            continue
        dot = sum(a * b for a, b in zip(qvec, vec))
        norm = math.sqrt(sum(b * b for b in vec)) or 1.0
        scored.append((dot / (qnorm * norm), int(pk), entry))
    scored.sort(key=lambda item: -item[0])
    return {
        "results": [
            {"pk": pk, "name": e["name"], "folder": e["folder"], "score": round(s, 4)}
            for s, pk, e in scored[:top_k]
        ]
    }
