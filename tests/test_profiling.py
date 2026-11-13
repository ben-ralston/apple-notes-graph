"""Tests for the context-scoped timing profiler (issue #98)."""

import re
import threading

import pytest

from apple_notes import profiling
from apple_notes.profiling import Profile, profile, profiled, record, span


class TestProfileObject:
    def test_record_accumulates_seconds_as_ms(self):
        prof = Profile("act", "detail")
        prof.record("x", 0.25)
        prof.record("x", 0.25)
        assert prof.timing_ms == {"x": 500}

    def test_finalize_is_idempotent(self):
        prof = Profile("act")
        prof.finalize()
        first = prof.total_secs
        prof.finalize()
        assert prof.total_secs == first

    def test_span_records_into_manually_activated_profile(self):
        prof = Profile("act")
        token = profiling._active_profile.set(prof)
        try:
            with span("p"):
                pass
        finally:
            profiling._active_profile.reset(token)
        assert "p" in prof.timing_ms


class TestNoOpWhenInactive:
    def test_span_is_noop_without_active_profile(self):
        # No active profile → no error, nothing recorded.
        with span("phase"):
            pass

    def test_record_is_noop_without_active_profile(self):
        record("lock_wait", 1.0)  # must not raise


class TestSpanAccumulation:
    def test_span_records_elapsed_into_active_profile(self):
        with profile("act") as prof:
            with span("phase"):
                pass
        assert "phase" in prof.timing_ms
        assert prof.timing_ms["phase"] >= 0

    def test_repeated_span_names_accumulate(self):
        with profile("act") as prof:
            with span("phase"):
                pass
            with span("phase"):
                pass
        # Two recordings under the same name sum into one entry.
        assert list(prof.timing_ms).count("phase") == 1

    def test_record_adds_named_duration(self):
        with profile("act") as prof:
            record("lock_wait", 0.5)
        assert prof.timing_ms["lock_wait"] == 500


class TestContextvarIsolation:
    def test_profiles_do_not_leak_across_threads(self):
        results = {}

        def worker(name):
            with profile(name) as prof:
                record(name, 0.1)
                results[name] = prof.timing_ms

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Each thread's profile saw only its own recording.
        assert results["a"] == {"a": 100}
        assert results["b"] == {"b": 100}

    def test_active_profile_reset_after_context(self):
        with profile("act"):
            assert profiling._active_profile.get() is not None
        assert profiling._active_profile.get() is None


class TestProfileLogging:
    def test_start_and_end_lines_written(self, tmp_path, monkeypatch):
        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)
        with profile("add_note", 'name="X"'):
            pass
        lines = log.read_text().splitlines()
        assert len(lines) == 2
        assert lines[0].endswith("START")
        assert "add_note" in lines[0] and 'name="X"' in lines[0]

    def test_end_line_format_with_spans(self, tmp_path, monkeypatch):
        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)
        with profile("add_note", 'name="X"') as prof:
            record("backup", 0.12)
            record("execute", 0.973)
        end = log.read_text().splitlines()[1]
        # e.g. 2026-... add_note name="X" total=NNms [backup=120 execute=973] ok
        assert re.search(r"total=\d+ms", end)
        assert "[backup=120 execute=973]" in end
        assert end.endswith("ok")

    def test_end_line_omits_bracket_when_no_spans(self, tmp_path, monkeypatch):
        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)
        with profile("read_note_content", 'name="X"'):
            pass
        end = log.read_text().splitlines()[1]
        assert "[" not in end
        assert end.endswith("ok")

    def test_end_line_records_error_outcome_on_exception(self, tmp_path, monkeypatch):
        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)
        with pytest.raises(ValueError):
            with profile("add_note"):
                raise ValueError("boom")
        end = log.read_text().splitlines()[1]
        assert end.endswith("error:ValueError")

    def test_outcome_override_wins(self, tmp_path, monkeypatch):
        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)
        with profile("create_note") as prof:
            prof.outcome = "error:SessionLockedError"
        end = log.read_text().splitlines()[1]
        assert end.endswith("error:SessionLockedError")

    def test_logging_failure_never_propagates(self, tmp_path, monkeypatch):
        # Point at a path whose parent cannot be created (a file, not a dir).
        bad_parent = tmp_path / "afile"
        bad_parent.write_text("x")
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", bad_parent / "timing.log")
        # Must not raise even though the write fails.
        with profile("add_note"):
            pass


class TestOutcomeMapping:
    def test_timeout_maps_to_timeout(self):
        assert profiling.outcome_for_error_type("EditLockTimeoutError") == "timeout"

    def test_other_errors_prefixed(self):
        assert profiling.outcome_for_error_type("ToolError") == "error:ToolError"


class TestProfiledDecorator:
    def test_attaches_elapsed_and_timing_for_write_dict(self, tmp_path, monkeypatch):
        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)

        @profiled("add_note", detail_arg="name", attach_response=True)
        def add_note(name, body):
            with span("backup"):
                pass
            return {"success": True, "note_name": name}

        result = add_note(name="X", body="b")
        assert result["success"] is True
        assert result["note_name"] == "X"
        assert isinstance(result["elapsed_ms"], int)
        assert "backup" in result["timing"]
        # And a START + END pair landed in the log.
        assert len(log.read_text().splitlines()) == 2

    def test_elapsed_ms_matches_logged_total(self, tmp_path, monkeypatch):
        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)

        @profiled("add_note", detail_arg="name", attach_response=True)
        def add_note(name):
            return {"ok": True}

        result = add_note(name="X")
        end = log.read_text().splitlines()[1]
        assert f"total={result['elapsed_ms']}ms" in end

    def test_read_return_unchanged(self, tmp_path, monkeypatch):
        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)

        @profiled("read_note_content", detail_arg="name")
        def read(name):
            return {"content": "hi"}

        result = read(name="X")
        assert result == {"content": "hi"}  # no elapsed_ms / timing

    def test_list_return_unchanged(self, tmp_path, monkeypatch):
        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)

        @profiled("list_notes")
        def list_notes():
            return [1, 2, 3]

        assert list_notes() == [1, 2, 3]

    def test_detail_rendered_from_named_arg(self, tmp_path, monkeypatch):
        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)

        @profiled("read_note_content", detail_arg="name")
        def read(name):
            return None

        read(name="My Note")
        assert 'name="My Note"' in log.read_text().splitlines()[0]

    def test_signature_preserved_for_introspection(self):
        import inspect

        @profiled("x", detail_arg="name")
        def f(name: str, count: int = 3) -> dict:
            return {}

        sig = inspect.signature(f)
        assert list(sig.parameters) == ["name", "count"]

    def test_exception_propagates_and_logs_error(self, tmp_path, monkeypatch):
        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)

        @profiled("add_note", attach_response=True)
        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError):
            boom()
        assert log.read_text().splitlines()[1].endswith("error:ValueError")
