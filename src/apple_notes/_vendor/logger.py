#!/usr/bin/env python3
"""Stdlib-only client for the unified logger (Cloudflare Worker + D1, #51).

Usage (library):
    from logger import log
    log("my-app", "info", "run started", json_extra={"run_date": "2026-08-09"})

`log()` NEVER raises and is best-effort: a logger outage must not break the
caller (money-path rule). Returns True when delivered, False when the event
could not be delivered.

Env seams (tests only — no caller-facing path/endpoint flags by design):
    LOGGER_URL              endpoint (required; there is no default)
    LOGGER_TOKEN_FILE       write token file (default ~/.config/notes-graph/logger-write.token)
    LOGGER_ADMIN_TOKEN_FILE admin token file (default ~/.config/notes-graph/logger-admin.token)
    LOGGER_QUEUE_FILE       fallback queue (default ~/Library/Application Support/notes-graph/logger-queue.jsonl)
    LOGGER_RETRY_SLEEPS     admin-lane retry backoff, comma-separated seconds
                            (default "1,3,9"; tests set "0,0,0")
    LOGGER_COOLDOWN_MS      in-process failure cooldown (default 60000)
"""

import fcntl
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# No built-in endpoint: reporting is off unless LOGGER_URL is set (see
# apple_notes.unified_log._enabled).
DEFAULT_URL = ""
MAX_MESSAGE_LEN = 8192
# Server limits are JS .length — UTF-16 code units, not Python code points.
MAX_JSON_EXTRA_LEN = 16384  # compact-serialized
MAX_SOURCE_LEN = 128
# The server rejects an empty message; a placeholder keeps the event.
EMPTY_MESSAGE = "(empty message)"
# An oversized json_extra is replaced by a marker carrying this much of its
# serialized form. Escaping at most doubles it, so the marker always fits.
JSON_EXTRA_PREVIEW_LEN = 4096
# Cloudflare's Browser Integrity Check bans the default Python-urllib UA at
# the edge (error 1010 -> 403) before the Worker runs; any custom UA passes.
USER_AGENT = "claude-logger/1.0"
TIMEOUT_S = 3
BATCH_MAX = 100  # server rejects arrays >100; replay chunks at this size
# Replay work per call is capped so the flock hold (and a money-path caller's
# stall behind it) stays bounded — at worst (MAX_CHUNKS_PER_CALL +
# MAX_SPLITS_PER_CALL) * TIMEOUT_S; the remaining backlog persists and drains
# over subsequent calls.
MAX_CHUNKS_PER_CALL = 5
# Chunk halvings (413 / unattributed reject) allowed per call: enough to
# isolate one offender in a full chunk, ceil(log2(BATCH_MAX)).
MAX_SPLITS_PER_CALL = 7
# A persistent failure that classifies as transient (e.g. a revoked token's
# 401) must not grow the queue — and each call's full re-read — forever:
# a call ending in such a failure quarantines the oldest overflow past this.
MAX_QUEUE_EVENTS = 2000

# In-process failure cooldown (logger#9): after a transient failure, calls
# within the window send only their own event, skipping the backlog read and
# replay. Short-lived CLI processes never benefit; long-lived ones do.
_last_transient_at = float("-inf")
# Cooldown appends since the last full-path call; at MAX_QUEUE_EVENTS the
# next call takes the full path so the queue cap still applies.
_cooldown_appends = 0


def _path_from_env(env_var: str, default: str) -> Path:
    return Path(os.environ.get(env_var, os.path.expanduser(default)))


def _url() -> str:
    return os.environ.get("LOGGER_URL", DEFAULT_URL).rstrip("/")


def _write_token_file() -> Path:
    return _path_from_env("LOGGER_TOKEN_FILE", "~/.config/notes-graph/logger-write.token")


def _admin_token_file() -> Path:
    return _path_from_env(
        "LOGGER_ADMIN_TOKEN_FILE", "~/.config/notes-graph/logger-admin.token"
    )


def _queue_file() -> Path:
    return _path_from_env("LOGGER_QUEUE_FILE", "~/Library/Application Support/notes-graph/logger-queue.jsonl")


def _cooldown_s() -> float:
    try:
        return float(os.environ.get("LOGGER_COOLDOWN_MS", "60000")) / 1000
    except ValueError:
        return 60.0


def _in_cooldown() -> bool:
    return time.monotonic() - _last_transient_at < _cooldown_s()


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _clip(text: str, max_units: int) -> str:
    """Clip to max UTF-16 code units (the server's measure) without ending on
    the high half of a surrogate pair, which would store as garbage."""
    data = text.encode("utf-16-le", "surrogatepass")
    cut = max_units * 2
    if len(data) <= cut:
        return text
    if 0xD800 <= int.from_bytes(data[cut - 2 : cut], "little") <= 0xDBFF:
        cut -= 2
    return data[:cut].decode("utf-16-le", "surrogatepass")


def _compact_json(value) -> str:
    # The shape the Worker re-serializes and measures (JSON.stringify).
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _read_token(path: Path) -> str:
    return path.read_text().strip()


_DEFAULT_RETRY_SLEEPS = "1,3,9"


def _retry_sleeps() -> list[float]:
    raw = os.environ.get("LOGGER_RETRY_SLEEPS", _DEFAULT_RETRY_SLEEPS)
    try:
        sleeps = [float(part) for part in raw.split(",") if part.strip()]
    except ValueError:
        sleeps = []
    if not sleeps:
        sleeps = [float(p) for p in _DEFAULT_RETRY_SLEEPS.split(",")]
    return sleeps


def _retryable(error: Exception) -> bool:
    """Transient-only: network-class errors and server-side 429/5xx. Other
    HTTP codes (validation, auth) can never heal by retrying and must
    surface immediately. HTTPError subclasses URLError/OSError — check it
    first."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or error.code >= 500
    return isinstance(error, (urllib.error.URLError, OSError))


def _open_with_retry(request: urllib.request.Request, timeout: float) -> dict:
    """Admin-lane transport (#232): urlopen + JSON-decode with bounded
    retries. The money-path log() lane deliberately does NOT use this —
    its queue+replay covers outages without stalling callers. Exhaustion
    re-raises the last error unchanged so the sentinel keeps deciding
    severity."""
    sleeps = _retry_sleeps()
    for attempt in range(len(sleeps) + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except Exception as error:
            if attempt >= len(sleeps) or not _retryable(error):
                raise
            time.sleep(sleeps[attempt])


def _quarantine(queue_file: Path, lines: list[str], reason: str) -> None:
    """Park permanently-undeliverable material in a .rejected sidecar.

    Nothing is silently dropped: corrupt queue lines and server-rejected
    chunks land here for manual recovery instead of wedging or vanishing.
    """
    sidecar = queue_file.with_name(queue_file.name + ".rejected")
    with sidecar.open("a") as f:
        for line in lines:
            f.write(line.rstrip("\n") + "\n")
    print(
        f"logger: {len(lines)} event(s) quarantined to {sidecar} ({reason})",
        file=sys.stderr,
    )


def _read_queue(queue_file: Path) -> list[dict]:
    if not queue_file.exists():
        return []
    events = []
    corrupt = []
    for line in queue_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            corrupt.append(line)
    if corrupt:
        _quarantine(queue_file, corrupt, "corrupt queue line")
        # Rewrite immediately (caller holds the lock) so a corrupt line is
        # quarantined exactly once — the append-only failure paths would
        # otherwise leave it in place and re-quarantine it every call.
        _write_queue(queue_file, events)
    return events


def _write_queue(queue_file: Path, events: list[dict]) -> None:
    """Atomic rewrite (temp file + rename): a crash mid-write must never
    destroy the queued events the old file held."""
    if not events:
        queue_file.unlink(missing_ok=True)
        return
    tmp = queue_file.with_name(queue_file.name + ".tmp")
    tmp.write_text("".join(json.dumps(e) + "\n" for e in events))
    os.replace(tmp, queue_file)


def _append_queue(queue_file: Path, event: dict) -> None:
    with queue_file.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _post_events(events: list[dict], token: str) -> tuple[str, int | None]:
    """POST a batch to /log. Returns (status, item): status is "ok",
    "transient", "too_large" (413), or "rejected" (permanent — 400/422 or an
    unserializable payload); item is the batch index a rejection names."""
    try:
        data = json.dumps(events).encode()
    except (TypeError, ValueError):
        return "rejected", None
    request = urllib.request.Request(
        _url() + "/log",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return ("ok" if response.status == 200 else "transient"), None
    except urllib.error.HTTPError as error:
        # Validation-class codes mean the PAYLOAD can never deliver. Auth
        # (401/403) and everything else may heal (token rotation, server
        # hiccup) — those must keep the events queued, not quarantine them.
        if error.code in (400, 422):
            return "rejected", _rejected_item(error)
        if error.code == 413:
            return "too_large", None
        return "transient", None
    except (urllib.error.URLError, OSError, ValueError):
        return "transient", None


def _rejected_item(error: urllib.error.HTTPError) -> int | None:
    """The failing index from a batch error body ({"error": "item N: ..."})."""
    try:
        match = re.match(r"item (\d+): ", str(json.loads(error.read())["error"]))
    except Exception:
        return None
    return int(match.group(1)) if match else None


def _build_event(
    app: str,
    level: str,
    message: str,
    json_extra: dict | None = None,
    source: str | None = None,
) -> dict:
    text = _clip(str(message), MAX_MESSAGE_LEN)
    event = {
        "app": app,
        "level": level,
        "message": text or EMPTY_MESSAGE,
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": _clip(str(source or socket.gethostname()), MAX_SOURCE_LEN),
    }
    if json_extra is not None:
        event["json_extra"] = _repair_json_extra(json_extra)
    return event


def _repair_json_extra(json_extra):
    """Repair a json_extra the server would reject (it must be an object/array
    of at most MAX_JSON_EXTRA_LEN serialized chars) instead of letting it
    quarantine the whole event. Returns the parsed snapshot, so what's
    validated here is exactly what's sent."""
    try:
        serialized = _compact_json(json_extra)
    except (TypeError, ValueError):
        # A caller's unserializable extra (or NaN, which would make the whole
        # batch invalid JSON) must not lose the event.
        return {"unserializable": str(json_extra)[:1000]}
    if not serialized.startswith(("{", "[")):
        serialized = _compact_json({"value": json_extra})
    if _utf16_len(serialized) > MAX_JSON_EXTRA_LEN:
        return {
            "truncated": True,
            "original_length": _utf16_len(serialized),
            "preview": _clip(serialized, JSON_EXTRA_PREVIEW_LEN),
        }
    return json.loads(serialized)


def log(
    app: str,
    level: str,
    message: str,
    json_extra: dict | None = None,
    source: str | None = None,
) -> bool:
    """Best-effort push to the unified logger. True=delivered, False=queued.

    Never raises: any failure (missing token, network down, server error)
    leaves the event in the local fallback queue and returns False. On every
    call, previously queued events replay FIRST (oldest first, original ts
    preserved) so an outage's events still reach D1 — and the sentinel —
    after recovery.
    """
    try:
        event = _build_event(app, level, message, json_extra, source)
        queue_file = _queue_file()
        queue_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = queue_file.with_name(queue_file.name + ".lock")
        with open(lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _deliver_or_queue(queue_file, event)
    except Exception:
        return False


def _reject_reason(status: str) -> str:
    if status == "too_large":
        return "server rejected event as too large"
    return "server rejected event as malformed"


def _deliver_alone(queue_file: Path, event: dict) -> bool:
    """Cooldown path: a recent transient failure is likely still in force, so
    skip the backlog read and replay (the per-call cost a persistent failure
    would otherwise grow without bound) but still try this call's event."""
    global _last_transient_at, _cooldown_appends
    try:
        status, _ = _post_events([event], _read_token(_write_token_file()))
    except OSError:
        status = "transient"
    if status == "transient":
        _last_transient_at = time.monotonic()
        _cooldown_appends += 1
        _append_queue(queue_file, event)
        return False
    # Any non-transient answer proves the server is reachable: the next call
    # replays the backlog.
    _last_transient_at = float("-inf")
    if status == "ok":
        return True
    _quarantine(queue_file, [json.dumps(event)], _reject_reason(status))
    return False


def _deliver_or_queue(queue_file: Path, event: dict) -> bool:
    """Called under the queue lock: replay queued events + this one, chunked.

    Returns True iff the server accepted the new event. Progress persists
    after EVERY delivered chunk (a crash can't re-send accepted events), the
    no-progress failure path is an O(1) append (not a full rewrite), and a
    permanently-rejected event is quarantined — alone, not with its chunk —
    so it can't wedge the queue head. Work per call is capped at
    MAX_CHUNKS_PER_CALL (+ MAX_SPLITS_PER_CALL halvings).
    """
    global _last_transient_at, _cooldown_appends
    if _in_cooldown() and _cooldown_appends < MAX_QUEUE_EVENTS:
        return _deliver_alone(queue_file, event)
    _cooldown_appends = 0
    queued = _read_queue(queue_file)
    pending = queued + [event]
    # Until the first rewrite the on-disk queue holds exactly `queued`, so a
    # failure only needs to append the new event.
    persisted = False

    def fail_transient(rest: list[dict]) -> bool:
        """Persist `rest` (the unsent tail) after a transient failure. The
        queue cap applies only here: overflow a healthy server could still
        take is never quarantined ahead of a delivery attempt."""
        global _last_transient_at
        _last_transient_at = time.monotonic()
        if len(rest) > MAX_QUEUE_EVENTS:
            overflow = len(rest) - MAX_QUEUE_EVENTS
            _quarantine(
                queue_file,
                [json.dumps(e) for e in rest[:overflow]],
                f"queue cap of {MAX_QUEUE_EVENTS} events exceeded",
            )
            _write_queue(queue_file, rest[overflow:])
        elif not persisted:
            _append_queue(queue_file, event)
        # else: the last rewrite already persisted `rest`
        return False

    try:
        token = _read_token(_write_token_file())
    except OSError:
        return fail_transient(pending)

    event_accepted = False
    index = 0
    chunks_sent = 0
    # A 413 or an unattributed reject halves the chunk; isolating an offender
    # resets it to full size. Halvings have their own per-call budget instead
    # of counting against MAX_CHUNKS_PER_CALL, so every call makes progress
    # (delivers a sub-chunk or isolates an offender) — even a
    # one-call-per-process client never wedges behind an offender.
    size = BATCH_MAX
    splits = 0
    while index < len(pending):
        if chunks_sent >= MAX_CHUNKS_PER_CALL:
            _write_queue(queue_file, pending[index:])
            return event_accepted
        chunk = pending[index : index + size]
        status, item = _post_events(chunk, token)
        if status == "transient":
            return fail_transient(pending[index:])
        if status == "ok":
            if any(e is event for e in chunk):
                event_accepted = True
            index += len(chunk)
        else:
            if len(chunk) == 1:
                offender = 0
            elif status == "rejected" and item is not None and item < len(chunk):
                offender = item
            else:
                if splits >= MAX_SPLITS_PER_CALL:
                    # Budget spent: the next call isolates the next offender.
                    _write_queue(queue_file, pending[index:])
                    return event_accepted
                splits += 1
                size = (len(chunk) + 1) // 2
                continue
            # Only the offender leaves the queue: the server writes nothing
            # from a rejected batch, so the rest of the chunk is retried.
            _quarantine(
                queue_file, [json.dumps(chunk[offender])], _reject_reason(status)
            )
            pending.pop(index + offender)
            size = BATCH_MAX
        chunks_sent += 1
        _write_queue(queue_file, pending[index:])
        persisted = True
    return event_accepted


def view(
    app: str | None = None,
    level: str | None = None,
    since: str | None = None,
    since_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """Query GET /logs with the admin token. Raises on failure (the viewer
    may fail loudly — unlike log(), nothing depends on it staying silent)."""
    import urllib.parse

    params = {"limit": str(limit)}
    if app is not None:
        params["app"] = app
    if level is not None:
        params["level"] = level
    if since is not None:
        params["since"] = since
    if since_id is not None:
        params["since_id"] = str(since_id)
    token = _read_token(_admin_token_file())
    request = urllib.request.Request(
        _url() + "/logs?" + urllib.parse.urlencode(params),
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
    )
    payload = _open_with_retry(request, timeout=10)
    return payload["logs"]


def _admin_request(path: str, method: str = "GET", body: dict | None = None) -> dict:
    """Authenticated admin-API call (sentinel side, #201). Raises on failure —
    the sentinel decides severity (WARN vs FAILED), not this client."""
    token = _read_token(_admin_token_file())
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        _url() + path, data=data, headers=headers, method=method
    )
    return _open_with_retry(request, timeout=10)


def get_watermark() -> int | None:
    """Sentinel sweep watermark (max log row id seen). None before first set."""
    return _admin_request("/watermark")["value"]


def put_watermark(value: int) -> int:
    return _admin_request("/watermark", method="PUT", body={"value": value})["value"]


def upsert_observation(fingerprint: str, app: str, summary: str | None = None) -> dict:
    """Upsert by fingerprint. Returns {"new", "times_seen", "issue_number"}.

    The Worker's ON CONFLICT never updates `summary`/`app` on re-observation —
    first write wins; only times_seen/last_seen advance.
    """
    body = {"fingerprint": fingerprint, "app": app}
    if summary is not None:
        body["summary"] = summary
    return _admin_request("/observations", method="POST", body=body)


def mark_filed(fingerprint: str, issue_number: int) -> dict:
    return _admin_request(
        "/observations/filed",
        method="POST",
        body={"fingerprint": fingerprint, "issue_number": issue_number},
    )


def list_observations(app: str | None = None) -> list[dict]:
    import urllib.parse

    path = "/observations"
    if app is not None:
        path += "?" + urllib.parse.urlencode({"app": app})
    return _admin_request(path)["observations"]


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Unified-logger client (#51)")
    sub = parser.add_subparsers(dest="command", required=True)

    log_p = sub.add_parser("log", help="push one event (best-effort; always exits 0)")
    log_p.add_argument("app")
    log_p.add_argument("level")
    log_p.add_argument("message")
    log_p.add_argument("--json-extra", help="JSON object with extra fields")
    log_p.add_argument("--source", help="client identifier (default: hostname)")

    view_p = sub.add_parser("view", help="query recent logs (admin token)")
    view_p.add_argument("--app")
    view_p.add_argument("--level", help="comma-separated, e.g. alert,error")
    view_p.add_argument("--since", help="ISO-8601 or relative like 1h / 24h / 7d")
    view_p.add_argument("--since-id", type=int)
    view_p.add_argument("--limit", type=int, default=100)
    view_p.add_argument("--json", action="store_true", help="raw JSON output")

    args = parser.parse_args()

    if args.command == "log":
        json_extra = None
        if args.json_extra:
            try:
                json_extra = json.loads(args.json_extra)
            except json.JSONDecodeError:
                # Malformed extra must not break the caller: log without it.
                print("logger: ignoring malformed --json-extra", file=sys.stderr)
            if json_extra is not None and not isinstance(json_extra, dict):
                # The API contract types json_extra as an object; anything
                # else would be server-rejected and quarantine the batch.
                print(
                    "logger: ignoring --json-extra (must be a JSON object)",
                    file=sys.stderr,
                )
                json_extra = None
        delivered = log(
            args.app,
            args.level,
            args.message,
            json_extra=json_extra,
            source=args.source,
        )
        if not delivered:
            print("logger: delivery failed, event queued", file=sys.stderr)
        return 0

    # view
    try:
        rows = view(
            app=args.app,
            level=args.level,
            since=args.since,
            since_id=args.since_id,
            limit=args.limit,
        )
    except Exception as error:
        print(f"logger: view failed: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            print(
                f"{row.get('ts', '?')}  {row.get('level', '?'):<5}  "
                f"{row.get('app', '?')}  {row.get('message', '')}"
            )
    return 0


def _main() -> int:
    # The `log` subcommand must ALWAYS exit 0 — even on argparse errors
    # (SystemExit 2) — so shell callers piping status text through log.sh
    # can never be broken by the logging path. `view` may fail loudly.
    is_log = len(sys.argv) > 1 and sys.argv[1] == "log"
    try:
        return _cli()
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 1
        return 0 if is_log else code
    except Exception as error:
        print(f"logger: {error}", file=sys.stderr)
        return 0 if is_log else 1


if __name__ == "__main__":
    sys.exit(_main())
