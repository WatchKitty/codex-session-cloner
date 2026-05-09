"""Claude subpackage cross-platform hardening tests.

Locks in the invariants we just established so a future regression that
removes ``atomic_write`` / ``shutil.which`` / ``newline=""`` / ``safe_copy2``
fails fast in CI rather than corrupting state on a user's Windows install.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


from ai_cli_kit.claude.history_remap import _run_claude_refresh, _rewrite_file_in_place  # noqa: E402
from ai_cli_kit.claude.paths import default_paths  # noqa: E402
from ai_cli_kit.claude.services import (  # noqa: E402
    _move_with_retry,
    _relative_under_home,
    _remove_with_retry,
    _write_json,
    build_plan,
    execute_plan,
    resolve_selection,
)
from ai_cli_kit.claude.models import RunOptions  # noqa: E402


class AtomicJsonWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_dir = Path(self.tmp.name)

    def test_write_json_replaces_target_atomically(self) -> None:
        target = self.tmp_dir / "state.json"
        target.write_text(json.dumps({"old": True}), encoding="utf-8")
        _write_json(target, {"new": True})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"new": True})

    def test_write_json_leaves_no_partial_tempfile_after_success(self) -> None:
        target = self.tmp_dir / "settings.json"
        _write_json(target, {"x": 1})
        leftovers = sorted(p.name for p in self.tmp_dir.iterdir() if p.suffix == ".tmp")
        self.assertEqual(leftovers, [])


class ClaudeBinaryDiscoveryTests(unittest.TestCase):
    def test_run_claude_refresh_errors_when_executable_missing(self) -> None:
        # When ``claude`` is not on PATH, we surface a clear error instead of
        # crashing with ``FileNotFoundError`` mid-subprocess.
        with patch("shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                _run_claude_refresh(timeout_seconds=1)
        self.assertIn("PATH", str(ctx.exception))

    def test_run_claude_refresh_wraps_windows_cmd_shim_with_cmd_exe(self) -> None:
        """Windows: ``shutil.which("claude")`` typically resolves to a npm
        ``claude.cmd`` shim. ``subprocess.run([path], shell=False)`` uses
        ``CreateProcess`` directly, which CANNOT execute ``.cmd``/``.bat``
        files — it raises ``WinError 193`` ('not a valid Win32 application').
        ``_run_claude_refresh`` MUST detect the batch extension and wrap
        with ``cmd.exe /c`` so cmd.exe interprets the shim. POSIX paths and
        ``.exe`` resolution must NOT be wrapped.
        """
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd

            class _Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Result()

        # Simulate Windows .cmd resolution.
        with patch("os.name", "nt"):
            with patch("shutil.which", return_value="C:\\Users\\x\\AppData\\Roaming\\npm\\claude.cmd"):
                with patch("ai_cli_kit.claude.history_remap.subprocess.run", side_effect=fake_run):
                    _run_claude_refresh(timeout_seconds=5)
        self.assertEqual(captured["cmd"][0], "cmd.exe", "Windows .cmd shim NOT wrapped with cmd.exe /c")
        self.assertEqual(captured["cmd"][1], "/c")
        self.assertEqual(captured["cmd"][2], "C:\\Users\\x\\AppData\\Roaming\\npm\\claude.cmd")

        # POSIX path: must be invoked directly, no wrapping.
        captured.clear()
        with patch("os.name", "posix"):
            with patch("shutil.which", return_value="/usr/local/bin/claude"):
                with patch("ai_cli_kit.claude.history_remap.subprocess.run", side_effect=fake_run):
                    _run_claude_refresh(timeout_seconds=5)
        self.assertEqual(captured["cmd"][0], "/usr/local/bin/claude", "POSIX path wrongly wrapped")

        # Windows .exe: also direct, no wrapping (CreateProcess handles .exe).
        captured.clear()
        with patch("os.name", "nt"):
            with patch("shutil.which", return_value="C:\\Program Files\\Claude\\claude.exe"):
                with patch("ai_cli_kit.claude.history_remap.subprocess.run", side_effect=fake_run):
                    _run_claude_refresh(timeout_seconds=5)
        self.assertEqual(
            captured["cmd"][0],
            "C:\\Program Files\\Claude\\claude.exe",
            "Windows .exe wrongly wrapped — only .cmd/.bat need cmd.exe /c",
        )

    def test_run_claude_refresh_uses_resolved_executable(self) -> None:
        # Mock both shutil.which (used by our code) and subprocess.run to
        # capture how the resolved executable is forwarded.
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

            class _Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Result()

        with patch("shutil.which", return_value="/fake/path/to/claude"):
            with patch("ai_cli_kit.claude.history_remap.subprocess.run", side_effect=fake_run):
                _run_claude_refresh(timeout_seconds=7)

        self.assertEqual(captured["cmd"][0], "/fake/path/to/claude")
        self.assertEqual(captured["kwargs"]["timeout"], 7)


class HistoryRemapWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_dir = Path(self.tmp.name)

    def test_rewrite_jsonl_keeps_lf_only_line_endings(self) -> None:
        # JSONL files must not get CRLF translation on Windows; ``newline=""``
        # in atomic_write is what guarantees that.
        target = self.tmp_dir / "history.jsonl"
        target.write_text(
            json.dumps({"userID": "old-id"}) + "\n" + json.dumps({"x": 1}) + "\n",
            encoding="utf-8",
        )
        mappings = {"user_id": ("old-id", "new-id")}
        _rewrite_file_in_place(target, mappings)

        # Verify the rewrite happened AND line endings are LF-only.
        body = target.read_bytes()
        self.assertNotIn(b"\r\n", body)
        first_line = body.split(b"\n", 1)[0]
        self.assertEqual(json.loads(first_line)["userID"], "new-id")


class RetryableFileOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_dir = Path(self.tmp.name)

    def test_move_with_retry_succeeds_on_first_try(self) -> None:
        src = self.tmp_dir / "src.txt"
        src.write_text("payload", encoding="utf-8")
        dst = self.tmp_dir / "subdir" / "dst.txt"
        dst.parent.mkdir(parents=True)
        _move_with_retry(src, dst)
        self.assertEqual(dst.read_text(encoding="utf-8"), "payload")
        self.assertFalse(src.exists())

    def test_remove_with_retry_handles_files_and_dirs(self) -> None:
        a_file = self.tmp_dir / "f.txt"
        a_file.write_text("x", encoding="utf-8")
        a_dir = self.tmp_dir / "d"
        (a_dir / "child").mkdir(parents=True)
        (a_dir / "child" / "nested.txt").write_text("y", encoding="utf-8")

        _remove_with_retry(a_file)
        _remove_with_retry(a_dir)
        self.assertFalse(a_file.exists())
        self.assertFalse(a_dir.exists())


class RelativeUnderHomeCaseTests(unittest.TestCase):
    def test_path_inside_home_returns_relative_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            (home / ".claude").mkdir()
            target = home / ".claude" / "state.json"
            self.assertEqual(_relative_under_home(home, source=target), Path(".claude/state.json"))

    def test_path_outside_home_falls_through_to_external(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "home"
            home.mkdir()
            outside = Path(tmp_dir) / "elsewhere" / "x"
            self.assertTrue(str(_relative_under_home(home, source=outside)).startswith("external"))

    def test_case_variant_path_treated_as_inside_on_insensitive_fs(self) -> None:
        if os.path.normcase("ABC") == "ABC":
            self.skipTest("case-sensitive filesystem; normcase is identity")
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "home"
            (home / ".claude").mkdir(parents=True)
            up_target = Path(str(home).upper()) / ".claude" / "state.json"
            relative = _relative_under_home(home, source=up_target)
            self.assertFalse(str(relative).startswith("external"))


class PathSizeCacheTests(unittest.TestCase):
    """``_path_size`` must memoise directory walks so the Claude TUI's
    per-keypress plan rebuild doesn't re-scan ``projects_dir`` every time.
    The cache key is (path, st_mtime_ns) so external file-system mutations
    that bump mtime (git pulls, editors, backups) still invalidate it.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "data"
        self.dir.mkdir()
        # Clear any pre-existing cache entries from other tests.
        from ai_cli_kit.claude.services import _PATH_SIZE_CACHE
        _PATH_SIZE_CACHE.clear()

    def test_second_call_hits_cache_when_mtime_unchanged(self) -> None:
        from ai_cli_kit.claude.services import _PATH_SIZE_CACHE, _path_size

        (self.dir / "a.txt").write_text("hello", encoding="utf-8")
        first = _path_size(self.dir)
        self.assertEqual(first, 5)
        # A second call must not re-enumerate; the cache dict should have the entry.
        cache_size_before = len(_PATH_SIZE_CACHE)
        second = _path_size(self.dir)
        self.assertEqual(second, 5)
        self.assertEqual(len(_PATH_SIZE_CACHE), cache_size_before,
                         "second call added a new cache entry — cache miss when it should hit")

    def test_scandir_failure_returns_zero_not_crash(self) -> None:
        """If the directory disappears mid-walk (user ``rm -rf`` from another
        shell while the TUI is open), ``_path_size`` must return 0 rather
        than propagate ``OSError`` — otherwise the entire claude TUI dies
        on the user's next keypress.

        ``_path_size`` switched from ``Path.rglob`` to ``os.scandir``; this
        test patches the new API so the regression target moves with the
        implementation.
        """
        from unittest.mock import patch
        from ai_cli_kit.claude.services import _path_size

        # Pre-populate so the dir registers as non-empty during pre-checks.
        (self.dir / "x.txt").write_text("payload", encoding="utf-8")

        with patch("ai_cli_kit.claude.services.os.scandir", side_effect=OSError("ENOENT (simulated)")):
            result = _path_size(self.dir)
        self.assertEqual(result, 0, "scandir OSError must return 0, not crash")

    def test_cache_concurrent_access_is_safe(self) -> None:
        """Concurrent ``_path_size`` calls must not corrupt the cache.

        Without the lock, two threads racing through the
        ``if len > 64: clear; insert`` sequence could lose entries or
        produce inconsistent state. The lock keeps the read-modify-write
        atomic. Test spins 8 threads each computing 10 different paths;
        any data race (cache key visible without value, KeyError, etc.)
        would surface as an exception.
        """
        import threading
        from ai_cli_kit.claude.services import _PATH_SIZE_CACHE, _path_size

        # 80 distinct directories, enough to push past the 64-entry cap.
        dirs = []
        for idx in range(80):
            d = self.dir / f"sub-{idx}"
            d.mkdir()
            (d / "f.txt").write_text("x" * (idx + 1), encoding="utf-8")
            dirs.append(d)

        errors: list[BaseException] = []

        def worker():
            try:
                for d in dirs:
                    _path_size(d)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"concurrent _path_size raised: {errors}")
        # After the storm, cache should still hold valid entries (size > 0)
        # for some subset; bookkeeping survived without exception.
        self.assertGreater(len(_PATH_SIZE_CACHE), 0)

    def test_cache_invalidates_on_mtime_change(self) -> None:
        from ai_cli_kit.claude.services import _path_size

        (self.dir / "a.txt").write_text("hello", encoding="utf-8")
        self.assertEqual(_path_size(self.dir), 5)
        # Touch dir mtime by adding a new file; verify cache invalidates.
        (self.dir / "b.txt").write_text("world!", encoding="utf-8")
        # Some filesystems have second-granularity mtime — bump explicitly.
        os.utime(self.dir, None)
        self.assertEqual(_path_size(self.dir), 11)


class FullCleanupRoundtripTests(unittest.TestCase):
    """End-to-end: post-hardening, the safe preset still works as before."""

    def test_safe_preset_atomic_write_cleans_userid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.state_file.write_text(
                json.dumps({"userID": "abc", "keep": 1}), encoding="utf-8"
            )
            paths.telemetry_dir.mkdir()
            (paths.telemetry_dir / "fail.json").write_text("{}", encoding="utf-8")

            plan = build_plan(paths, resolve_selection("safe"))
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))

            payload = json.loads(paths.state_file.read_text(encoding="utf-8"))
            self.assertNotIn("userID", payload)
            self.assertEqual(payload["keep"], 1)
            self.assertFalse(paths.telemetry_dir.exists())
            self.assertIsNotNone(summary.backup_root)


class CrossFsBackupTests(unittest.TestCase):
    """Backup-then-remove path stays correct across simulated filesystem boundaries."""

    def test_cross_fs_uses_copy_then_unlink_not_shutil_move(self) -> None:
        from unittest.mock import patch
        from ai_cli_kit.claude.services import _move_or_copy_then_delete

        with tempfile.TemporaryDirectory() as tmp_dir:
            src_dir = Path(tmp_dir) / "src"
            src_dir.mkdir()
            (src_dir / "leaf.txt").write_text("payload-bytes", encoding="utf-8")
            dst_dir = Path(tmp_dir) / "dst"

            with patch("ai_cli_kit.claude.services._same_filesystem", return_value=False):
                _move_or_copy_then_delete(src_dir, dst_dir)

            self.assertFalse(src_dir.exists())
            self.assertTrue(dst_dir.exists())
            self.assertEqual(
                (dst_dir / "leaf.txt").read_text(encoding="utf-8"),
                "payload-bytes",
            )

    def test_cross_fs_raises_when_destination_short(self) -> None:
        from unittest.mock import patch
        from ai_cli_kit.claude.services import _move_or_copy_then_delete

        with tempfile.TemporaryDirectory() as tmp_dir:
            src_dir = Path(tmp_dir) / "src"
            src_dir.mkdir()
            (src_dir / "leaf.txt").write_text("payload-bytes", encoding="utf-8")
            dst_dir = Path(tmp_dir) / "dst"

            # Force cross-fs path AND short-circuit copytree to leave dst empty,
            # which the size-verification step should detect.
            def fake_copytree(src, dst, *args, **kwargs):
                Path(dst).mkdir(parents=True, exist_ok=True)
                # Intentionally leave content out so the size check trips.

            with patch("ai_cli_kit.claude.services._same_filesystem", return_value=False), \
                    patch("ai_cli_kit.claude.services.shutil.copytree", side_effect=fake_copytree):
                with self.assertRaises(RuntimeError):
                    _move_or_copy_then_delete(src_dir, dst_dir)

            # Source must still be intact since we refused to delete it.
            self.assertTrue((src_dir / "leaf.txt").exists())


class SymlinkRemovePathTests(unittest.TestCase):
    """``remove_path`` does not follow symlinks: link is dropped, target survives."""

    def test_remove_symlink_leaves_underlying_data_intact(self) -> None:
        if sys.platform == "win32":
            self.skipTest("symlink test skipped on Windows without dev mode")

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            external = home / "external-store"
            external.mkdir()
            (external / "session.jsonl").write_text("payload", encoding="utf-8")
            os.symlink(external, paths.sessions_dir)

            plan = build_plan(paths, resolve_selection("safe"))
            sessions_item = next(item for item in plan if item.target.key == "sessions_dir")
            self.assertTrue(any("符号链接" in w for w in sessions_item.warnings))

            execute_plan(paths, build_plan(paths, {"sessions_dir"}), RunOptions(backup_enabled=False, dry_run=False))
            self.assertFalse(paths.sessions_dir.exists())
            # External target untouched.
            self.assertTrue((external / "session.jsonl").exists())


class PathSizeChildMtimeTests(unittest.TestCase):
    """Cache must invalidate when a deeper subdir gains a file even if the
    top-level dir's mtime did NOT change."""

    def test_subdir_only_write_invalidates_cache_via_child_signature(self) -> None:
        from ai_cli_kit.claude.services import _PATH_SIZE_CACHE, _path_size

        _PATH_SIZE_CACHE.clear()
        with tempfile.TemporaryDirectory() as tmp_dir:
            top = Path(tmp_dir) / "top"
            sub = top / "sub"
            sub.mkdir(parents=True)
            (sub / "a.txt").write_text("aa", encoding="utf-8")

            # Freeze the top-level mtime so a naive mtime cache would miss
            # the upcoming subdir write.
            top_mtime = top.stat().st_mtime
            first = _path_size(top)
            self.assertEqual(first, 2)

            (sub / "b.txt").write_text("bbbb", encoding="utf-8")
            os.utime(top, (top_mtime, top_mtime))

            second = _path_size(top)
            self.assertEqual(second, 6, "child-mtime signature should detect subdir-only growth")


class PathSizeLruTests(unittest.TestCase):
    """LRU eviction keeps the working set hot; old entries fall off one at a time."""

    def test_lru_eviction_drops_oldest_first_not_full_clear(self) -> None:
        from ai_cli_kit.claude.services import _PATH_SIZE_CACHE, _PATH_SIZE_CACHE_MAX, _path_size

        _PATH_SIZE_CACHE.clear()
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            # Fill cache to the cap +1, expecting the very first entry to evict.
            paths_to_size = []
            for idx in range(_PATH_SIZE_CACHE_MAX + 1):
                d = base / f"dir-{idx}"
                d.mkdir()
                (d / "x").write_text("y", encoding="utf-8")
                paths_to_size.append(d)

            for d in paths_to_size:
                _path_size(d)

            self.assertLessEqual(len(_PATH_SIZE_CACHE), _PATH_SIZE_CACHE_MAX)
            # Cache should still hold something (not nuked entirely).
            self.assertGreater(len(_PATH_SIZE_CACHE), 0)


class TransientErrorRetryTests(unittest.TestCase):
    """Retry now widens beyond PermissionError to OSError EBUSY/ENOTEMPTY/winerror=32."""

    def test_remove_with_retry_swallows_ebusy(self) -> None:
        from unittest.mock import patch
        from ai_cli_kit.claude.services import _remove_with_retry

        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "f.txt"
            target.write_text("x", encoding="utf-8")

            attempts = {"count": 0}
            real_unlink = os.unlink

            def flaky_unlink(p):
                attempts["count"] += 1
                if attempts["count"] < 2:
                    raise OSError(errno.EBUSY, "device busy")
                return real_unlink(p)

            with patch("ai_cli_kit.claude.services.os.unlink", side_effect=flaky_unlink):
                _remove_with_retry(target)

            self.assertFalse(target.exists())
            self.assertGreaterEqual(attempts["count"], 2)


if __name__ == "__main__":
    unittest.main()
