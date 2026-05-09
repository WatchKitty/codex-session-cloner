"""CLI and TUI dispatch for cc-clean."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence

from . import APP_COMMAND, APP_DISPLAY_NAME, __version__
from .history_remap import remap_history_identifiers
from .models import ExecutionSummary, PlanItem, RunOptions
from .paths import resolve_default_paths
from .services import (
    build_plan,
    execute_plan,
    format_bytes,
    list_backup_roots,
    prune_backup_roots,
    resolve_selection,
    restore_from_backup,
    target_keys,
)
from .tui import run_tui
from .tui.terminal import configure_text_streams, is_interactive_terminal


def create_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_COMMAND,
        description="%s：用于清理 Claude 本地数据的备份安全工具。" % APP_DISPLAY_NAME,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_home_arg(parser)

    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list-targets", help="显示支持的清理目标键名。")
    _add_format_arg(list_parser)
    list_parser.set_defaults(command="list-targets")

    plan_parser = subparsers.add_parser("plan", help="预览清理计划。")
    _add_selection_args(plan_parser)
    _add_format_arg(plan_parser)

    clean_parser = subparsers.add_parser("clean", help="执行清理操作。")
    _add_selection_args(clean_parser)
    _add_format_arg(clean_parser)
    clean_parser.add_argument("--yes", action="store_true", help="跳过确认提示。")
    clean_parser.add_argument(
        "--keep-backups",
        type=int,
        default=5,
        help="清理结束后只保留最新 N 个备份目录（<=0 时禁用自动 prune；默认 5）。",
    )

    remap_parser = subparsers.add_parser("remap-history", help="用当前新标识回写旧的结构化本地记录。")
    _add_format_arg(remap_parser)
    remap_parser.add_argument("--yes", action="store_true", help="跳过确认提示。")
    remap_parser.add_argument("--dry-run", action="store_true", help="仅预览变更，不写入磁盘。")
    remap_parser.add_argument("--no-backup", action="store_true", help="直接覆盖，不创建备份。")
    remap_parser.add_argument(
        "--run-claude",
        action="store_true",
        help="执行前先运行一次 Claude，以便生成新的活跃 userID / stableID。",
    )
    remap_parser.add_argument(
        "--claude-timeout",
        type=int,
        default=45,
        help="运行 Claude 预热时的超时秒数。",
    )
    remap_parser.add_argument(
        "--backup-root",
        default="",
        help="手动指定旧标识来源备份目录；默认自动选择最新的 cc-clean 备份。",
    )

    restore_parser = subparsers.add_parser(
        "restore",
        help="从备份目录还原 Claude 本地数据；不带参数时列出可用的备份目录。",
    )
    _add_format_arg(restore_parser)
    restore_parser.add_argument(
        "backup_root",
        nargs="?",
        default="",
        help="要还原的备份目录路径（默认列出可用备份）。",
    )
    restore_parser.add_argument("--yes", action="store_true", help="跳过确认提示。")
    restore_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览将要还原的文件，不实际写入。",
    )
    restore_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="目标文件已存在时也覆盖（默认跳过已存在文件）。",
    )

    prune_parser = subparsers.add_parser("prune-backups", help="按保留数量清理旧的备份目录。")
    _add_format_arg(prune_parser)
    prune_parser.add_argument(
        "--keep",
        type=int,
        default=5,
        help="保留最新 N 个备份目录（默认 5）。",
    )
    prune_parser.add_argument("--yes", action="store_true", help="跳过确认提示。")
    prune_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览将删除的备份目录，不实际执行。",
    )

    debug_parser = subparsers.add_parser(
        "debug-paths",
        help="打印解析后的 ClaudePaths 字段、env 状态和 auto-memory 重定向（用于排错）。",
    )
    _add_format_arg(debug_parser)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_text_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv and is_interactive_terminal():
        return run_tui(resolve_default_paths())

    parser = create_arg_parser()
    args = parser.parse_args(argv)
    # ``resolve_default_paths`` honours ``CLAUDE_CONFIG_DIR`` so CLI users
    # whose cc data lives outside ``$HOME`` get the correct config root
    # while we still anchor the backup tree at their real ``$HOME``.
    paths = resolve_default_paths(Path(args.home))
    output_format = getattr(args, "format", "text") or "text"

    if args.command == "list-targets":
        # R7 pass-5 L1: honor --format=json so automation gets parseable
        # output instead of newline-separated strings.
        # R7 pass-6 M1: include status field for envelope consistency.
        if output_format == "json":
            print(json.dumps(
                {"command": "list-targets", "status": "ok", "keys": list(target_keys())},
                ensure_ascii=False, indent=2))
        else:
            for key in target_keys():
                print(key)
        return 0

    if args.command == "remap-history":
        backup_root = Path(args.backup_root).expanduser() if args.backup_root else None
        # R7 pass-3 H1: require --yes for destructive runs even in JSON
        # mode. Otherwise automation scripts that pipe --format=json
        # would silently rewrite history with zero consent.
        if not args.yes and not bool(args.dry_run):
            if output_format == "json":
                print(json.dumps(
                    {"command": "remap-history", "status": "error",
                     "error": "--yes required for non-dry-run mode (refusing silent destructive run)"},
                    ensure_ascii=False, indent=2))
                return 2
            if not _confirm_cli("确认继续执行历史标识回写？[y/N] "):
                print("已取消。")
                return 1
        # R7 pass-3 M1: ``remap_history_identifiers`` (via _run_claude_refresh)
        # may raise RuntimeError on missing claude binary / timeout.
        # Wrap so JSON consumers get a structured error envelope.
        try:
            summary = remap_history_identifiers(
                paths,
                options=RunOptions(
                    backup_enabled=(not args.no_backup),
                    dry_run=bool(args.dry_run),
                ),
                run_claude=bool(args.run_claude),
                claude_timeout_seconds=int(args.claude_timeout),
                backup_root_hint=backup_root,
            )
        except RuntimeError as exc:
            if output_format == "json":
                print(json.dumps(
                    {"command": "remap-history", "status": "error", "error": str(exc)},
                    ensure_ascii=False, indent=2))
            else:
                print("执行失败：%s" % exc, file=sys.stderr)
            return 2
        _emit_execution_summary(summary, output_format, command=args.command)
        return 2 if _summary_has_errors(summary) else 0

    if args.command in {"plan", "clean"}:
        # Pass-7 H2: ``resolve_selection`` raises ``ValueError`` for
        # unknown keys / invalid preset. Without this wrap, --format=json
        # consumers see a Python traceback on stderr and rc=2 from
        # SystemExit, with no parseable JSON body. Convert to a clean
        # error record so JSON / shell consumers get a structured signal.
        try:
            selected = resolve_selection(
                preset=args.preset,
                include_keys=args.select,
                exclude_keys=args.exclude,
            )
        except ValueError as exc:
            # R7 pass-2 H2/M4: unify command field + error envelope
            # shape with success path. Use status field to dispatch.
            if output_format == "json":
                print(
                    json.dumps(
                        {"command": args.command, "status": "error", "error": str(exc)},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print("参数错误：%s" % exc, file=sys.stderr)
            return 2
        # R7 pass-2 M3: surface conflict in BOTH text (stderr) and JSON
        # so automation gets the signal too.
        conflicts = sorted(set(args.select) & set(args.exclude))
        if conflicts and output_format == "text":
            print(
                "提示：以下键同时出现在 --select 和 --exclude，已按 exclude 处理：%s"
                % ", ".join(conflicts),
                file=sys.stderr,
            )
        plan = build_plan(paths, selected)

        # plan-only command — single-document output is what consumers
        # already expect, keep the existing _emit_plan path.
        if args.command == "plan":
            _emit_plan(plan, backup_enabled=(not args.no_backup), dry_run=bool(args.dry_run), output_format=output_format)
            return 0

        # R7 pass-3 H1: require explicit --yes for destructive runs in
        # JSON mode too. Auto-confirming on --format=json was a real
        # regression — automation scripts could wipe user data with
        # zero consent. Dry-run is always allowed (no destructive op).
        if not args.yes and not bool(args.dry_run):
            if output_format == "json":
                print(json.dumps(
                    {"command": "clean", "status": "error",
                     "error": "--yes required for non-dry-run mode (refusing silent destructive run)"},
                    ensure_ascii=False, indent=2))
                return 2
            # Text mode: show the plan first, then ask for confirmation.
            _emit_plan(plan, backup_enabled=(not args.no_backup), dry_run=bool(args.dry_run), output_format=output_format)
            if not _confirm_cli():
                print("已取消。")
                return 1
        elif output_format == "text":
            # --yes or --dry-run path — still show the plan for user
            # awareness (matches old behaviour).
            _emit_plan(plan, backup_enabled=(not args.no_backup), dry_run=bool(args.dry_run), output_format=output_format)

        summary = execute_plan(
            paths,
            plan,
            RunOptions(
                backup_enabled=(not args.no_backup),
                dry_run=bool(args.dry_run),
            ),
        )

        keep_backups = int(getattr(args, "keep_backups", 5) or 0)
        prune_failed = False
        prune_outcome = None
        if keep_backups > 0 and not bool(args.dry_run):
            outcome = prune_backup_roots(paths, keep_last=keep_backups)
            prune_outcome = outcome
            if (outcome.removed or outcome.failed) and output_format == "text":
                print("")
                if outcome.removed:
                    print("已清理 %d 个旧备份目录，保留最新 %d 个。" % (len(outcome.removed), keep_backups))
                for failed_path, err in outcome.failed:
                    print("  ⚠ 部分删除失败：%s（%s）— 请手动检查后清理。" % (failed_path, err))
            if outcome.failed:
                prune_failed = True

        if output_format == "text":
            _emit_execution_summary(summary, output_format)
        else:
            # R7 pass-2 H1: ``clean --format=json`` MUST emit a single
            # parseable JSON document. Combine plan + execution + prune
            # + warnings into one envelope so ``jq``/``json.loads`` work.
            # R7 pass-3 L2: tri-state ``status`` distinguishes total
            # success (``ok``), total failure (``error``), and mixed
            # results (``partial``) so automation can differentiate.
            has_err = _summary_has_errors(summary) or prune_failed
            has_ok = any(
                r.status not in {"error", "skipped", "dry-run"} for r in summary.records
            )
            if has_err and has_ok:
                envelope_status = "partial"
            elif has_err:
                envelope_status = "error"
            else:
                envelope_status = "ok"
            # R7 pass-3 M2: distinguish prune-disabled vs prune-skipped
            # vs prune-actual so JSON consumers / audit logs can tell.
            if prune_outcome is not None:
                prune_field = {
                    "removed": [str(p) for p in prune_outcome.removed],
                    "failed": [{"path": str(p), "error": err} for p, err in prune_outcome.failed],
                }
            elif bool(args.dry_run):
                prune_field = {"removed": [], "failed": [], "skipped_reason": "dry_run"}
            elif keep_backups <= 0:
                prune_field = {"removed": [], "failed": [], "skipped_reason": "disabled"}
            else:
                prune_field = None
            envelope = {
                "command": "clean",
                "status": envelope_status,
                "warnings": (
                    [{"type": "select_exclude_conflict", "keys": conflicts}]
                    if conflicts
                    else []
                ),
                "plan": {
                    "backup_enabled": not bool(args.no_backup),
                    "dry_run": bool(args.dry_run),
                    "items": [_plan_item_to_dict(item) for item in plan],
                },
                "execution": _execution_summary_to_dict(summary),
                "prune": prune_field,
            }
            print(json.dumps(envelope, ensure_ascii=False, indent=2))

        if _summary_has_errors(summary) or prune_failed:
            return 2
        return 0

    if args.command == "restore":
        if not args.backup_root:
            roots = list_backup_roots(paths)
            _emit_backup_list(roots, output_format)
            # Empty list signals "nothing to restore" — return 1 so
            # JSON consumers / scripts can branch on that without
            # parsing the payload.
            return 0 if roots else 1
        # R7 pass-3 H1: same JSON-mode --yes guard as clean / remap.
        if not args.yes and not bool(args.dry_run):
            if output_format == "json":
                print(json.dumps(
                    {"command": "restore", "status": "error",
                     "error": "--yes required for non-dry-run mode (refusing silent destructive run)"},
                    ensure_ascii=False, indent=2))
                return 2
            if not _confirm_cli("确认从备份还原？[y/N] "):
                print("已取消。")
                return 1
        summary = restore_from_backup(
            paths,
            Path(args.backup_root).expanduser(),
            dry_run=bool(args.dry_run),
            overwrite=bool(args.overwrite),
        )
        _emit_execution_summary(summary, output_format, command=args.command)
        return 2 if _summary_has_errors(summary) else 0

    if args.command == "debug-paths":
        return _run_debug_paths(paths, output_format)

    if args.command == "prune-backups":
        keep = int(args.keep)
        before = list_backup_roots(paths)
        # R7 pass-4 H2: prune-backups is destructive (rmtree) — apply
        # the same --yes / --dry-run guards as clean/restore.
        # R7 pass-5 M2: in text mode, show what WILL be removed BEFORE
        # asking for confirmation (matches clean's "plan-then-confirm"
        # UX pattern). dry_run preview is the same listing.
        if not args.yes and not bool(args.dry_run):
            if output_format == "json":
                print(json.dumps(
                    {"command": "prune-backups", "status": "error",
                     "error": "--yes required for non-dry-run mode (refusing silent destructive run)"},
                    ensure_ascii=False, indent=2))
                return 2
            would_remove_preview = list(before[keep:]) if keep > 0 and len(before) > keep else []
            if not would_remove_preview:
                # R7 pass-6 H2: no-op MUST honor --format=json so
                # automation gets parseable output.
                if output_format == "json":
                    print(json.dumps(
                        {"command": "prune-backups", "status": "ok",
                         "keep": keep,
                         "before": [str(p) for p in before],
                         "removed": [], "failed": [],
                         "skipped_reason": "no_excess"},
                        ensure_ascii=False, indent=2))
                else:
                    print("没有需要清理的旧备份。保留参数：%d，现存：%d。" % (keep, len(before)))
                return 0
            print("将删除以下 %d 个旧备份目录（保留最新 %d 个）：" % (len(would_remove_preview), keep))
            for p in would_remove_preview:
                print("  - %s" % p)
            if not _confirm_cli("确认删除以上备份目录？[y/N] "):
                print("已取消。")
                return 1
        if bool(args.dry_run):
            # Compute what WOULD be removed without touching disk.
            would_remove = list(before[keep:]) if keep > 0 and len(before) > keep else []
            if output_format == "json":
                print(json.dumps(
                    {"command": "prune-backups", "status": "ok", "dry_run": True,
                     "keep": keep,
                     "before": [str(p) for p in before],
                     "would_remove": [str(p) for p in would_remove]},
                    ensure_ascii=False, indent=2))
            else:
                print("演练模式（dry-run）— 不会实际删除")
                print("保留参数：%d" % keep)
                print("现存备份：%d" % len(before))
                for path in would_remove:
                    print("将删除：%s" % path)
                if not would_remove:
                    print("没有需要清理的旧备份。")
            return 0
        outcome = prune_backup_roots(paths, keep_last=keep)
        # Pass-3 audit: rc=2 when any deletion failed mid-tree, mirroring
        # clean / restore / remap-history. Empty `failed` list => rc=0.
        prune_rc = 2 if outcome.failed else 0
        if output_format == "json":
            payload: Dict[str, object] = {
                "command": "prune-backups",
                "status": "error" if outcome.failed else "ok",
                "keep": keep,
                "before": [str(p) for p in before],
                "removed": [str(p) for p in outcome.removed],
                "failed": [{"path": str(p), "error": err} for p, err in outcome.failed],
            }
            # R7 pass-7 M1: emit skipped_reason for symmetry with the
            # no-op path (no-`--yes` already does this). Lets JSON
            # consumers branch consistently regardless of which path
            # produced the empty outcome.
            if not outcome.removed and not outcome.failed and len(before) <= keep:
                payload["skipped_reason"] = "no_excess"
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("保留参数：%d" % keep)
            print("现存备份：%d" % len(before))
            for path in outcome.removed:
                print("已删除：%s" % path)
            for failed_path, err in outcome.failed:
                print("  ⚠ 部分删除失败：%s（%s）" % (failed_path, err))
            if not outcome.removed and not outcome.failed:
                print("没有需要清理的旧备份。")
        return prune_rc

    parser.print_help()
    return 0


def _add_selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--preset",
        choices=("safe", "full", "none"),
        default="safe",
        help="初始目标预设。",
    )
    parser.add_argument(
        "--select",
        action="append",
        default=[],
        help="向选择集追加目标键名，可重复传入。",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="从选择集中移除目标键名，可重复传入。",
    )
    parser.add_argument("--dry-run", action="store_true", help="仅预览变更，不写入磁盘。")
    parser.add_argument("--no-backup", action="store_true", help="直接删除，不创建备份。")


def _add_home_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--home",
        default=str(Path.home()),
        help="覆盖要检查的主目录路径，默认使用当前用户主目录。",
    )


def _add_format_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="输出格式：text（默认，人类可读）或 json（机器可读）。",
    )


def _plan_item_to_dict(item: PlanItem) -> dict:
    target = item.target
    return {
        "key": target.key,
        "label": target.label,
        "description": target.description,
        "action": target.action,
        "target_path": target.target_path,
        "json_fields": list(target.json_fields),
        "env_keys": list(target.env_keys),
        "glob_patterns": list(target.glob_patterns),
        "danger": bool(target.danger),
        "deep_scrub": bool(target.deep_scrub),
        "may_remove_sessions": bool(target.may_remove_sessions),
        "selected": bool(item.selected),
        "exists": bool(item.exists),
        "applicable": bool(item.applicable),
        "size_bytes": int(item.size_bytes),
        "details": item.details,
        "warnings": list(item.warnings),
    }


def _emit_plan(
    plan: Iterable[PlanItem],
    *,
    backup_enabled: bool,
    dry_run: bool,
    output_format: str,
) -> None:
    plan_list = list(plan)
    if output_format == "json":
        # R7 pass-9 H2: include status field for envelope consistency
        # with all other JSON envelopes. Plan has no execution outcome;
        # "ok" reflects "successfully built the plan".
        payload = {
            "command": "plan",
            "status": "ok",
            "backup_enabled": bool(backup_enabled),
            "dry_run": bool(dry_run),
            "items": [_plan_item_to_dict(item) for item in plan_list],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print("%s 计划" % APP_DISPLAY_NAME)
    print("=" * 72)
    print("备份：%s" % ("开启" if backup_enabled else "关闭"))
    print("演练模式：%s" % ("开启" if dry_run else "关闭"))
    print("")
    for item in plan_list:
        marker = "[x]" if item.selected else "[ ]"
        status = "可执行" if item.applicable else ("存在" if item.exists else "缺失")
        risk = "危险" if item.target.danger else "安全"
        print(
            "%s %-24s %-7s %-7s %8s"
            % (marker, item.target.key, status, risk, format_bytes(item.size_bytes))
        )
        print("    %s" % item.target.label)
        print("    %s" % item.details)
        for warning in item.warnings:
            print("    警告：%s" % warning)
    print("")


def _execution_summary_to_dict(summary: ExecutionSummary) -> dict:
    return {
        "backup_root": summary.backup_root,
        "records": [
            {
                "key": record.key,
                "status": record.status,
                "message": record.message,
                "backup_path": record.backup_path,
            }
            for record in summary.records
        ],
    }


def _summary_has_errors(summary: ExecutionSummary) -> bool:
    """Whether ``summary.records`` contains at least one ``status=error``.

    JSON consumers and shell scripts need a non-zero exit code on any
    error inside an otherwise-completing run; this helper centralises
    the decision so ``clean`` / ``remap-history`` / ``restore`` agree.
    """
    return any(record.status == "error" for record in summary.records)


def _emit_execution_summary(summary: ExecutionSummary, output_format: str, *, command: str = "execute") -> None:
    """Emit ExecutionSummary as text or JSON.

    R7 pass-6 M1: ``command`` parameter so success path uses the actual
    subcommand name (matching the error path's envelope shape). Default
    keeps backwards-compat with callers that pass no kwarg.
    """
    if output_format == "json":
        # R7 pass-7 M2: tri-state status matches the clean envelope.
        # Partial = some records succeeded + some failed.
        has_err = any(r.status == "error" for r in summary.records)
        has_ok = any(
            r.status not in {"error", "skipped", "dry-run"} for r in summary.records
        )
        if has_err and has_ok:
            envelope_status = "partial"
        elif has_err:
            envelope_status = "error"
        else:
            envelope_status = "ok"
        payload = {
            "command": command,
            "status": envelope_status,
            **_execution_summary_to_dict(summary),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print("")
    if summary.backup_root:
        print("备份目录：%s" % summary.backup_root)
    for record in summary.records:
        print("[%s] %s - %s" % (record.status, record.key, record.message))
        if record.backup_path:
            print("  备份：%s" % record.backup_path)


def _emit_backup_list(roots: Sequence[Path], output_format: str) -> None:
    if output_format == "json":
        # R7 pass-6 M1: include status field for envelope consistency.
        # R7 pass-8 M1: empty backup list is INFORMATIONAL not an
        # error. Use ``empty`` so consumers don't conflate it with
        # actual error states (rc=1 still signals "nothing to do").
        payload = {
            "command": "restore-list",
            "status": "ok" if roots else "empty",
            "roots": [str(p) for p in roots],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not roots:
        print("没有可用的备份目录。")
        return
    print("可用的备份目录（按时间倒序）：")
    for path in roots:
        print("  %s" % path)


def _run_debug_paths(paths, output_format: str) -> int:
    """Diagnostic command: dump resolved paths + env + override state.

    Lets users verify exactly which directories cc-clean will touch
    BEFORE running cleanup. Especially useful with ``CLAUDE_CONFIG_DIR``
    where the legacy single-anchor mental model breaks down.
    """
    from .paths import CLAUDE_CONFIG_DIR_ENV
    from .services import _AUTO_MEMORY_ENV_VAR, resolve_auto_memory_override

    auto_mem = resolve_auto_memory_override(paths)
    payload = {
        "command": "debug-paths",
        "status": "ok",
        "paths": {
            "home": str(paths.home),
            "config_root": str(paths.config_root),
            "claude_dir": str(paths.claude_dir),
            "state_file": str(paths.state_file),
            "legacy_state_file": str(paths.legacy_state_file),
            "settings_file": str(paths.settings_file),
            "credentials_file": str(paths.credentials_file),
            "telemetry_dir": str(paths.telemetry_dir),
            "statsig_dir": str(paths.statsig_dir),
            "projects_dir": str(paths.projects_dir),
            "history_file": str(paths.history_file),
            "sessions_dir": str(paths.sessions_dir),
            "session_env_dir": str(paths.session_env_dir),
            "shell_snapshots_dir": str(paths.shell_snapshots_dir),
            "ide_dir": str(paths.ide_dir),
            "teams_dir": str(paths.teams_dir),
            "paste_cache_dir": str(paths.paste_cache_dir),
            "plugins_dir": str(paths.plugins_dir),
            "debug_dir": str(paths.debug_dir),
            "usage_data_dir": str(paths.usage_data_dir),
            "agents_dir": str(paths.agents_dir),
            "skills_dir": str(paths.skills_dir),
            "plans_dir": str(paths.plans_dir),
            "rules_dir": str(paths.rules_dir),
            "user_claude_md": str(paths.user_claude_md),
            "keybindings_file": str(paths.keybindings_file),
            "cache_dir": str(paths.cache_dir),
            "local_install_dir": str(paths.local_install_dir),
            "jobs_dir": str(paths.jobs_dir),
            "tasks_dir": str(paths.tasks_dir),
            "mcp_auth_cache_file": str(paths.mcp_auth_cache_file),
            "magic_docs_dir": str(paths.magic_docs_dir),
            "chrome_dir": str(paths.chrome_dir),
            "image_store_dir": str(paths.image_store_dir),
            "stats_cache_file": str(paths.stats_cache_file),
            "startup_perf_dir": str(paths.startup_perf_dir),
            "update_lock_file": str(paths.update_lock_file),
            "npm_cache_marker": str(paths.npm_cache_marker),
            "version_cleanup_marker": str(paths.version_cleanup_marker),
            "upload_bridge_dir": str(paths.upload_bridge_dir),
            "policy_limits_file": str(paths.policy_limits_file),
            "remote_settings_file": str(paths.remote_settings_file),
            "computer_use_lock_file": str(paths.computer_use_lock_file),
            "traces_dir": str(paths.traces_dir),
            "file_history_dir": str(paths.file_history_dir),
            "session_memory_dir": str(paths.session_memory_dir),
            "deep_link_failure_marker": str(paths.deep_link_failure_marker),
            "user_commands_dir": str(paths.user_commands_dir),
            "workflows_dir": str(paths.workflows_dir),
            "output_styles_dir": str(paths.output_styles_dir),
            "agent_memory_dir": str(paths.agent_memory_dir),
            "dump_prompts_dir": str(paths.dump_prompts_dir),
            "cowork_plugins_dir": str(paths.cowork_plugins_dir),
            "claude_backups_dir": str(paths.claude_backups_dir),
            "backup_root_base": str(paths.backup_root_base),
            "xdg_data_claude": str(paths.xdg_data_claude),
            "xdg_cache_claude": str(paths.xdg_cache_claude),
            "xdg_state_claude": str(paths.xdg_state_claude),
        },
        "globs": {
            "state_backup": paths.state_backup_glob,
            "state_corrupted": paths.state_corrupted_glob,
            "completion": paths.completion_glob,
            "mcp_refresh": paths.mcp_refresh_glob,
        },
        "env": {
            # All path-affecting env vars cc-clean knows about. Unset
            # AND empty-string values surface as JSON null. NOTE: cc's
            # JS ``??`` operator only catches null/undefined, NOT
            # empty strings — so cc with ``CLAUDE_CONFIG_DIR=""`` gets
            # the literal empty string. We diverge: empty == unset
            # for safer defaults. R8 pass-2 doc-fix.
            CLAUDE_CONFIG_DIR_ENV: os.environ.get(CLAUDE_CONFIG_DIR_ENV) or None,
            _AUTO_MEMORY_ENV_VAR: os.environ.get(_AUTO_MEMORY_ENV_VAR) or None,
            "CLAUDE_CODE_PLUGIN_CACHE_DIR": os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR") or None,
            "CLAUDE_CODE_REMOTE_MEMORY_DIR": os.environ.get("CLAUDE_CODE_REMOTE_MEMORY_DIR") or None,
            "CLAUDE_CODE_TMPDIR": os.environ.get("CLAUDE_CODE_TMPDIR") or None,
            "CLAUDE_CODE_USE_COWORK_PLUGINS": os.environ.get("CLAUDE_CODE_USE_COWORK_PLUGINS") or None,
        },
        "resolved_auto_memory_override": str(auto_mem) if auto_mem else None,
    }
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("== %s 路径诊断 ==" % APP_DISPLAY_NAME)
    print("")
    print("[paths]")
    for key, value in payload["paths"].items():
        print("  %-22s %s" % (key, value))
    print("")
    print("[globs]")
    for key, value in payload["globs"].items():
        print("  %-22s %s" % (key, value))
    print("")
    print("[env]")
    for key, value in payload["env"].items():
        print("  %-22s %s" % (key, value if value is not None else "<unset>"))
    print("")
    print("[auto-memory override]")
    print("  %s" % (payload["resolved_auto_memory_override"] or "<未配置；使用默认 projects_dir>"))
    return 0


def _confirm_cli(prompt: str = "确认继续执行清理？[y/N] ") -> bool:
    try:
        response = input(prompt)
    except EOFError:
        return False
    return response.strip().lower() in {"y", "yes"}
