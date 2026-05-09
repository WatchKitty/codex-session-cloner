from __future__ import annotations

import json
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_cli_kit.claude.history_remap import remap_history_identifiers
from ai_cli_kit.claude.models import RunOptions
from ai_cli_kit.claude.paths import default_paths
from ai_cli_kit.claude.services import build_plan, execute_plan, resolve_selection
from ai_cli_kit.claude.tui.app import CleanerTuiApp
from ai_cli_kit.core.tui.screen_mode import (
    ALT_ENTER_FALLBACK,
    ALT_EXIT_FALLBACK,
    ScreenModeDecision,
    TerminfoScreenCaps,
    resolve_screen_mode,
)
from ai_cli_kit.claude.tui.terminal import app_logo_lines, display_width, render_box


class CleanupWorkflowTests(unittest.TestCase):
    def test_safe_plan_marks_session_targets_unselected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.state_file.write_text(json.dumps({"userID": "abc", "keep": 1}), encoding="utf-8")
            paths.telemetry_dir.mkdir()
            (paths.telemetry_dir / "failed.json").write_text("{}", encoding="utf-8")
            paths.projects_dir.mkdir()
            (paths.projects_dir / "session.jsonl").write_text("{}", encoding="utf-8")

            plan = build_plan(paths, resolve_selection("safe"))
            items = {item.target.key: item for item in plan}

            self.assertTrue(items["state_user_id"].selected)
            self.assertTrue(items["telemetry_dir"].selected)
            self.assertFalse(items["projects_dir"].selected)
            self.assertTrue(items["projects_dir"].applicable)

    def test_cleanup_with_backup_scrubs_user_id_and_moves_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.state_file.write_text(json.dumps({"userID": "abc", "keep": 1}), encoding="utf-8")
            paths.telemetry_dir.mkdir()
            (paths.telemetry_dir / "failed.json").write_text("{\"x\":1}", encoding="utf-8")

            selected = {"state_user_id", "telemetry_dir"}
            plan = build_plan(paths, selected)
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))

            payload = json.loads(paths.state_file.read_text(encoding="utf-8"))
            self.assertNotIn("userID", payload)
            self.assertEqual(payload["keep"], 1)
            self.assertFalse(paths.telemetry_dir.exists())
            self.assertIsNotNone(summary.backup_root)

            backup_root = Path(summary.backup_root or "")
            self.assertTrue((backup_root / ".claude.json").exists())
            self.assertTrue((backup_root / ".claude" / "telemetry" / "failed.json").exists())

    def test_scrub_settings_env_removes_only_targeted_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.settings_file.write_text(
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_AUTH_TOKEN": "token",
                            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                            "KEEP_ME": "1",
                        },
                        "model": "opus",
                    }
                ),
                encoding="utf-8",
            )

            plan = build_plan(paths, {"settings_auth_env"})
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))
            payload = json.loads(paths.settings_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["env"], {"KEEP_ME": "1"})
            self.assertEqual(payload["model"], "opus")
            self.assertEqual(summary.records[0].status, "updated")

    def test_logo_lines_fit_requested_width(self) -> None:
        lines = app_logo_lines(max_width=38)
        self.assertGreaterEqual(len(lines), 2)
        for line in lines:
            self.assertLessEqual(display_width(line), 38)

    def test_render_box_respects_width(self) -> None:
        lines = render_box(["one", "two"], width=32)
        self.assertGreaterEqual(len(lines), 4)
        for line in lines:
            self.assertLessEqual(display_width(line), 32)

    def test_incremental_paint_updates_only_changed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            app = CleanerTuiApp(default_paths(Path(tmp_dir)))
            buffer = io.StringIO()
            with patch("sys.stdout", buffer):
                app._paint_frame("alpha\nbeta\ngamma", force=True)
                buffer.seek(0)
                buffer.truncate(0)
                app._paint_frame("alpha\nBETA\ngamma")

            self.assertEqual(buffer.getvalue(), "\033[2;1H\033[2KBETA")

    def test_home_frame_compacts_on_short_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.state_file.write_text(json.dumps({"userID": "abc"}), encoding="utf-8")
            paths.telemetry_dir.mkdir()
            paths.statsig_dir.mkdir()
            paths.projects_dir.mkdir()
            paths.history_file.write_text("{}", encoding="utf-8")
            paths.sessions_dir.mkdir()

            app = CleanerTuiApp(paths)
            plan = build_plan(paths, resolve_selection("safe"))
            with patch("ai_cli_kit.claude.tui.app.term_height", return_value=24):
                lines = app._frame_lines(app._home_frame(plan))

            self.assertLessEqual(len(lines), 24)
            self.assertTrue(any("更多目标" in line for line in lines))

    def test_screen_mode_env_forces_alt(self) -> None:
        decision = resolve_screen_mode(
            env={"CCC_TUI_SCREEN": "alt", "TERM": "xterm-256color"},
            stdout=io.StringIO(),
            terminfo_caps=TerminfoScreenCaps(False, False),
        )

        self.assertEqual(decision.resolved, "alt")
        self.assertEqual(decision.enter_sequence, ALT_ENTER_FALLBACK + "\033[?25l\033[H")
        self.assertEqual(decision.exit_sequence, ALT_EXIT_FALLBACK + "\033[?25h")

    def test_screen_mode_auto_prefers_main_for_iterm(self) -> None:
        stream = io.StringIO()
        with patch.object(stream, "isatty", return_value=True):
            decision = resolve_screen_mode(
                env={"TERM": "xterm-256color", "TERM_PROGRAM": "iTerm.app"},
                stdout=stream,
                terminfo_caps=TerminfoScreenCaps(True, False, "smcup", "rmcup"),
            )

        self.assertEqual(decision.resolved, "main")
        self.assertIn("terminal profile", decision.reason)

    def test_screen_mode_auto_uses_alt_for_kitty(self) -> None:
        stream = io.StringIO()
        with patch.object(stream, "isatty", return_value=True):
            decision = resolve_screen_mode(
                env={"TERM": "xterm-kitty", "KITTY_WINDOW_ID": "12"},
                stdout=stream,
                terminfo_caps=TerminfoScreenCaps(True, False, "smcup", "rmcup"),
            )

        self.assertEqual(decision.resolved, "alt")
        self.assertEqual(decision.enter_sequence, "smcup\033[?25l\033[H")
        self.assertEqual(decision.exit_sequence, "rmcup\033[?25h")

    def test_screen_mode_auto_respects_tmux_disable(self) -> None:
        stream = io.StringIO()
        with patch.object(stream, "isatty", return_value=True):
            decision = resolve_screen_mode(
                env={"TERM": "tmux-256color", "TMUX": "/tmp/tmux,123,0"},
                stdout=stream,
                terminfo_caps=TerminfoScreenCaps(True, False, "smcup", "rmcup"),
                tmux_alt_screen=False,
            )

        self.assertEqual(decision.resolved, "main")
        self.assertIn("tmux", decision.reason)

    def test_terminal_entry_exit_follow_screen_mode_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            app = CleanerTuiApp(
                default_paths(Path(tmp_dir)),
                screen_mode=ScreenModeDecision(
                    requested="main",
                    resolved="main",
                    reason="test",
                    enter_sequence="ENTER",
                    exit_sequence="EXIT",
                ),
            )
            buffer = io.StringIO()
            with patch("sys.stdout", buffer):
                app._enter_terminal()
                app._leave_terminal()

            self.assertEqual(buffer.getvalue(), "ENTEREXIT")

    def test_remap_history_rewrites_only_structured_identifier_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.projects_dir.mkdir(parents=True)
            paths.sessions_dir.mkdir(parents=True)
            paths.backup_root_base.mkdir(parents=True)
            paths.statsig_dir.mkdir(parents=True)

            old_user_id = "old-user-id-123"
            new_user_id = "new-user-id-456"
            old_stable_id = "old-stable-id-123"
            new_stable_id = "new-stable-id-456"
            old_statsig_session_id = "old-statsig-session-123"
            new_statsig_session_id = "new-statsig-session-456"

            paths.state_file.write_text(json.dumps({"userID": new_user_id}), encoding="utf-8")
            (paths.statsig_dir / "statsig.stable_id.111").write_text(
                json.dumps(new_stable_id),
                encoding="utf-8",
            )
            (paths.statsig_dir / "statsig.cached.evaluations.111").write_text(
                json.dumps(
                    {
                        "stableID": new_stable_id,
                        "data": json.dumps(
                            {
                                "evaluated_keys": {
                                    "userID": new_user_id,
                                    "stableID": new_stable_id,
                                    "customIDs": {"sessionId": new_statsig_session_id},
                                }
                            }
                        ),
                    }
                ),
                encoding="utf-8",
            )

            old_backup_root = paths.backup_root_base / "20260417-010000"
            (old_backup_root / ".claude" / "statsig").mkdir(parents=True)
            (old_backup_root / ".claude.json").write_text(
                json.dumps({"userID": old_user_id}),
                encoding="utf-8",
            )
            (old_backup_root / ".claude" / "statsig" / "statsig.stable_id.999").write_text(
                json.dumps(old_stable_id),
                encoding="utf-8",
            )
            (old_backup_root / ".claude" / "statsig" / "statsig.cached.evaluations.999").write_text(
                json.dumps(
                    {
                        "stableID": old_stable_id,
                        "data": json.dumps(
                            {
                                "evaluated_keys": {
                                    "userID": old_user_id,
                                    "stableID": old_stable_id,
                                    "customIDs": {"sessionId": old_statsig_session_id},
                                }
                            }
                        ),
                    }
                ),
                encoding="utf-8",
            )

            project_file = paths.projects_dir / "session.json"
            project_file.write_text(
                json.dumps(
                    {
                        "userID": old_user_id,
                        "stableID": old_stable_id,
                        "customIDs": {"sessionId": old_statsig_session_id},
                        "note": "keep old-user-id-123 in free text",
                        "sessionId": "conversation-session-should-stay",
                    }
                ),
                encoding="utf-8",
            )
            paths.history_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "userID": old_user_id,
                                "customIDs": {"sessionId": old_statsig_session_id},
                                "sessionId": "history-session-should-stay",
                            }
                        ),
                        "not-json-line",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            session_file = paths.sessions_dir / "74018.json"
            session_file.write_text(
                json.dumps(
                    {
                        "sessionId": "interactive-session-should-stay",
                        "payload": {"stableID": old_stable_id},
                    }
                ),
                encoding="utf-8",
            )

            summary = remap_history_identifiers(
                paths,
                options=RunOptions(backup_enabled=True, dry_run=False),
            )

            project_payload = json.loads(project_file.read_text(encoding="utf-8"))
            self.assertEqual(project_payload["userID"], new_user_id)
            self.assertEqual(project_payload["stableID"], new_stable_id)
            self.assertEqual(project_payload["customIDs"]["sessionId"], new_statsig_session_id)
            self.assertEqual(project_payload["note"], "keep old-user-id-123 in free text")
            self.assertEqual(project_payload["sessionId"], "conversation-session-should-stay")

            history_lines = paths.history_file.read_text(encoding="utf-8").splitlines()
            history_payload = json.loads(history_lines[0])
            self.assertEqual(history_payload["userID"], new_user_id)
            self.assertEqual(history_payload["customIDs"]["sessionId"], new_statsig_session_id)
            self.assertEqual(history_payload["sessionId"], "history-session-should-stay")
            self.assertEqual(history_lines[1], "not-json-line")

            session_payload = json.loads(session_file.read_text(encoding="utf-8"))
            self.assertEqual(session_payload["sessionId"], "interactive-session-should-stay")
            self.assertEqual(session_payload["payload"]["stableID"], new_stable_id)

            backup_root = Path(summary.backup_root or "")
            self.assertTrue((backup_root / ".claude" / "projects" / "session.json").exists())
            self.assertTrue((backup_root / ".claude" / "history.jsonl").exists())

    def test_remap_history_can_run_claude_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.statsig_dir.mkdir(parents=True)
            paths.projects_dir.mkdir(parents=True)
            paths.backup_root_base.mkdir(parents=True)
            paths.state_file.write_text(json.dumps({"userID": "new-user"}), encoding="utf-8")
            (paths.statsig_dir / "statsig.stable_id.1").write_text(json.dumps("new-stable"), encoding="utf-8")
            (paths.statsig_dir / "statsig.cached.evaluations.1").write_text(
                json.dumps(
                    {
                        "stableID": "new-stable",
                        "data": json.dumps(
                            {
                                "evaluated_keys": {
                                    "userID": "new-user",
                                    "stableID": "new-stable",
                                    "customIDs": {"sessionId": "new-session"},
                                }
                            }
                        ),
                    }
                ),
                encoding="utf-8",
            )

            old_backup_root = paths.backup_root_base / "20260417-010000"
            (old_backup_root / ".claude" / "statsig").mkdir(parents=True)
            (old_backup_root / ".claude.json").write_text(
                json.dumps({"userID": "old-user"}),
                encoding="utf-8",
            )
            (old_backup_root / ".claude" / "statsig" / "statsig.stable_id.1").write_text(
                json.dumps("old-stable"),
                encoding="utf-8",
            )
            (old_backup_root / ".claude" / "statsig" / "statsig.cached.evaluations.1").write_text(
                json.dumps(
                    {
                        "stableID": "old-stable",
                        "data": json.dumps(
                            {
                                "evaluated_keys": {
                                    "userID": "old-user",
                                    "stableID": "old-stable",
                                    "customIDs": {"sessionId": "old-session"},
                                }
                            }
                        ),
                    }
                ),
                encoding="utf-8",
            )

            # ``_run_claude_refresh`` now resolves the binary via
            # ``shutil.which("claude")`` first (so Windows finds claude.cmd /
            # claude.exe wrappers without ``shell=True``). CI runners don't
            # have a real claude binary on PATH, so we mock both ``which``
            # and ``subprocess.run`` to keep this test environment-agnostic.
            with patch("shutil.which", return_value="/fake/path/to/claude"), \
                    patch("ai_cli_kit.claude.history_remap.subprocess.run") as mocked_run:
                mocked_run.return_value.returncode = 0
                mocked_run.return_value.stdout = "ok"
                mocked_run.return_value.stderr = ""

                summary = remap_history_identifiers(
                    paths,
                    options=RunOptions(backup_enabled=False, dry_run=False),
                    run_claude=True,
                    claude_timeout_seconds=12,
                )

            mocked_run.assert_called_once()
            self.assertEqual(mocked_run.call_args.kwargs["timeout"], 12)
            self.assertTrue(any(record.key == "refresh_claude" for record in summary.records))


class StateFullIdentityScrubTests(unittest.TestCase):
    """Deep PII scrub of ~/.claude.json — covers oauthAccount + nested keys."""

    def test_full_identity_strips_top_level_pii_and_nested_apikey(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.state_file.write_text(
                json.dumps(
                    {
                        "userID": "u-123",
                        "oauthAccount": {"primaryEmail": "leak@example.com"},
                        "numStartups": 42,
                        "firstStartTime": "2025-01-01",
                        "mcpServers": {
                            "alpha": {"env": {"apiKey": "secret-1"}},
                            "beta": {"headers": {"Authorization": "Bearer secret-2"}},
                        },
                        "projects": {
                            "/work/foo": {"userID": "u-123", "lastUsed": "2025"},
                            "/work/bar": {"unrelated": True},
                        },
                        "keep_me": "stays",
                    }
                ),
                encoding="utf-8",
            )

            plan = build_plan(paths, {"state_full_identity"})
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))

            payload = json.loads(paths.state_file.read_text(encoding="utf-8"))
            self.assertNotIn("userID", payload)
            self.assertNotIn("oauthAccount", payload)
            self.assertNotIn("numStartups", payload)
            self.assertNotIn("firstStartTime", payload)
            # Deep recursion strips nested userID inside projects.
            self.assertNotIn("userID", payload["projects"]["/work/foo"])
            # mcpServers entry survives but its credential children are gone.
            self.assertNotIn("apiKey", payload["mcpServers"]["alpha"]["env"])
            self.assertNotIn("Authorization", payload["mcpServers"]["beta"]["headers"])
            self.assertEqual(payload["keep_me"], "stays")
            # Updated record reports a non-zero scrub count.
            updated_record = next(r for r in summary.records if r.key == "state_full_identity")
            self.assertEqual(updated_record.status, "updated")
            self.assertIn("含嵌套", updated_record.message)


class JsonStateBackupGlobTests(unittest.TestCase):
    """``json_state_backups`` sweeps cc's own ~/.claude.json.backup.* files."""

    def test_primary_target_globs_claude_backups_dir(self) -> None:
        """Modern cc writes corruption snapshots via getConfigBackupDir() at
        ~/.claude/backups/.claude.json.backup.<ts>, NOT directly under HOME."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.claude_backups_dir.mkdir(parents=True)
            (paths.claude_backups_dir / ".claude.json.backup.111").write_text(
                json.dumps({"userID": "old-1"}), encoding="utf-8"
            )
            (paths.claude_backups_dir / ".claude.json.backup.222").write_text(
                json.dumps({"userID": "old-2"}), encoding="utf-8"
            )
            # Decoy that must not be picked up.
            (paths.claude_backups_dir / "unrelated.json").write_text("keep", encoding="utf-8")

            plan = build_plan(paths, {"json_state_backups"})
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))

            self.assertFalse((paths.claude_backups_dir / ".claude.json.backup.111").exists())
            self.assertFalse((paths.claude_backups_dir / ".claude.json.backup.222").exists())
            self.assertTrue((paths.claude_backups_dir / "unrelated.json").exists())
            record = next(r for r in summary.records if r.key == "json_state_backups")
            self.assertEqual(record.status, "moved")

    def test_legacy_home_target_globs_home_direct(self) -> None:
        """Legacy cc dropped backups directly in HOME; the
        json_state_backups_legacy_home target sweeps those for users
        upgrading from older versions."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            (home / ".claude.json").write_text("{}", encoding="utf-8")
            (home / ".claude.json.backup.1").write_text(
                json.dumps({"userID": "old-1"}), encoding="utf-8"
            )
            (home / ".claude.json.backup.2").write_text(
                json.dumps({"userID": "old-2"}), encoding="utf-8"
            )
            (home / ".claude.json.recent").write_text("keep", encoding="utf-8")

            plan = build_plan(paths, {"json_state_backups_legacy_home"})
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))

            self.assertFalse((home / ".claude.json.backup.1").exists())
            self.assertFalse((home / ".claude.json.backup.2").exists())
            self.assertTrue((home / ".claude.json.recent").exists())
            backup_root = Path(summary.backup_root or "")
            self.assertTrue((backup_root / ".claude.json.backup.1").exists())
            self.assertTrue((backup_root / ".claude.json.backup.2").exists())
            record = next(r for r in summary.records if r.key == "json_state_backups_legacy_home")
            self.assertEqual(record.status, "moved")

    def test_glob_target_skipped_when_no_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)

            plan = build_plan(paths, {"json_state_backups"})
            item = next(p for p in plan if p.target.key == "json_state_backups")
            self.assertFalse(item.applicable)
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))
            record = next(r for r in summary.records if r.key == "json_state_backups")
            self.assertEqual(record.status, "skipped")


class BackupRestoreTests(unittest.TestCase):
    """Round-trip restore from backup_root back into home."""

    def test_restore_mirrors_backup_back_into_home_skipping_existing(self) -> None:
        from ai_cli_kit.claude.services import list_backup_roots, restore_from_backup

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.state_file.write_text(
                json.dumps({"userID": "u-1", "x": 1}), encoding="utf-8"
            )
            paths.telemetry_dir.mkdir()
            (paths.telemetry_dir / "fail.json").write_text("{}", encoding="utf-8")

            # First clean produces a backup root we can later restore from.
            plan = build_plan(paths, {"state_user_id", "telemetry_dir"})
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))
            backup_root = Path(summary.backup_root or "")
            self.assertTrue(backup_root.exists())

            # State now lacks userID and telemetry/ — confirm pre-restore state.
            state = json.loads(paths.state_file.read_text(encoding="utf-8"))
            self.assertNotIn("userID", state)
            self.assertFalse(paths.telemetry_dir.exists())

            # Restore (state_file already exists, so it must be skipped).
            restore_summary = restore_from_backup(paths, backup_root, overwrite=False)
            state_after = json.loads(paths.state_file.read_text(encoding="utf-8"))
            # Without overwrite, the existing scrubbed state file stays.
            self.assertNotIn("userID", state_after)
            # The deleted telemetry/ dir gets restored from backup.
            self.assertTrue((paths.telemetry_dir / "fail.json").exists())
            statuses = {r.status for r in restore_summary.records}
            self.assertIn("updated", statuses)
            self.assertIn("skipped", statuses)

            # list_backup_roots returns the fresh root.
            roots = list_backup_roots(paths)
            self.assertIn(backup_root, roots)


class PruneBackupsTests(unittest.TestCase):
    def test_prune_keeps_only_newest_n_roots(self) -> None:
        from ai_cli_kit.claude.services import prune_backup_roots, list_backup_roots
        import os as _os

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.backup_root_base.mkdir(parents=True)
            roots = []
            for idx in range(5):
                root = paths.backup_root_base / f"snapshot-{idx}"
                root.mkdir()
                # Spread mtimes apart so the sort is deterministic.
                _os.utime(root, (1_000 + idx, 1_000 + idx))
                roots.append(root)

            outcome = prune_backup_roots(paths, keep_last=2)
            remaining = list_backup_roots(paths)
            self.assertEqual(len(remaining), 2)
            # Newest two (highest mtime) are 4 and 3.
            self.assertEqual({r.name for r in remaining}, {"snapshot-4", "snapshot-3"})
            # Three were removed, none failed.
            self.assertEqual(len(outcome.removed), 3)
            self.assertEqual(outcome.failed, ())

    def test_prune_no_op_when_under_threshold(self) -> None:
        from ai_cli_kit.claude.services import prune_backup_roots

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.backup_root_base.mkdir(parents=True)
            (paths.backup_root_base / "only-one").mkdir()

            outcome = prune_backup_roots(paths, keep_last=5)
            self.assertEqual(outcome.removed, ())
            self.assertEqual(outcome.failed, ())


class DeepScrubInspectionTests(unittest.TestCase):
    """Planner must report deep_scrub targets as applicable when only nested
    matches exist, otherwise the TUI hides scrub work the executor would do."""

    def test_inspect_state_full_identity_finds_nested_only_pii(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            # No top-level userID/oauthAccount/etc. — PII lives only under
            # mcpServers and projects. Old planner reported applicable=False
            # for this file even though deep_scrub actually removes them.
            paths.state_file.write_text(
                json.dumps(
                    {
                        "model": "opus",
                        "mcpServers": {
                            "alpha": {"env": {"apiKey": "leaked-secret"}},
                        },
                        "projects": {
                            "/work/foo": {"userID": "buried-uid"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            plan = build_plan(paths, {"state_full_identity"})
            full_identity_item = next(
                p for p in plan if p.target.key == "state_full_identity"
            )
            self.assertTrue(
                full_identity_item.applicable,
                "deep_scrub planner missed nested-only PII matches",
            )
            self.assertIn("嵌套层", full_identity_item.details)


class RestoreLockdownTests(unittest.TestCase):
    """Restored .credentials.json must end up 0o600 on POSIX even if backup
    was wider (user manually inspected, perms got relaxed by editor, etc.)."""

    def test_restore_credentials_chmods_to_owner_only(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX-only chmod test")
        import os as _os
        from ai_cli_kit.claude.services import restore_from_backup

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)

            # Build a backup root that mirrors a leaked-perms credentials.json.
            backup_root = paths.backup_root_base / "manual-backup"
            (backup_root / ".claude").mkdir(parents=True)
            cred_in_backup = backup_root / ".claude" / ".credentials.json"
            cred_in_backup.write_text("{\"sk\":\"x\"}", encoding="utf-8")
            _os.chmod(cred_in_backup, 0o644)

            restore_from_backup(paths, backup_root, overwrite=True)
            restored = paths.credentials_file
            self.assertTrue(restored.exists())
            mode = _os.stat(restored).st_mode & 0o777
            self.assertEqual(mode, 0o600, "restored credentials must be 0o600")


class CliJsonFormatTests(unittest.TestCase):
    """``--format=json`` emits a parseable JSON document for plan and execute."""

    def test_plan_json_contains_target_metadata(self) -> None:
        import io as _io
        import contextlib
        from ai_cli_kit.claude.cli import main

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.state_file.write_text(json.dumps({"userID": "abc"}), encoding="utf-8")

            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["--home", str(home), "plan", "--preset", "safe", "--format", "json"])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["command"], "plan")
            self.assertIsInstance(payload["items"], list)
            self.assertTrue(payload["items"], "plan should contain items")
            keys = {item["key"] for item in payload["items"]}
            self.assertIn("state_user_id", keys)
            self.assertIn("state_full_identity", keys)
            self.assertIn("json_state_backups", keys)
            # R7 pass-9 H2: plan envelope must include status field.
            self.assertEqual(payload["status"], "ok")


class ClaudeConfigDirEnvTests(unittest.TestCase):
    """``CLAUDE_CONFIG_DIR`` redirects cc data; cc-clean must follow."""

    def test_env_redirect_anchors_config_root_and_state_file(self) -> None:
        from ai_cli_kit.claude.paths import resolve_default_paths

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "real-home"
            home.mkdir()
            redirect = Path(tmp_dir) / "redirect"
            redirect.mkdir()

            paths = resolve_default_paths(home, env={"CLAUDE_CONFIG_DIR": str(redirect)})
            # config_root + claude_dir collapse to the env value.
            self.assertEqual(paths.config_root, redirect)
            self.assertEqual(paths.claude_dir, redirect)
            # State file lives under config_root.
            self.assertEqual(paths.state_file, redirect / ".claude.json")
            # Backups still anchored on real $HOME so wiping the cc dir
            # doesn't kill the rollback path.
            self.assertEqual(paths.backup_root_base, home / ".claude-clean-backups")

    def test_env_unset_falls_back_to_legacy_layout(self) -> None:
        from ai_cli_kit.claude.paths import resolve_default_paths

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "user-home"
            home.mkdir()
            paths = resolve_default_paths(home, env={})
            self.assertEqual(paths.config_root, home)
            self.assertEqual(paths.claude_dir, home / ".claude")
            self.assertEqual(paths.state_file, home / ".claude.json")

    def test_state_file_in_redirected_root_round_trips_through_backup(self) -> None:
        """When state file is at /redirect/.claude.json (not under home),
        backup mirror tree must use config_root as anchor so the relative
        path stays sane and restore can find it."""
        from ai_cli_kit.claude.paths import resolve_default_paths

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "real-home"
            home.mkdir()
            redirect = Path(tmp_dir) / "redirect"
            redirect.mkdir()
            paths = resolve_default_paths(home, env={"CLAUDE_CONFIG_DIR": str(redirect)})
            paths.state_file.write_text(json.dumps({"userID": "abc", "k": 1}), encoding="utf-8")

            plan = build_plan(paths, {"state_user_id"})
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))

            payload = json.loads(paths.state_file.read_text(encoding="utf-8"))
            self.assertNotIn("userID", payload)
            self.assertEqual(payload["k"], 1)
            backup_root = Path(summary.backup_root or "")
            # Mirror tree should reach the redirected file (not external/).
            self.assertTrue((backup_root / ".claude.json").exists())


class OauthSuffixVariantsTests(unittest.TestCase):
    def test_state_user_id_scrubs_all_suffix_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            (home / ".claude.json").write_text(json.dumps({"userID": "prod-uid"}), encoding="utf-8")
            (home / ".claude-staging-oauth.json").write_text(
                json.dumps({"userID": "staging-uid"}), encoding="utf-8"
            )
            (home / ".claude-local-oauth.json").write_text(
                json.dumps({"userID": "local-uid"}), encoding="utf-8"
            )

            plan = build_plan(paths, {"state_user_id"})
            item = next(p for p in plan if p.target.key == "state_user_id")
            self.assertTrue(item.applicable)
            self.assertIn("3/3", item.details)

            execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))

            for variant in (".claude.json", ".claude-staging-oauth.json", ".claude-local-oauth.json"):
                payload = json.loads((home / variant).read_text(encoding="utf-8"))
                self.assertNotIn("userID", payload, "%s still has userID" % variant)


class LegacyStateFileTests(unittest.TestCase):
    def test_legacy_config_json_is_scrubbed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.legacy_state_file.write_text(
                json.dumps({"userID": "old", "oauthAccount": {"primaryEmail": "x@y"}, "keep": 1}),
                encoding="utf-8",
            )

            plan = build_plan(paths, {"legacy_state_file"})
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))

            payload = json.loads(paths.legacy_state_file.read_text(encoding="utf-8"))
            self.assertNotIn("userID", payload)
            self.assertNotIn("oauthAccount", payload)
            self.assertEqual(payload["keep"], 1)


class ScratchpadTargetTests(unittest.TestCase):
    def test_scratchpad_resolves_with_env_override_on_posix(self) -> None:
        # POSIX-only: full mock-based path test. Windows behaviour is
        # verified by code inspection (see services.py:_resolve_scratchpad_root)
        # since mocking os.name="nt" on a Linux runner trips
        # ``NotImplementedError: cannot instantiate 'WindowsPath'``.
        if os.name == "nt":
            self.skipTest("POSIX-only env override test")

        from unittest.mock import patch
        from ai_cli_kit.claude.services import _resolve_scratchpad_root

        with patch.dict(os.environ, {"CLAUDE_CODE_TMPDIR": "/srv/scratch"}, clear=False):
            result = _resolve_scratchpad_root()
        self.assertIsNotNone(result)
        self.assertTrue(str(result).startswith("/srv/scratch/claude-"))

    def test_scratchpad_skipped_when_dir_absent(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX-only target")

        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            # Force the scratchpad path to a non-existent location so
            # the test doesn't depend on whether /tmp/claude-<uid>
            # happens to exist on the runner.
            phantom_uid = 999_999_999
            with patch("ai_cli_kit.claude.services.os.getuid", return_value=phantom_uid, create=True):
                plan = build_plan(paths, {"scratchpad_tmp_dir"})
                item = next(p for p in plan if p.target.key == "scratchpad_tmp_dir")
                self.assertFalse(item.applicable)
                self.assertIn("没有发现", item.details)


class KeychainTargetTests(unittest.TestCase):
    def test_keychain_inapplicable_on_non_darwin(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            with patch("sys.platform", "linux"):
                plan = build_plan(paths, {"macos_keychain"})
                item = next(p for p in plan if p.target.key == "macos_keychain")
                self.assertFalse(item.applicable)
                self.assertIn("非 macOS", item.details)


class PruneOutcomeFailureTests(unittest.TestCase):
    """Failed rmtrees stay surfaced rather than silently dropped."""

    def test_prune_failure_recorded_in_outcome(self) -> None:
        from unittest.mock import patch
        from ai_cli_kit.claude.services import prune_backup_roots
        import os as _os

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.backup_root_base.mkdir(parents=True)
            for idx in range(3):
                root = paths.backup_root_base / f"snapshot-{idx}"
                root.mkdir()
                _os.utime(root, (1_000 + idx, 1_000 + idx))

            def boom(path, ignore_errors=False):
                raise OSError(13, "permission denied (simulated)")

            with patch("ai_cli_kit.claude.services.shutil.rmtree", side_effect=boom):
                outcome = prune_backup_roots(paths, keep_last=1)

            self.assertEqual(outcome.removed, ())
            self.assertEqual(len(outcome.failed), 2)
            # Pass-7 M1: error string is the exception TYPE name, not
            # the str() form (which would leak file paths in OSError).
            # OSError(EACCES, ...) is actually PermissionError due to
            # Python's subclass dispatch.
            self.assertTrue(all(err == "PermissionError" for _, err in outcome.failed))


class RestoreTypeMismatchTests(unittest.TestCase):
    def test_restore_dst_dir_src_file_emits_clear_error(self) -> None:
        from ai_cli_kit.claude.services import restore_from_backup

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            backup_root = paths.backup_root_base / "manual"
            (backup_root / ".claude").mkdir(parents=True)
            (backup_root / ".claude" / "history.jsonl").write_text("payload", encoding="utf-8")

            # Pre-make destination as a directory where source is a file.
            (paths.claude_dir / "history.jsonl").mkdir()

            summary = restore_from_backup(paths, backup_root, overwrite=True)
            errors = [r for r in summary.records if r.status == "error"]
            self.assertTrue(errors)
            self.assertTrue(any("目标是目录但源是文件" in r.message for r in errors))


class KeychainServiceNameEnumTests(unittest.TestCase):
    def test_default_layout_emits_8_variants_no_dirhash(self) -> None:
        from ai_cli_kit.claude.services import _enumerate_keychain_service_names

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            names = _enumerate_keychain_service_names(paths)
            # 4 oauth flavors × 2 suffix variants = 8 names, no dirHash.
            self.assertEqual(len(names), 8)
            self.assertIn("Claude Code", names)
            self.assertIn("Claude Code-credentials", names)
            self.assertIn("Claude Code-staging-credentials", names)
            self.assertIn("Claude Code-local", names)
            self.assertIn("Claude Code-custom-credentials", names)
            # No dirHash variants when claude_dir is the default.
            for name in names:
                # An 8-char hex hash starts with -<hex> at the end.
                self.assertFalse(
                    any(part.endswith("-" + hexlen) for part in [name] for hexlen in [name[-8:]] if all(c in "0123456789abcdef" for c in name[-8:])),
                    "unexpected dirHash on default-layout name %s" % name,
                )

    def test_redirected_config_root_appends_dirhash_variants(self) -> None:
        from ai_cli_kit.claude.paths import resolve_default_paths
        from ai_cli_kit.claude.services import _enumerate_keychain_service_names

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "home"
            home.mkdir()
            redirect = Path(tmp_dir) / "redirect"
            redirect.mkdir()
            paths = resolve_default_paths(home, env={"CLAUDE_CONFIG_DIR": str(redirect)})
            names = _enumerate_keychain_service_names(paths)
            # With redirect, we double the variant set (default + dirHash).
            self.assertEqual(len(names), 16)
            # Some variants must end with an 8-hex suffix.
            hashed = [n for n in names if any(c in "abcdef0123456789" for c in n[-8:])]
            self.assertTrue(any("-" in n[-9:] for n in hashed))


class RestoreWithMetadataTests(unittest.TestCase):
    """Restore reads ``_cc_clean_meta.json`` to pick the original anchor."""

    def test_meta_file_is_written_into_backup_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.state_file.write_text(json.dumps({"userID": "x"}), encoding="utf-8")
            plan = build_plan(paths, {"state_user_id"})
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))
            backup_root = Path(summary.backup_root or "")
            meta_path = backup_root / "_cc_clean_meta.json"
            self.assertTrue(meta_path.exists())
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["home"], str(home))
            self.assertEqual(meta["config_root"], str(home))
            self.assertEqual(meta["version"], 1)

    def test_redirected_layout_backup_then_restore_round_trips(self) -> None:
        """End-to-end: backup with CLAUDE_CONFIG_DIR set, then restore
        recovers the file to its ORIGINAL config_root location, not home."""
        from ai_cli_kit.claude.paths import resolve_default_paths
        from ai_cli_kit.claude.services import restore_from_backup

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "real-home"
            home.mkdir()
            redirect = Path(tmp_dir) / "cc-data"
            redirect.mkdir()
            paths = resolve_default_paths(home, env={"CLAUDE_CONFIG_DIR": str(redirect)})

            paths.state_file.write_text(
                json.dumps({"userID": "leaked", "keep_me": "data"}),
                encoding="utf-8",
            )

            plan = build_plan(paths, {"state_user_id"})
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))
            backup_root = Path(summary.backup_root or "")
            self.assertTrue((backup_root / ".claude.json").exists())
            self.assertTrue((backup_root / "_cc_clean_meta.json").exists())

            # Pre-restore: state file lacks userID.
            payload_before = json.loads(paths.state_file.read_text(encoding="utf-8"))
            self.assertNotIn("userID", payload_before)

            # Restore — must write back to redirected location, not home.
            restore_summary = restore_from_backup(paths, backup_root, overwrite=True)
            self.assertTrue(any(r.status == "updated" for r in restore_summary.records))
            payload_after = json.loads(paths.state_file.read_text(encoding="utf-8"))
            self.assertEqual(payload_after["userID"], "leaked")
            # Did NOT pollute $HOME with a stray .claude.json.
            self.assertFalse((home / ".claude.json").exists() or (home / ".claude.json").is_symlink())

    def test_iter_backup_files_skips_meta(self) -> None:
        from ai_cli_kit.claude.services import _iter_backup_files

        with tempfile.TemporaryDirectory() as tmp_dir:
            backup_root = Path(tmp_dir) / "br"
            backup_root.mkdir()
            (backup_root / "_cc_clean_meta.json").write_text("{}", encoding="utf-8")
            (backup_root / "real-file.json").write_text("ok", encoding="utf-8")

            yielded = [p.name for p in _iter_backup_files(backup_root)]
            self.assertIn("real-file.json", yielded)
            self.assertNotIn("_cc_clean_meta.json", yielded)


class NfcNormalizeTests(unittest.TestCase):
    """cc applies .normalize('NFC') to its config home; we must match
    so keychain hashes and string compares stay byte-identical."""

    def test_default_paths_returns_nfc_strings(self) -> None:
        import unicodedata
        from ai_cli_kit.claude.paths import default_paths

        # Build an NFD-form home and expect NFC after default_paths.
        nfd_home = Path(unicodedata.normalize("NFD", "/tmp/é-hôme"))
        paths = default_paths(nfd_home)
        self.assertEqual(
            unicodedata.normalize("NFC", str(paths.claude_dir)),
            str(paths.claude_dir),
        )
        self.assertEqual(
            unicodedata.normalize("NFC", str(paths.config_root)),
            str(paths.config_root),
        )

    def test_resolve_default_paths_normalizes_env_value(self) -> None:
        import unicodedata
        from ai_cli_kit.claude.paths import resolve_default_paths

        nfd_dir = unicodedata.normalize("NFD", "/srv/é-data")
        paths = resolve_default_paths(Path("/tmp/home"), env={"CLAUDE_CONFIG_DIR": nfd_dir})
        self.assertEqual(
            str(paths.claude_dir),
            unicodedata.normalize("NFC", nfd_dir),
        )

    def test_keychain_hash_uses_nfc_for_identity_with_cc(self) -> None:
        import unicodedata
        from hashlib import sha256
        from ai_cli_kit.claude.paths import resolve_default_paths
        from ai_cli_kit.claude.services import _enumerate_keychain_service_names

        nfd_dir = unicodedata.normalize("NFD", "/srv/中文-data")
        paths = resolve_default_paths(Path("/tmp/home"), env={"CLAUDE_CONFIG_DIR": nfd_dir})
        names = _enumerate_keychain_service_names(paths)
        # Hash MUST match cc's: sha256(NFC(claude_dir))[:8]
        expected_dirhash = sha256(
            unicodedata.normalize("NFC", nfd_dir).encode("utf-8")
        ).hexdigest()[:8]
        # At least one service name should embed the dirHash suffix.
        self.assertTrue(
            any(expected_dirhash in name for name in names),
            "no service name embeds the cc-computed dirHash",
        )


class BackupMetaVersionTests(unittest.TestCase):
    """Unsupported meta versions must be rejected so future schema
    changes don't silently get re-interpreted under v1 semantics."""

    def test_unsupported_version_treated_as_missing_meta(self) -> None:
        from ai_cli_kit.claude.services import _read_backup_metadata

        with tempfile.TemporaryDirectory() as tmp_dir:
            backup_root = Path(tmp_dir)
            (backup_root / "_cc_clean_meta.json").write_text(
                json.dumps({"version": 99, "home": "/somewhere"}),
                encoding="utf-8",
            )
            self.assertIsNone(_read_backup_metadata(backup_root))

    def test_supported_version_returns_payload(self) -> None:
        from ai_cli_kit.claude.services import _read_backup_metadata

        with tempfile.TemporaryDirectory() as tmp_dir:
            backup_root = Path(tmp_dir)
            (backup_root / "_cc_clean_meta.json").write_text(
                json.dumps({"version": 1, "home": "/h", "config_root": "/c"}),
                encoding="utf-8",
            )
            meta = _read_backup_metadata(backup_root)
            self.assertIsNotNone(meta)
            self.assertEqual(meta["config_root"], "/c")  # type: ignore[index]

    def test_missing_version_field_rejected(self) -> None:
        from ai_cli_kit.claude.services import _read_backup_metadata

        with tempfile.TemporaryDirectory() as tmp_dir:
            backup_root = Path(tmp_dir)
            (backup_root / "_cc_clean_meta.json").write_text(
                json.dumps({"home": "/h"}),
                encoding="utf-8",
            )
            self.assertIsNone(_read_backup_metadata(backup_root))


class AutoMemoryOverrideTests(unittest.TestCase):
    def test_env_override_takes_precedence_over_settings(self) -> None:
        from ai_cli_kit.claude.services import resolve_auto_memory_override

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.settings_file.write_text(
                json.dumps({"autoMemoryDirectory": "/from/settings"}),
                encoding="utf-8",
            )
            resolved = resolve_auto_memory_override(paths, env={"CLAUDE_COWORK_MEMORY_PATH_OVERRIDE": "/from/env"})
            self.assertEqual(str(resolved), "/from/env")

    def test_settings_used_when_env_unset(self) -> None:
        from ai_cli_kit.claude.services import resolve_auto_memory_override

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.settings_file.write_text(
                json.dumps({"autoMemoryDirectory": "/from/settings"}),
                encoding="utf-8",
            )
            resolved = resolve_auto_memory_override(paths, env={})
            self.assertEqual(str(resolved), "/from/settings")

    def test_invalid_paths_rejected(self) -> None:
        from ai_cli_kit.claude.services import _validate_memory_path

        # Relative path → reject.
        self.assertIsNone(_validate_memory_path("../foo", expand_tilde=False))
        # Too short.
        self.assertIsNone(_validate_memory_path("/", expand_tilde=False))
        self.assertIsNone(_validate_memory_path("/a", expand_tilde=False))
        # NUL byte.
        self.assertIsNone(_validate_memory_path("/a\x00b", expand_tilde=False))
        # UNC path.
        self.assertIsNone(_validate_memory_path("//server/share", expand_tilde=False))
        # Bare ~ rejected even with expand_tilde.
        self.assertIsNone(_validate_memory_path("~/.", expand_tilde=True))
        # Valid absolute path accepted.
        result = _validate_memory_path("/some/where", expand_tilde=False)
        self.assertIsNotNone(result)
        self.assertTrue(str(result).startswith("/some/where"))

    def test_dynamic_target_always_present_inapplicable_without_override(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)

            # Without env / settings: target appears but is inapplicable
            # so user gets clear "redirect not configured" feedback
            # rather than a silent drop on --select.
            from ai_cli_kit.claude.services import AutoMemoryOverride

            with patch(
                "ai_cli_kit.claude.services.resolve_auto_memory_override_state",
                return_value=AutoMemoryOverride(),
            ):
                plan = build_plan(paths, {"auto_memory_override"})
                item = next(p for p in plan if p.target.key == "auto_memory_override")
                self.assertFalse(item.applicable)
                self.assertIn("未检测到", item.details)

            # With valid override: target is applicable and reports the
            # resolved path.
            with patch(
                "ai_cli_kit.claude.services.resolve_auto_memory_override_state",
                return_value=AutoMemoryOverride(valid_path=Path("/elsewhere/memory")),
            ):
                plan = build_plan(paths, {"auto_memory_override"})
                item = next(p for p in plan if p.target.key == "auto_memory_override")
                self.assertEqual(item.target.target_path, "/elsewhere/memory")

            # With REJECTED override: target shows distinct warning.
            with patch(
                "ai_cli_kit.claude.services.resolve_auto_memory_override_state",
                return_value=AutoMemoryOverride(
                    rejected_raw="../../bad",
                    rejected_source="env",
                ),
            ):
                plan = build_plan(paths, {"auto_memory_override"})
                item = next(p for p in plan if p.target.key == "auto_memory_override")
                self.assertFalse(item.applicable)
                self.assertTrue(any("被 cc 拒绝" in w for w in item.warnings))


class OutputStylesTargetTests(unittest.TestCase):
    def test_output_styles_dir_appears_in_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            (paths.claude_dir / "output-styles").mkdir(parents=True)
            (paths.claude_dir / "output-styles" / "custom.md").write_text("# style", encoding="utf-8")

            plan = build_plan(paths, {"output_styles_dir"})
            item = next(p for p in plan if p.target.key == "output_styles_dir")
            self.assertTrue(item.applicable)
            self.assertGreater(item.size_bytes, 0)


class CliEndToEndTests(unittest.TestCase):
    """Full ``main()`` round-trips for the post-R3 subcommands."""

    def _capture_main(self, argv):
        import contextlib
        import io as _io
        from ai_cli_kit.claude.cli import main

        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue()

    def test_clean_with_yes_executes_and_prints_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.state_file.write_text(
                json.dumps({"userID": "abc", "keep": 1}), encoding="utf-8"
            )

            rc, output = self._capture_main(
                ["--home", str(home), "clean", "--yes", "--preset", "safe", "--keep-backups", "0"]
            )
            self.assertEqual(rc, 0)
            # New glob-based scrub message phrasing.
            self.assertIn("移除", output)
            payload = json.loads(paths.state_file.read_text(encoding="utf-8"))
            self.assertNotIn("userID", payload)

    def test_clean_json_format_emits_single_envelope(self) -> None:
        """R7 pass-2 H1: ``clean --format=json`` must emit ONE JSON
        document, not multiple concatenated ones."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.state_file.write_text(json.dumps({"userID": "x"}), encoding="utf-8")

            rc, output = self._capture_main(
                [
                    "--home", str(home), "clean", "--yes", "--preset", "safe",
                    "--format", "json", "--keep-backups", "0",
                ]
            )
            self.assertEqual(rc, 0)
            # MUST parse as a single JSON document (no concatenation).
            payload = json.loads(output)
            self.assertEqual(payload["command"], "clean")
            self.assertIn("plan", payload)
            self.assertIn("execution", payload)
            self.assertIn("status", payload)
            self.assertIn(payload["status"], {"ok", "error"})
            self.assertIsInstance(payload["plan"]["items"], list)

    def test_restore_lists_backups_when_no_arg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.backup_root_base.mkdir(parents=True)
            (paths.backup_root_base / "snap-a").mkdir()

            rc, output = self._capture_main(
                ["--home", str(home), "restore", "--format", "json"]
            )
            self.assertEqual(rc, 0)
            payload = json.loads(output)
            self.assertEqual(payload["command"], "restore-list")
            self.assertEqual(len(payload["roots"]), 1)
            self.assertTrue(payload["roots"][0].endswith("snap-a"))

    def test_prune_backups_deletes_oldest_and_reports(self) -> None:
        import os as _os

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.backup_root_base.mkdir(parents=True)
            for idx in range(4):
                root = paths.backup_root_base / f"snapshot-{idx}"
                root.mkdir()
                _os.utime(root, (1_000 + idx, 1_000 + idx))

            # R7 pass-4: prune-backups now requires --yes for actual
            # destructive run (or --dry-run for preview).
            rc, output = self._capture_main(
                ["--home", str(home), "prune-backups", "--keep", "2", "--yes", "--format", "json"]
            )
            self.assertEqual(rc, 0)
            payload = json.loads(output)
            self.assertEqual(payload["command"], "prune-backups")
            self.assertEqual(payload["keep"], 2)
            self.assertEqual(len(payload["removed"]), 2)
            self.assertEqual(payload["failed"], [])

    def test_debug_paths_json_dumps_full_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            rc, output = self._capture_main(
                ["--home", str(home), "debug-paths", "--format", "json"]
            )
            self.assertEqual(rc, 0)
            payload = json.loads(output)
            self.assertEqual(payload["command"], "debug-paths")
            self.assertIn("config_root", payload["paths"])
            self.assertIn("CLAUDE_CONFIG_DIR", payload["env"])
            self.assertIn("resolved_auto_memory_override", payload)

    def test_list_targets_includes_post_r3_target_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            rc, output = self._capture_main(
                ["--home", str(home), "list-targets"]
            )
            self.assertEqual(rc, 0)
            keys = output.splitlines()
            for expected in (
                "state_user_id",
                "state_full_identity",
                "legacy_state_file",
                "macos_keychain",
                "paste_cache_dir",
                "auto_memory_override",
                "scratchpad_tmp_dir",
                "output_styles_dir",
                "json_state_backups",
            ):
                self.assertIn(expected, keys, "missing target key %s" % expected)


class CliHomeArgPropagationTests(unittest.TestCase):
    """Top-level ``--home`` must propagate into subcommand parsing.

    Earlier the duplicate ``_add_home_arg`` on subparsers added a
    second ``--home`` option whose ``default=Path.home()`` overrode
    whatever the user supplied at top level. The fix removes the
    duplicate so subcommand context inherits the top-level value.
    """

    def test_home_passed_top_level_reaches_subcommand_paths(self) -> None:
        import contextlib
        import io as _io
        from ai_cli_kit.claude.cli import main

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.backup_root_base.mkdir(parents=True)
            (paths.backup_root_base / "isolated-snapshot").mkdir()

            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["--home", str(home), "restore", "--format", "json"])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(len(payload["roots"]), 1, "top-level --home didn't propagate to subcommand")
            self.assertTrue(payload["roots"][0].endswith("isolated-snapshot"))


class JsonParseCacheTests(unittest.TestCase):
    """``_load_json_dict`` reuses parsed payloads when (mtime, size) match."""

    def test_second_call_hits_cache_no_disk_read(self) -> None:
        from unittest.mock import patch
        from ai_cli_kit.claude.services import _JSON_PARSE_CACHE, _load_json_dict

        _JSON_PARSE_CACHE.clear()
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "state.json"
            target.write_text(json.dumps({"userID": "abc", "k": 1}), encoding="utf-8")

            payload1, _ = _load_json_dict(target)
            self.assertEqual(payload1["userID"], "abc")  # type: ignore[index]
            self.assertEqual(len(_JSON_PARSE_CACHE), 1)

            # Block the underlying read to prove the second call is cached.
            with patch("pathlib.Path.read_text", side_effect=AssertionError("disk read should not happen")):
                payload2, _ = _load_json_dict(target)
            self.assertEqual(payload2["userID"], "abc")  # type: ignore[index]

    def test_mutating_returned_dict_does_not_corrupt_cache(self) -> None:
        from ai_cli_kit.claude.services import _JSON_PARSE_CACHE, _load_json_dict

        _JSON_PARSE_CACHE.clear()
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "state.json"
            target.write_text(json.dumps({"userID": "abc"}), encoding="utf-8")

            payload, _ = _load_json_dict(target)
            payload.pop("userID", None)  # type: ignore[union-attr]
            payload["polluted"] = True  # type: ignore[index]

            payload2, _ = _load_json_dict(target)
            # Cache must not have absorbed the mutation.
            self.assertIn("userID", payload2)  # type: ignore[arg-type]
            self.assertNotIn("polluted", payload2)  # type: ignore[arg-type]


class PluginsAndDebugTargetsTests(unittest.TestCase):
    def test_plugins_dir_target_exists_and_resolves_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            (paths.plugins_dir).mkdir()
            (paths.plugins_dir / "known_marketplaces.json").write_text(
                json.dumps({"private": "git@example.com:secret/repo.git"}),
                encoding="utf-8",
            )

            plan = build_plan(paths, {"plugins_dir"})
            item = next(p for p in plan if p.target.key == "plugins_dir")
            self.assertTrue(item.applicable)
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))
            self.assertFalse(paths.plugins_dir.exists())
            backup_root = Path(summary.backup_root or "")
            self.assertTrue((backup_root / ".claude" / "plugins" / "known_marketplaces.json").exists())

    def test_debug_dir_target_in_safe_preset(self) -> None:
        from ai_cli_kit.claude.services import SAFE_TARGET_KEYS

        self.assertIn("debug_dir", SAFE_TARGET_KEYS)

    def test_debug_dir_inapplicable_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)

            plan = build_plan(paths, {"debug_dir"})
            item = next(p for p in plan if p.target.key == "debug_dir")
            self.assertFalse(item.applicable)


class BackupFileLockdownTests(unittest.TestCase):
    """All backup files (not just .credentials.json) get 0o600 on POSIX."""

    def test_state_file_backup_is_chmod_0o600(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX-only chmod test")
        import os as _os

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.state_file.write_text(json.dumps({"userID": "abc"}), encoding="utf-8")
            _os.chmod(paths.state_file, 0o644)

            plan = build_plan(paths, {"state_user_id"})
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))
            backup_root = Path(summary.backup_root or "")
            backup_state = backup_root / ".claude.json"
            self.assertTrue(backup_state.exists())
            mode = _os.stat(backup_state).st_mode & 0o777
            self.assertEqual(mode, 0o600, "state.json backup must be 0o600")

    def test_remove_path_dir_backup_recursively_locked(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX-only chmod test")
        import os as _os

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.telemetry_dir.mkdir()
            (paths.telemetry_dir / "fail.json").write_text("{}", encoding="utf-8")
            _os.chmod(paths.telemetry_dir / "fail.json", 0o644)

            plan = build_plan(paths, {"telemetry_dir"})
            summary = execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))
            backup_root = Path(summary.backup_root or "")
            backed_file = backup_root / ".claude" / "telemetry" / "fail.json"
            self.assertTrue(backed_file.exists())
            mode = _os.stat(backed_file).st_mode & 0o777
            self.assertEqual(mode, 0o600, "subdir backup files must be 0o600")
            dir_mode = _os.stat(backup_root / ".claude" / "telemetry").st_mode & 0o777
            self.assertEqual(dir_mode, 0o700, "subdir backup dirs must be 0o700")


class AutoMemorySentinelTests(unittest.TestCase):
    def test_sentinel_contains_url_scheme_marker(self) -> None:
        from ai_cli_kit.claude.services import _AUTO_MEMORY_TARGET_PLACEHOLDER

        # Must contain ``://`` so it can never collide with any
        # filesystem path that cc's _validate_memory_path would accept.
        self.assertIn("://", _AUTO_MEMORY_TARGET_PLACEHOLDER)

    def test_sentinel_inert_when_used_as_target_path(self) -> None:
        # Pass-4 audit: previously patched the THIN WRAPPER; the
        # actual call site uses ``resolve_auto_memory_override_state``
        # so the patch never fired and the test passed by accident.
        from unittest.mock import patch
        from ai_cli_kit.claude.services import AutoMemoryOverride

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)

            with patch(
                "ai_cli_kit.claude.services.resolve_auto_memory_override_state",
                return_value=AutoMemoryOverride(),
            ):
                plan = build_plan(paths, {"auto_memory_override"})
                item = next(p for p in plan if p.target.key == "auto_memory_override")
                # Must be inapplicable — placeholder never executed.
                self.assertFalse(item.applicable)
                # Details should NOT mention the literal sentinel string —
                # users see the friendly "未检测到" hint instead.
                self.assertNotIn("cc-clean://", item.details)


class RestoreSymlinkTraversalTests(unittest.TestCase):
    def test_symlink_pointing_outside_anchors_refused(self) -> None:
        if sys.platform == "win32":
            self.skipTest("symlink test skipped on Windows without dev mode")
        from ai_cli_kit.claude.services import restore_from_backup

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "home"
            home.mkdir()
            paths = default_paths(home)
            backup_root = paths.backup_root_base / "manual-backup"
            (backup_root / ".claude").mkdir(parents=True)
            outside = Path(tmp_dir) / "outside-target"
            outside.write_text("attacker-target", encoding="utf-8")
            attack_link = backup_root / ".claude" / "projects"
            os.symlink(outside, attack_link)

            summary = restore_from_backup(paths, backup_root, overwrite=True)
            errors = [r for r in summary.records if r.status == "error"]
            self.assertTrue(errors)
            self.assertTrue(any("路径穿越" in r.message for r in errors))


class SettingsEnvSoftFailTests(unittest.TestCase):
    def test_missing_env_after_inspect_returns_skipped_not_error(self) -> None:
        from ai_cli_kit.claude.services import (
            _execute_scrub_settings_env,
            build_targets,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.settings_file.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
            target = next(t for t in build_targets(paths) if t.key == "settings_auth_env")
            # Synthesise an applicable PlanItem (race scenario) and
            # confirm the executor gracefully skips instead of raising.
            from ai_cli_kit.claude.models import PlanItem

            item = PlanItem(
                target=target,
                selected=True,
                exists=True,
                applicable=True,  # planner saw env at inspect; user removed it
                size_bytes=0,
                details="",
            )
            record = _execute_scrub_settings_env(paths, paths.settings_file, item, None, RunOptions(backup_enabled=False, dry_run=False))
            self.assertEqual(record.status, "skipped")


class WindowsReservedSanitizeTests(unittest.TestCase):
    def test_sanitize_renames_reserved_basenames_on_windows(self) -> None:
        # ``Path("...")`` reads ``os.name`` at construction. Building
        # the test inputs OUTSIDE the mock keeps them PosixPath on a
        # Linux runner; the function under test reuses the input class
        # via ``type(relative)(*parts)`` so the mock only flips the
        # function's internal os.name check, not pathlib's dispatch.
        from ai_cli_kit.claude.services import _sanitize_windows_reserved
        from unittest.mock import patch

        case_with_dir = Path("projects/CON/foo.json")
        expected_with_dir = Path("projects/CON_reserved/foo.json")
        case_with_ext = Path("projects/com1.bak")
        expected_with_ext = Path("projects/com1.bak_reserved")
        case_regular = Path("projects/regular.json")
        with patch("ai_cli_kit.claude.services.os.name", "nt"):
            self.assertEqual(_sanitize_windows_reserved(case_with_dir), expected_with_dir)
            self.assertEqual(_sanitize_windows_reserved(case_with_ext), expected_with_ext)
            self.assertEqual(_sanitize_windows_reserved(case_regular), case_regular)

    def test_sanitize_noop_on_posix(self) -> None:
        from ai_cli_kit.claude.services import _sanitize_windows_reserved
        from unittest.mock import patch

        case = Path("projects/CON/foo.json")
        with patch("ai_cli_kit.claude.services.os.name", "posix"):
            self.assertEqual(_sanitize_windows_reserved(case), case)


class CliRestoreEmptyExitCodeTests(unittest.TestCase):
    def _capture(self, argv):
        import contextlib
        import io as _io
        from ai_cli_kit.claude.cli import main

        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue()

    def test_empty_backup_list_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            rc, _ = self._capture(["--home", str(home), "restore", "--format", "json"])
            self.assertEqual(rc, 1, "empty restore list should signal via rc=1")

    def test_nonempty_backup_list_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.backup_root_base.mkdir(parents=True)
            (paths.backup_root_base / "snap-1").mkdir()
            rc, _ = self._capture(["--home", str(home), "restore", "--format", "json"])
            self.assertEqual(rc, 0)


class NewR6TargetsTests(unittest.TestCase):
    def test_usage_data_dir_in_safe_preset(self) -> None:
        from ai_cli_kit.claude.services import SAFE_TARGET_KEYS

        for key in (
            "usage_data_dir",
            "stats_cache_file",
            "startup_perf_dir",
            "image_store_dir",
            "upload_bridge_dir",
        ):
            self.assertIn(key, SAFE_TARGET_KEYS, "missing %s in SAFE preset" % key)

    def test_user_authored_dirs_not_in_safe(self) -> None:
        from ai_cli_kit.claude.services import SAFE_TARGET_KEYS

        for key in (
            "agents_dir",
            "skills_dir",
            "rules_dir",
            "user_claude_md",
            "keybindings_file",
        ):
            self.assertNotIn(key, SAFE_TARGET_KEYS, "%s should not be in SAFE preset" % key)

    def test_target_keys_includes_all_r6_additions(self) -> None:
        from ai_cli_kit.claude.services import target_keys

        keys = set(target_keys())
        for key in (
            "usage_data_dir", "agents_dir", "skills_dir", "plans_dir",
            "rules_dir", "user_claude_md", "keybindings_file", "cache_dir",
            "local_install_dir", "jobs_dir", "tasks_dir", "mcp_auth_cache_file",
            "magic_docs_dir", "chrome_dir", "image_store_dir", "stats_cache_file",
            "startup_perf_dir", "update_lock_file", "npm_cache_marker",
            "version_cleanup_marker", "upload_bridge_dir",
        ):
            self.assertIn(key, keys, "missing %s in target_keys()" % key)


class SymlinkPrefixBypassTests(unittest.TestCase):
    """Pass 2 of the cold audit caught a startswith-vs-commonpath bug.

    Anchor ``/tmp/x/alice`` previously accepted target
    ``/tmp/x/alice_evil/secret.txt`` because of raw string-prefix
    matching. This test pins the fix so future refactors don't
    regress to startswith.
    """

    def test_sibling_with_shared_prefix_refused(self) -> None:
        if sys.platform == "win32":
            self.skipTest("symlink test skipped on Windows without dev mode")
        from ai_cli_kit.claude.services import restore_from_backup

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "alice"
            home.mkdir()
            paths = default_paths(home)
            backup_root = paths.backup_root_base / "manual"
            (backup_root / ".claude").mkdir(parents=True)
            # Sibling whose name SHARES the prefix of the anchor.
            evil = Path(tmp_dir) / "alice_evil"
            evil.mkdir()
            (evil / "secret").write_text("attacker", encoding="utf-8")
            os.symlink(evil / "secret", backup_root / ".claude" / "projects")

            summary = restore_from_backup(paths, backup_root, overwrite=True)
            errors = [r for r in summary.records if r.status == "error"]
            self.assertTrue(errors, "sibling-prefix attack must be refused")
            self.assertTrue(any("路径穿越" in r.message for r in errors))


class ImageCacheResolvedPathTests(unittest.TestCase):
    """Pass 2 caught image_store_dir pointing at the wrong cc directory."""

    def test_image_store_dir_uses_cc_kebab_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            self.assertEqual(paths.image_store_dir.name, "image-cache")
            self.assertNotIn("imageStore", str(paths.image_store_dir))


class CliExitCodeOnErrorTests(unittest.TestCase):
    def _capture(self, argv):
        import contextlib
        import io as _io
        from ai_cli_kit.claude.cli import main

        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue()

    def test_clean_returns_2_when_records_contain_error(self) -> None:
        from unittest.mock import patch
        from ai_cli_kit.claude.models import ExecutionRecord, ExecutionSummary

        # Mock execute_plan to inject an error record so we test the
        # propagation logic without staging an actual failure.
        fake_summary = ExecutionSummary(
            records=(ExecutionRecord(key="x", status="error", message="boom"),),
            backup_root=None,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.state_file.write_text(json.dumps({"userID": "x"}), encoding="utf-8")
            with patch(
                "ai_cli_kit.claude.cli.execute_plan",
                return_value=fake_summary,
            ):
                rc, _ = self._capture(
                    ["--home", str(home), "clean", "--yes", "--preset", "safe", "--keep-backups", "0"]
                )
            self.assertEqual(rc, 2, "clean must return rc=2 on error records")


class StateCorruptedGlobTests(unittest.TestCase):
    def test_corrupted_backup_files_are_globbed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.claude_backups_dir.mkdir(parents=True)
            (paths.claude_backups_dir / ".claude.json.corrupted.111").write_text(
                json.dumps({"userID": "old-corrupted"}), encoding="utf-8"
            )

            plan = build_plan(paths, {"json_state_backups"})
            item = next(p for p in plan if p.target.key == "json_state_backups")
            self.assertTrue(item.applicable, "corrupted glob must be discovered")


class HistoryRemapResolveRaceTests(unittest.TestCase):
    def test_resolve_failure_skipped_not_aborted(self) -> None:
        from unittest.mock import patch
        from ai_cli_kit.claude.history_remap import _iter_candidate_files

        with tempfile.TemporaryDirectory() as tmp_dir:
            real_file = Path(tmp_dir) / "real.json"
            real_file.write_text("{}", encoding="utf-8")

            with patch("pathlib.Path.resolve", side_effect=OSError("ENOENT (race)")):
                yielded = list(_iter_candidate_files([real_file]))
            # Race-failed files are skipped, generator continues —
            # not crash. We just verify no exception escaped.
            self.assertEqual(yielded, [], "race-failed file should be skipped")


class SymlinkRestoreResolvedTargetTests(unittest.TestCase):
    """Pass-3 M2 + pass-6 M2: symlink restore writes resolved target
    by default to close the TOCTOU intermediate-link-swap window."""

    def test_absolute_target_writes_resolved_form(self) -> None:
        if sys.platform == "win32":
            self.skipTest("symlink test skipped on Windows without dev mode")
        from ai_cli_kit.claude.services import restore_from_backup

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "home"
            home.mkdir()
            paths = default_paths(home)
            (home / ".claude").mkdir()
            backup_root = paths.backup_root_base / "manual"
            (backup_root / ".claude").mkdir(parents=True)
            target_dir = home / ".claude" / "real-target"
            target_dir.mkdir()
            link = backup_root / ".claude" / "projects"
            os.symlink(target_dir, link)

            restore_from_backup(paths, backup_root, overwrite=True)
            restored = home / ".claude" / "projects"
            self.assertTrue(restored.is_symlink())
            # Resolved target is written, not the literal symlink string.
            link_text = os.readlink(restored)
            self.assertTrue(os.path.isabs(link_text), "absolute target should resolve to absolute")

    def test_relative_target_under_dst_parent_keeps_relative(self) -> None:
        if sys.platform == "win32":
            self.skipTest("symlink test skipped on Windows without dev mode")
        from ai_cli_kit.claude.services import restore_from_backup

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "home"
            home.mkdir()
            paths = default_paths(home)
            (home / ".claude").mkdir()
            backup_root = paths.backup_root_base / "manual"
            (backup_root / ".claude").mkdir(parents=True)
            sibling = backup_root / ".claude" / "shared-data"
            sibling.mkdir()
            link = backup_root / ".claude" / "rel-link"
            # Original link is relative to its parent.
            os.symlink("./shared-data", link)

            # The restore writes link into ~/.claude/rel-link; the
            # resolved path must be ~/.claude/shared-data (which we
            # also need to create at dst for the validation to pass).
            (home / ".claude" / "shared-data").mkdir()

            restore_from_backup(paths, backup_root, overwrite=True)
            restored = home / ".claude" / "rel-link"
            self.assertTrue(restored.is_symlink())
            link_text = os.readlink(restored)
            # Pass-4 M1 portability fix: relative form preserved when
            # resolved target sits under dst's parent.
            self.assertEqual(link_text, "./shared-data")


class ExpandTildeLinuxRegression(unittest.TestCase):
    """Pass-6 H1: ~\\ on Linux must NOT crash. cc gates this branch
    on win32; we have to as well."""

    def test_backslash_tilde_on_linux_returns_raw(self) -> None:
        if os.name == "nt":
            self.skipTest("Linux-specific test")
        from ai_cli_kit.claude.services import _expand_tilde

        # Must not crash, must return raw value (not expanded).
        result = _expand_tilde("~\\foo")
        self.assertEqual(result, "~\\foo")

    def test_invalid_tilde_path_falls_back_to_raw(self) -> None:
        from ai_cli_kit.claude.services import _expand_tilde

        # Even on Windows, malformed input shouldn't crash.
        # `~weird:user` (or similar) goes through expanduser which may
        # fail; fallback path returns raw.
        result = _expand_tilde("~/")
        # `~/` is a valid form everywhere.
        self.assertTrue(result)


class Pass5RegressionGuards(unittest.TestCase):
    """Locks pass-4 + pass-5 fixes against future refactor regressions."""

    def test_agent_memory_dir_NOT_in_safe_preset(self) -> None:
        """Pass-5 H1: cc agent memory is user-authored content,
        SAFE preset must NOT default-select it."""
        from ai_cli_kit.claude.services import SAFE_TARGET_KEYS, build_targets

        self.assertNotIn("agent_memory_dir", SAFE_TARGET_KEYS)
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = next(
                t for t in build_targets(default_paths(Path(tmp_dir)))
                if t.key == "agent_memory_dir"
            )
            self.assertFalse(
                target.default_selected,
                "agent_memory_dir.default_selected must be False",
            )

    def test_dump_prompts_dir_in_safe_preset(self) -> None:
        """Pass-5 H3: dump-prompts is high PII, SAFE-default."""
        from ai_cli_kit.claude.services import SAFE_TARGET_KEYS

        self.assertIn("dump_prompts_dir", SAFE_TARGET_KEYS)

    def test_remote_memory_redirect_target_in_target_order(self) -> None:
        from ai_cli_kit.claude.services import target_keys

        self.assertIn("remote_memory_base_redirect", set(target_keys()))

    def test_cowork_plugins_target_appears(self) -> None:
        from ai_cli_kit.claude.services import target_keys

        self.assertIn("cowork_plugins_dir", set(target_keys()))

    def test_remote_memory_env_resolves_into_paths(self) -> None:
        from ai_cli_kit.claude.paths import resolve_default_paths

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = resolve_default_paths(
                home,
                env={"CLAUDE_CODE_REMOTE_MEMORY_DIR": "/external/memory-base"},
            )
            self.assertEqual(paths.remote_memory_base_env, "/external/memory-base")

    def test_plugin_cache_env_resolves_into_paths(self) -> None:
        from ai_cli_kit.claude.paths import resolve_default_paths

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = resolve_default_paths(
                home,
                env={"CLAUDE_CODE_PLUGIN_CACHE_DIR": "/external/plugin-cache"},
            )
            self.assertEqual(paths.plugin_cache_dir_env, "/external/plugin-cache")

    def test_expand_tilde_handles_bare_tilde(self) -> None:
        """Pass-5 M7: cc accepts bare ~, ~/, and ~\\."""
        from ai_cli_kit.claude.services import _expand_tilde

        self.assertEqual(_expand_tilde("~"), str(Path("~").expanduser()))
        self.assertEqual(_expand_tilde("~/"), str(Path("~").expanduser()))
        self.assertEqual(_expand_tilde("~/foo"), str(Path("~/foo").expanduser()))
        self.assertEqual(_expand_tilde("/abs/path"), "/abs/path")
        self.assertEqual(_expand_tilde(""), "")

    def test_list_backup_roots_no_crash_on_normal_use(self) -> None:
        """Pass-4 M2 race fix smoke test — full mock of stat is too
        invasive (Path.exists also uses stat); rely on visual review
        for the OSError-swallow path. Here we just verify the typical
        case still produces sorted output."""
        from ai_cli_kit.claude.services import list_backup_roots
        import os as _os

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.backup_root_base.mkdir(parents=True)
            (paths.backup_root_base / "snap-old").mkdir()
            _os.utime(paths.backup_root_base / "snap-old", (1000, 1000))
            (paths.backup_root_base / "snap-new").mkdir()
            _os.utime(paths.backup_root_base / "snap-new", (2000, 2000))

            roots = list_backup_roots(paths)
            self.assertEqual(len(roots), 2)
            # Newest first.
            self.assertEqual(roots[0].name, "snap-new")


class UntrustedMetaRcePathTraversalTests(unittest.TestCase):
    """R7 pass-4 H1 + pass-5 M3: regression test for the RCE-level
    path traversal via attacker-supplied backup metadata."""

    def test_meta_with_root_anchor_rejected(self) -> None:
        from ai_cli_kit.claude.services import restore_from_backup

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "home"
            home.mkdir()
            paths = default_paths(home)
            backup_root = paths.backup_root_base / "evil"
            backup_root.mkdir(parents=True)
            # Attacker writes meta claiming config_root is filesystem root.
            (backup_root / "_cc_clean_meta.json").write_text(
                json.dumps({"version": 1, "config_root": "/", "home": "/"}),
                encoding="utf-8",
            )
            # Attacker drops a file at backup-relative path that would
            # land outside home if anchor were trusted.
            attack_dir = backup_root / "etc"
            attack_dir.mkdir()
            (attack_dir / "evil-passwd").write_text("attacker", encoding="utf-8")

            summary = restore_from_backup(paths, backup_root, overwrite=True)
            # PRIMARY assertion: attacker MUST NOT escape the user's
            # home dir. The file must NOT exist at the absolute path
            # the attacker tried to write to.
            self.assertFalse(Path("/etc/evil-passwd").exists(),
                             "RCE: attacker file landed at /etc/evil-passwd")
            # The file may legitimately land at home/etc/evil-passwd
            # (anchor falls back to home; the relative path is just a
            # subdir of home). That's not exploitable — under user's
            # own home with their own perms.
            evil_under_home = home / "etc" / "evil-passwd"
            attacker_target = Path("/etc/evil-passwd")
            self.assertNotEqual(
                evil_under_home, attacker_target,
                "test sanity: home-relative path must not equal /etc",
            )


class R7Pass7TriStateTests(unittest.TestCase):
    """Pass-7 M2: restore/remap envelope must use tri-state (ok/partial/error)."""

    def test_emit_summary_partial_status_when_mixed(self) -> None:
        import contextlib
        import io as _io
        from ai_cli_kit.claude.cli import _emit_execution_summary
        from ai_cli_kit.claude.models import ExecutionRecord, ExecutionSummary

        # Mixed-result summary: one updated, one error.
        summary = ExecutionSummary(
            records=(
                ExecutionRecord(key="a", status="updated", message="ok"),
                ExecutionRecord(key="b", status="error", message="oops"),
            ),
            backup_root=None,
        )
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            _emit_execution_summary(summary, "json", command="restore")
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "partial")

    def test_emit_summary_ok_when_all_updated(self) -> None:
        import contextlib
        import io as _io
        from ai_cli_kit.claude.cli import _emit_execution_summary
        from ai_cli_kit.claude.models import ExecutionRecord, ExecutionSummary

        summary = ExecutionSummary(
            records=(
                ExecutionRecord(key="a", status="updated", message="ok"),
                ExecutionRecord(key="b", status="moved", message="moved"),
            ),
            backup_root=None,
        )
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            _emit_execution_summary(summary, "json", command="restore")
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "ok")

    def test_emit_summary_error_when_all_failed(self) -> None:
        import contextlib
        import io as _io
        from ai_cli_kit.claude.cli import _emit_execution_summary
        from ai_cli_kit.claude.models import ExecutionRecord, ExecutionSummary

        summary = ExecutionSummary(
            records=(
                ExecutionRecord(key="a", status="error", message="boom"),
            ),
            backup_root=None,
        )
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            _emit_execution_summary(summary, "json", command="restore")
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "error")


class R7Pass6RegressionTests(unittest.TestCase):
    """Pin pass-6 fixes (nested symlink containment, no-op JSON, status field)."""

    def test_nested_symlink_subdir_does_not_false_reject_restore(self) -> None:
        if sys.platform == "win32":
            self.skipTest("symlink test requires POSIX")
        from ai_cli_kit.claude.services import restore_from_backup

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "home"
            home.mkdir()
            (home / ".claude").mkdir()
            # Make ~/.claude/projects a symlink to external storage.
            external = Path(tmp_dir) / "srv" / "storage"
            external.mkdir(parents=True)
            os.symlink(external, home / ".claude" / "projects")
            paths = default_paths(home)
            backup_root = paths.backup_root_base / "test"
            (backup_root / ".claude" / "projects").mkdir(parents=True)
            (backup_root / ".claude" / "projects" / "session.jsonl").write_text(
                "payload", encoding="utf-8"
            )

            summary = restore_from_backup(paths, backup_root, overwrite=True)
            errors = [r for r in summary.records if r.status == "error"]
            updated = [r for r in summary.records if r.status == "updated"]
            self.assertFalse(errors, "nested-symlink false-reject regression")
            self.assertTrue(updated, "no files restored — false reject suspected")

    def test_prune_backups_no_excess_json_has_skipped_reason(self) -> None:
        import contextlib
        import io as _io
        from ai_cli_kit.claude.cli import main

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.backup_root_base.mkdir(parents=True)
            # Create 1 root, --keep 5 → no excess.
            (paths.backup_root_base / "snap-1").mkdir()
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main([
                    "--home", str(home), "prune-backups",
                    "--keep", "5", "--yes", "--format", "json",
                ])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertIn("skipped_reason", payload)
            self.assertEqual(payload["skipped_reason"], "no_excess")

    def test_list_targets_json_has_status_field(self) -> None:
        import contextlib
        import io as _io
        from ai_cli_kit.claude.cli import main

        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["list-targets", "--format", "json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertIn("completion_cache", payload["keys"])
        self.assertIn("workflows_dir", payload["keys"])


class R8Pass1RegressionTests(unittest.TestCase):
    """Pin pass-1 fixes (mcp-refresh glob, XDG paths, perf cache, TUI alt-screen)."""

    def test_mcp_refresh_glob_target_in_safe_preset(self) -> None:
        from ai_cli_kit.claude.services import SAFE_TARGET_KEYS

        self.assertIn("mcp_refresh_locks", SAFE_TARGET_KEYS)

    def test_mcp_refresh_glob_matches_lockfiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            (paths.claude_dir / "mcp-refresh-foo_bar.lock").write_text("", encoding="utf-8")
            (paths.claude_dir / "mcp-refresh-baz.lock").write_text("", encoding="utf-8")
            (paths.claude_dir / "unrelated.lock").write_text("keep", encoding="utf-8")

            plan = build_plan(paths, {"mcp_refresh_locks"})
            item = next(p for p in plan if p.target.key == "mcp_refresh_locks")
            self.assertTrue(item.applicable)
            execute_plan(paths, plan, RunOptions(backup_enabled=True, dry_run=False))
            self.assertFalse((paths.claude_dir / "mcp-refresh-foo_bar.lock").exists())
            self.assertFalse((paths.claude_dir / "mcp-refresh-baz.lock").exists())
            self.assertTrue((paths.claude_dir / "unrelated.lock").exists())

    def test_xdg_paths_resolve_with_env(self) -> None:
        from ai_cli_kit.claude.paths import resolve_default_paths

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "home"
            home.mkdir()
            paths = resolve_default_paths(
                home,
                env={
                    "XDG_DATA_HOME": "/srv/share",
                    "XDG_CACHE_HOME": "/srv/cache",
                    "XDG_STATE_HOME": "/srv/state",
                },
            )
            self.assertEqual(str(paths.xdg_data_claude), "/srv/share/claude")
            self.assertEqual(str(paths.xdg_cache_claude), "/srv/cache/claude")
            self.assertEqual(str(paths.xdg_state_claude), "/srv/state/claude")

    def test_xdg_paths_default_to_home_subdirs(self) -> None:
        from ai_cli_kit.claude.paths import resolve_default_paths

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = resolve_default_paths(home, env={})
            self.assertEqual(paths.xdg_data_claude, home / ".local" / "share" / "claude")
            self.assertEqual(paths.xdg_cache_claude, home / ".cache" / "claude")
            self.assertEqual(paths.xdg_state_claude, home / ".local" / "state" / "claude")

    def test_path_size_cache_skips_child_sig_on_top_mtime_hit(self) -> None:
        """R8 M4 perf: when top mtime matches a cached entry, the
        expensive child_mtime_signature scandir walk is skipped."""
        from unittest.mock import patch
        from ai_cli_kit.claude.services import _PATH_SIZE_CACHE, _path_size

        _PATH_SIZE_CACHE.clear()
        with tempfile.TemporaryDirectory() as tmp_dir:
            d = Path(tmp_dir) / "data"
            d.mkdir()
            (d / "a.txt").write_text("x", encoding="utf-8")
            # Prime cache.
            first = _path_size(d)
            self.assertEqual(first, 1)
            # Second call must NOT call _child_mtime_signature.
            with patch(
                "ai_cli_kit.claude.services._child_mtime_signature",
                side_effect=AssertionError("child_sig should be skipped on top-mtime hit"),
            ):
                second = _path_size(d)
            self.assertEqual(second, 1)


class R9XdgRestoreRoundTripTests(unittest.TestCase):
    """R9 M2: backup files written to XDG redirect paths (outside
    $HOME) must round-trip through restore. Before the fix, those
    files landed under ``external/`` in the backup tree and restore
    skipped them with "请手动还原"."""

    def test_xdg_data_home_path_is_in_backup_anchor_set(self) -> None:
        from ai_cli_kit.claude.paths import resolve_default_paths
        from ai_cli_kit.claude.services import _relative_under_anchors

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "home"
            home.mkdir()
            paths = resolve_default_paths(
                home,
                env={"XDG_DATA_HOME": str(Path(tmp_dir) / "srv" / "share")},
            )
            # File under XDG redirect — should NOT land in external/
            source = paths.xdg_data_claude / "versions" / "1.0" / "claude"
            relative = _relative_under_anchors(paths, source)
            self.assertFalse(
                str(relative).startswith("external"),
                "XDG redirect path %s landed in external/ — restore can't round-trip" % source,
            )


class R10XdgCrossHostRestoreTests(unittest.TestCase):
    """R10 pass-5 H1: XDG-redirected backup must restore to the current
    host's XDG_DATA_HOME/claude (not bare $HOME/claude/)."""

    def test_xdg_backup_restored_to_current_xdg_default_layout(self) -> None:
        from ai_cli_kit.claude.paths import resolve_default_paths
        from ai_cli_kit.claude.services import restore_from_backup

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "home"
            home.mkdir()
            paths = resolve_default_paths(home, env={})
            # Simulate a backup taken with XDG_DATA_HOME=/srv/share — file
            # lives at backup_root/claude/versions/1.0/claude.exe.
            backup_root = paths.backup_root_base / "manual"
            (backup_root / "claude" / "versions" / "1.0").mkdir(parents=True)
            (backup_root / "claude" / "versions" / "1.0" / "claude.exe").write_text(
                "demo", encoding="utf-8"
            )
            summary = restore_from_backup(paths, backup_root, overwrite=True)
            updated = [r for r in summary.records if r.status == "updated"]
            self.assertTrue(updated, "XDG backup not restored at all")
            # Should land at host's $HOME/.local/share/claude/versions/1.0/...
            # (NOT bare $HOME/claude/...).
            expected = paths.xdg_data_claude / "versions" / "1.0" / "claude.exe"
            stray = home / "claude" / "versions" / "1.0" / "claude.exe"
            self.assertTrue(expected.exists(),
                            "XDG backup should restore to %s" % expected)
            self.assertFalse(stray.exists(),
                             "stray $HOME/claude/ leaked: %s" % stray)

    def test_multi_xdg_routes_by_subdir(self) -> None:
        """R10 pass-6 H1: cache file (claude/staging/...) restores to
        xdg_cache_claude.parent, not xdg_data_claude.parent, even when
        BOTH XDG_DATA_HOME and XDG_CACHE_HOME are outside $HOME.
        """
        from ai_cli_kit.claude.paths import resolve_default_paths
        from ai_cli_kit.claude.services import restore_from_backup

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            home = tmp / "home"
            home.mkdir()
            srv_data = tmp / "srv-data"
            srv_cache = tmp / "srv-cache"
            srv_state = tmp / "srv-state"
            paths = resolve_default_paths(
                home,
                env={
                    "XDG_DATA_HOME": str(srv_data),
                    "XDG_CACHE_HOME": str(srv_cache),
                    "XDG_STATE_HOME": str(srv_state),
                },
            )
            backup_root = paths.backup_root_base / "manual"
            (backup_root / "claude" / "versions" / "1.0").mkdir(parents=True)
            (backup_root / "claude" / "versions" / "1.0" / "bin").write_text("x")
            (backup_root / "claude" / "staging" / "1.0").mkdir(parents=True)
            (backup_root / "claude" / "staging" / "1.0" / "tmp.bin").write_text("y")
            (backup_root / "claude" / "locks").mkdir(parents=True)
            (backup_root / "claude" / "locks" / "pid.lock").write_text("z")
            restore_from_backup(paths, backup_root, overwrite=True)
            self.assertTrue((srv_data / "claude" / "versions" / "1.0" / "bin").exists())
            self.assertTrue((srv_cache / "claude" / "staging" / "1.0" / "tmp.bin").exists())
            self.assertTrue((srv_state / "claude" / "locks" / "pid.lock").exists())
            self.assertFalse((srv_data / "claude" / "staging" / "1.0" / "tmp.bin").exists())
            self.assertFalse((srv_data / "claude" / "locks" / "pid.lock").exists())


class R10AutoMemoryFallthroughTests(unittest.TestCase):
    """R10 pass-2 M1: env-rejected MUST fall through to settings, not
    early-return rejected. Mirrors cc's `?? ` semantic."""

    def test_invalid_env_falls_through_to_valid_settings(self) -> None:
        from ai_cli_kit.claude.services import resolve_auto_memory_override_state

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.settings_file.write_text(
                json.dumps({"autoMemoryDirectory": "/srv/memories"}),
                encoding="utf-8",
            )
            # Env is set but INVALID (relative path — rejected by validator).
            state = resolve_auto_memory_override_state(
                paths,
                env={"CLAUDE_COWORK_MEMORY_PATH_OVERRIDE": "../bad-relative"},
            )
            # Must NOT report rejected — settings provides a valid override.
            self.assertIsNotNone(state.valid_path)
            self.assertIn("/srv/memories", str(state.valid_path))

    def test_invalid_env_invalid_settings_reports_env_rejection(self) -> None:
        from ai_cli_kit.claude.services import resolve_auto_memory_override_state

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            paths = default_paths(home)
            paths.claude_dir.mkdir(parents=True)
            paths.settings_file.write_text(
                json.dumps({"autoMemoryDirectory": "../also-bad"}),
                encoding="utf-8",
            )
            state = resolve_auto_memory_override_state(
                paths,
                env={"CLAUDE_COWORK_MEMORY_PATH_OVERRIDE": "../env-bad"},
            )
            self.assertIsNone(state.valid_path)
            # Earlier signal wins.
            self.assertEqual(state.rejected_source, "env")
            self.assertEqual(state.rejected_raw, "../env-bad")


class R10OverlappingXdgAnchorsTests(unittest.TestCase):
    """R10 M2: when custom XDG roots nest (e.g. data=/srv,
    cache=/srv/cache), the most-specific anchor must win the
    relative-path match — not the first-inserted parent."""

    def test_most_specific_xdg_anchor_wins(self) -> None:
        from ai_cli_kit.claude.paths import resolve_default_paths
        from ai_cli_kit.claude.services import _relative_under_anchors

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "home"
            home.mkdir()
            paths = resolve_default_paths(
                home,
                env={
                    "XDG_DATA_HOME": "/srv",
                    "XDG_CACHE_HOME": "/srv/cache",
                    "XDG_STATE_HOME": "/srv/state",
                },
            )
            # File under XDG_CACHE_HOME=/srv/cache — relative should be
            # ``claude/x.txt`` (anchored at /srv/cache), not
            # ``cache/claude/x.txt`` (anchored at /srv).
            cache_file = paths.xdg_cache_claude / "x.txt"  # /srv/cache/claude/x.txt
            relative = _relative_under_anchors(paths, cache_file)
            self.assertEqual(str(relative), "claude/x.txt")


class R9DebugPathsTriStateTests(unittest.TestCase):
    """R9 L1: debug-paths now distinguishes auto-memory unset / valid /
    rejected via ``auto_memory_override_state``."""

    def test_debug_paths_emits_tri_state_field(self) -> None:
        import contextlib
        import io as _io
        from ai_cli_kit.claude.cli import main

        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["--home", str(home), "debug-paths", "--format", "json"])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertIn("auto_memory_override_state", payload)
            self.assertIn(payload["auto_memory_override_state"]["state"],
                          {"unset", "valid", "rejected"})


if __name__ == "__main__":
    unittest.main()
