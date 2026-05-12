"""Structured identifier remapping for Claude local history and backups."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..core.support import atomic_write
from .models import ExecutionRecord, ExecutionSummary, RunOptions
from .paths import ClaudePaths
from .services import _backup_file_copy, _ensure_backup_root as _services_ensure_backup_root

DEFAULT_CLAUDE_PROMPT = "Reply with a single word: ok"


@dataclass(frozen=True)
class IdentifierSnapshot:
    user_id: Optional[str] = None
    stable_id: Optional[str] = None
    statsig_session_id: Optional[str] = None


def remap_history_identifiers(
    paths: ClaudePaths,
    *,
    options: RunOptions,
    run_claude: bool = False,
    claude_timeout_seconds: int = 45,
    backup_root_hint: Optional[Path] = None,
) -> ExecutionSummary:
    records: List[ExecutionRecord] = []
    backup_root: Optional[Path] = None

    if run_claude:
        if options.dry_run:
            records.append(
                ExecutionRecord(
                    key="refresh_claude",
                    status="dry-run",
                    message="演练模式：会先运行 Claude 生成新的活跃标识。",
                )
            )
        else:
            _run_claude_refresh(timeout_seconds=claude_timeout_seconds)
            records.append(
                ExecutionRecord(
                    key="refresh_claude",
                    status="updated",
                    message="已运行 Claude 预热，准备读取新的活跃标识。",
                )
            )

    old_snapshot, old_source = load_old_identifier_snapshot(paths, backup_root_hint)
    current_snapshot = load_current_identifier_snapshot(paths)
    mappings = _build_identifier_mappings(old_snapshot, current_snapshot)

    if old_source is None:
        records.append(
            ExecutionRecord(
                key="remap_identifiers",
                status="skipped",
                message="没有找到旧标识备份来源；请先执行一次带备份的清理。",
            )
        )
        return ExecutionSummary(records=tuple(records), backup_root=None)

    if not mappings:
        records.append(
            ExecutionRecord(
                key="remap_identifiers",
                status="skipped",
                message="没有形成可替换映射。通常表示当前还没生成新的 userID/stableID，或旧值与新值相同。",
            )
        )
        return ExecutionSummary(records=tuple(records), backup_root=None)

    # Snapshot the candidate set BEFORE we start writing backups. ``rglob``
    # is lazy, and ``_rewrite_roots`` includes ``backup_root_base``; without
    # the snapshot, the freshly-created backup files we drop into
    # ``backup_root`` would themselves be re-discovered and re-backed-up,
    # cascading into a path that exceeds the OS NAME_MAX limit (~255 chars
    # on Linux ext4) and crashing with ``OSError: File name too long``.
    candidate_files = list(_iter_candidate_files(_rewrite_roots(paths)))
    for file_path in candidate_files:
        change_count = _inspect_rewrite_count(file_path, mappings)
        if change_count == 0:
            continue

        if options.dry_run:
            records.append(
                ExecutionRecord(
                    key=str(file_path),
                    status="dry-run",
                    message="会更新 %d 处结构化标识。" % change_count,
                )
            )
            continue

        if options.backup_enabled:
            if backup_root is None:
                backup_root = _ensure_backup_root(paths, backup_root, options)
            if backup_root is None:
                raise RuntimeError("backup root was not created")
            backup_path = _backup_file_copy(paths, backup_root, file_path)
        else:
            backup_path = None

        _rewrite_file_in_place(file_path, mappings)
        records.append(
            ExecutionRecord(
                key=str(file_path),
                status="updated",
                message="已更新 %d 处结构化标识。" % change_count,
                backup_path=str(backup_path) if backup_path is not None else None,
            )
        )

    if not any(record.status in {"updated", "dry-run"} and record.key != "refresh_claude" for record in records):
        records.append(
            ExecutionRecord(
                key="remap_identifiers",
                status="skipped",
                message="已建立映射，但在受控范围内没有发现可安全替换的结构化字段。",
            )
        )

    records.append(
        ExecutionRecord(
            key="identifier_mapping",
            status="updated" if not options.dry_run else "dry-run",
            message=_mapping_summary_message(old_snapshot, current_snapshot, old_source),
        )
    )
    return ExecutionSummary(records=tuple(records), backup_root=str(backup_root) if backup_root is not None else None)


def load_current_identifier_snapshot(paths: ClaudePaths) -> IdentifierSnapshot:
    return _load_identifier_snapshot(paths.state_file, paths.statsig_dir)


def load_old_identifier_snapshot(
    paths: ClaudePaths,
    backup_root_hint: Optional[Path] = None,
) -> Tuple[IdentifierSnapshot, Optional[Path]]:
    candidates: List[Path] = []
    if backup_root_hint is not None:
        candidates.append(Path(backup_root_hint).expanduser())

    if paths.backup_root_base.exists():
        for child in sorted(paths.backup_root_base.iterdir(), reverse=True):
            if child.is_dir():
                candidates.append(child)

    for candidate in candidates:
        snapshot = _load_identifier_snapshot(candidate / ".claude.json", candidate / ".claude" / "statsig")
        if snapshot.user_id or snapshot.stable_id or snapshot.statsig_session_id:
            return snapshot, candidate
    return IdentifierSnapshot(), None


def _run_claude_refresh(timeout_seconds: int) -> None:
    # ``shutil.which`` resolves PATHEXT-aware locations on Windows so we find
    # ``claude.cmd`` / ``claude.exe`` wrappers without scanning ourselves. On
    # POSIX it just returns the absolute path of ``claude`` from PATH.
    import os as _os
    import shutil as _shutil

    executable = _shutil.which("claude")
    if executable is None:
        raise RuntimeError(
            "未在 PATH 中找到 ``claude`` 可执行文件；请确认 Claude Code CLI 已安装并加入 PATH。"
        )

    # On Windows, npm-installed CLIs (Claude Code is one) ship as ``.cmd`` /
    # ``.bat`` shims. ``subprocess.run([path], shell=False)`` uses
    # ``CreateProcess`` directly, which CANNOT execute batch files — it would
    # raise ``OSError: [WinError 193] %1 is not a valid Win32 application``.
    # Wrap such targets with ``cmd.exe /c`` so cmd.exe interprets the batch.
    # ``.exe`` paths are executed directly (no wrapping needed).
    cmd: list = [executable, "-p", DEFAULT_CLAUDE_PROMPT]
    if _os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd.exe", "/c", executable, "-p", DEFAULT_CLAUDE_PROMPT]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        # R7 pass-5 L3: TimeoutExpired's __str__ embeds the executable
        # path. Replace with a generic message.
        raise RuntimeError(
            "Claude 调用超时（%ds）；请运行 `claude --debug` 单独排查。" % timeout_seconds
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        # R7 pass-6 M2: subprocess.run can raise these (Windows .cmd
        # shim deletion race, exec-perm dropped) with the executable
        # path in the message. Scrub.
        raise RuntimeError(
            "无法启动 Claude（%s）；请运行 `claude --version` 排查 PATH 配置。"
            % type(exc).__name__
        )
    if result.returncode != 0:
        # R7 pass-4 H3: cc binary stderr/stdout commonly embeds absolute
        # paths (config dir, MCP server paths, lockfiles). Don't proxy
        # the raw text up through our exception chain — it gets emitted
        # in JSON envelopes. Surface only the exit code.
        raise RuntimeError(
            "运行 Claude 生成新标识失败：claude 退出码 %d。请运行 `claude --debug` 单独排查。"
            % result.returncode
        )


def _load_identifier_snapshot(state_file: Path, statsig_dir: Path) -> IdentifierSnapshot:
    user_id = _load_state_user_id(state_file)
    stable_id = None
    statsig_session_id = None
    if statsig_dir.exists():
        stable_id = _load_statsig_stable_id(statsig_dir)
        statsig_session_id = _load_statsig_session_id(statsig_dir)
    return IdentifierSnapshot(
        user_id=user_id,
        stable_id=stable_id,
        statsig_session_id=statsig_session_id,
    )


def _load_state_user_id(state_file: Path) -> Optional[str]:
    payload = _load_json(state_file)
    if isinstance(payload, dict):
        value = payload.get("userID")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _safe_glob(directory: Path, pattern: str) -> List[Path]:
    """``Path.glob`` wrapper tolerant of mid-walk dir removal.

    cc rotates statsig files under ``~/.claude/statsig/``; if cc
    triggers a rotation while we're enumerating, the underlying
    ``readdir`` can raise ``OSError(ENOENT)`` partway through. Returning
    an empty list (rather than letting it escape) keeps the remap pass
    robust — the worst case is missing one mapping, not aborting the
    whole run.
    """
    try:
        return sorted(directory.glob(pattern))
    except OSError:
        return []


def _load_statsig_stable_id(statsig_dir: Path) -> Optional[str]:
    for child in _safe_glob(statsig_dir, "statsig.stable_id.*"):
        value = _load_json_scalar_string(child)
        if value:
            return value

    for child in _safe_glob(statsig_dir, "statsig.cached.evaluations.*"):
        payload = _load_json(child)
        if not isinstance(payload, dict):
            continue
        value = payload.get("stableID")
        if isinstance(value, str) and value.strip():
            return value.strip()
        inner = _load_embedded_statsig_payload(payload)
        if isinstance(inner, dict):
            value = inner.get("stableID")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _load_statsig_session_id(statsig_dir: Path) -> Optional[str]:
    for child in _safe_glob(statsig_dir, "statsig.session_id.*"):
        value = _load_json_scalar_string(child)
        if value:
            return value

    for child in _safe_glob(statsig_dir, "statsig.cached.evaluations.*"):
        payload = _load_json(child)
        if not isinstance(payload, dict):
            continue
        inner = _load_embedded_statsig_payload(payload)
        value = _extract_nested_string(inner, ("evaluated_keys", "customIDs", "sessionId"))
        if value:
            return value

    for child in _safe_glob(statsig_dir, "statsig.failed_logs.*"):
        payload = _load_json(child)
        value = _extract_nested_string(payload, ("0", "user", "customIDs", "sessionId"))
        if value:
            return value
        if isinstance(payload, list):
            for event in payload:
                value = _extract_nested_string(event, ("user", "customIDs", "sessionId"))
                if value:
                    return value
    return None


def _load_embedded_statsig_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = payload.get("data")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        decoded = json.loads(value)
    except Exception:
        return None
    return decoded if isinstance(decoded, dict) else None


def _load_json(path: Path) -> Any:
    if not path.exists() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_json_scalar_string(path: Path) -> Optional[str]:
    payload = _load_json(path)
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return None


def _extract_nested_string(payload: Any, path: Sequence[str]) -> Optional[str]:
    current = payload
    for key in path:
        if isinstance(current, list):
            try:
                current = current[int(key)]
            except Exception:
                return None
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, str) and current.strip():
        return current.strip()
    return None


def _build_identifier_mappings(old: IdentifierSnapshot, current: IdentifierSnapshot) -> Dict[str, Tuple[str, str]]:
    mappings: Dict[str, Tuple[str, str]] = {}
    if old.user_id and current.user_id and old.user_id != current.user_id:
        mappings["user_id"] = (old.user_id, current.user_id)
    if old.stable_id and current.stable_id and old.stable_id != current.stable_id:
        mappings["stable_id"] = (old.stable_id, current.stable_id)
    if (
        old.statsig_session_id
        and current.statsig_session_id
        and old.statsig_session_id != current.statsig_session_id
    ):
        mappings["statsig_session_id"] = (old.statsig_session_id, current.statsig_session_id)
    return mappings


def _rewrite_roots(paths: ClaudePaths) -> Tuple[Path, ...]:
    # NB: paths.backup_root_base is deliberately NOT here. That tree is this
    # tool's own backup store — `restore` and `load_old_identifier_snapshot`
    # read the *original* identifiers from it; rewriting it in place would make
    # restore irreversible and break a second remap (no old value left to map).
    return (
        paths.projects_dir,
        paths.sessions_dir,
        paths.history_file,
        paths.session_env_dir,
        paths.telemetry_dir,
        paths.claude_backups_dir,
    )


def _iter_candidate_files(roots: Iterable[Path]) -> Iterator[Path]:
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            # ``resolve()`` can raise OSError if the file is unlinked
            # between exists() and resolve() (race with cc rotating
            # state files). Wrap so a single missing file doesn't
            # abort the entire remap pass.
            try:
                resolved = root.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            yield root
            continue
        # rglob can raise OSError if ``root`` (or a subdir) is deleted by
        # another process mid-walk. Skip the rest of THIS root and continue
        # the outer iteration — we'd rather lose a few candidate files than
        # crash an entire history-remap run.
        try:
            for child in root.rglob("*"):
                if not child.is_file():
                    continue
                # R7 pass-2 M2: per-child resolve() can race with cc
                # rotating files. Skip the bad child instead of letting
                # OSError abort the rest of THIS root.
                try:
                    resolved = child.resolve()
                except OSError:
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield child
        except OSError:
            continue


def _inspect_rewrite_count(path: Path, mappings: Dict[str, Tuple[str, str]]) -> int:
    outcome = _transform_file(path, mappings)
    return outcome[1] if outcome is not None else 0


def _rewrite_file_in_place(path: Path, mappings: Dict[str, Tuple[str, str]]) -> None:
    outcome = _transform_file(path, mappings)
    if outcome is None:
        return
    updated_text, change_count = outcome
    if change_count <= 0:
        return
    # Atomic write so a crash mid-rewrite cannot leave history.jsonl /
    # session JSON in a half-mutated state. ``atomic_write`` opens with
    # ``newline=""`` so JSONL files keep their LF-only line endings on
    # Windows (Claude Code's own writers emit LF; mixing CRLF would break
    # byte-equality checks downstream).
    with atomic_write(path) as fh:
        fh.write(updated_text)


def _transform_file(path: Path, mappings: Dict[str, Tuple[str, str]]) -> Optional[Tuple[str, int]]:
    name = path.name
    if name == ".claude.json" or name.startswith(".claude.json.backup."):
        return _transform_json_file(path, mappings)
    if name.startswith("statsig.stable_id.") or name.startswith("statsig.session_id."):
        return _transform_json_scalar_string_file(path, mappings)
    if name.startswith("statsig."):
        return _transform_json_file(path, mappings)
    if path.suffix == ".json":
        return _transform_json_file(path, mappings)
    if path.suffix == ".jsonl":
        return _transform_jsonl_file(path, mappings)
    return None


def _transform_json_scalar_string_file(path: Path, mappings: Dict[str, Tuple[str, str]]) -> Optional[Tuple[str, int]]:
    payload = _load_json(path)
    if not isinstance(payload, str):
        return None

    change_count = 0
    if path.name.startswith("statsig.stable_id.") and "stable_id" in mappings:
        old_value, new_value = mappings["stable_id"]
        if payload == old_value:
            payload = new_value
            change_count += 1
    if path.name.startswith("statsig.session_id.") and "statsig_session_id" in mappings:
        old_value, new_value = mappings["statsig_session_id"]
        if payload == old_value:
            payload = new_value
            change_count += 1

    if change_count == 0:
        return None
    return json.dumps(payload, ensure_ascii=True) + "\n", change_count


def _transform_json_file(path: Path, mappings: Dict[str, Tuple[str, str]]) -> Optional[Tuple[str, int]]:
    payload = _load_json(path)
    if payload is None:
        return None
    updated, change_count = _rewrite_json_payload(payload, (), mappings)
    if change_count == 0:
        return None
    return json.dumps(updated, indent=2, ensure_ascii=True) + "\n", change_count


def _transform_jsonl_file(path: Path, mappings: Dict[str, Tuple[str, str]]) -> Optional[Tuple[str, int]]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None

    lines = text.splitlines()
    transformed: List[str] = []
    change_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            transformed.append(line)
            continue
        try:
            payload = json.loads(line)
        except Exception:
            transformed.append(line)
            continue
        updated, line_changes = _rewrite_json_payload(payload, (), mappings)
        change_count += line_changes
        transformed.append(json.dumps(updated, ensure_ascii=True))

    if change_count == 0:
        return None

    suffix = "\n" if text.endswith("\n") or not transformed else ""
    return "\n".join(transformed) + suffix, change_count


def _rewrite_json_payload(
    payload: Any,
    path: Tuple[str, ...],
    mappings: Dict[str, Tuple[str, str]],
) -> Tuple[Any, int]:
    if isinstance(payload, dict):
        updated: Dict[str, Any] = {}
        change_count = 0
        for key, value in payload.items():
            child_path = path + (key,)
            replacement = _replace_scalar_by_path(child_path, value, mappings)
            if replacement is not value:
                updated[key] = replacement
                change_count += 1
                continue

            if key == "data" and isinstance(value, str):
                embedded_value, embedded_changes = _rewrite_embedded_json_string(value, mappings)
                updated[key] = embedded_value
                change_count += embedded_changes
                continue

            nested, nested_changes = _rewrite_json_payload(value, child_path, mappings)
            updated[key] = nested
            change_count += nested_changes
        return updated, change_count

    if isinstance(payload, list):
        updated_items: List[Any] = []
        change_count = 0
        for index, item in enumerate(payload):
            nested, nested_changes = _rewrite_json_payload(item, path + (str(index),), mappings)
            updated_items.append(nested)
            change_count += nested_changes
        return updated_items, change_count

    return payload, 0


def _rewrite_embedded_json_string(value: str, mappings: Dict[str, Tuple[str, str]]) -> Tuple[str, int]:
    try:
        decoded = json.loads(value)
    except Exception:
        return value, 0
    updated, change_count = _rewrite_json_payload(decoded, (), mappings)
    if change_count == 0:
        return value, 0
    return json.dumps(updated, ensure_ascii=True), change_count


def _replace_scalar_by_path(
    path: Tuple[str, ...],
    value: Any,
    mappings: Dict[str, Tuple[str, str]],
) -> Any:
    if not isinstance(value, str):
        return value

    if path and path[-1] == "userID" and "user_id" in mappings:
        old_value, new_value = mappings["user_id"]
        if value == old_value:
            return new_value

    if path and path[-1] in {"stableID", "stableId", "stable_id"} and "stable_id" in mappings:
        old_value, new_value = mappings["stable_id"]
        if value == old_value:
            return new_value

    if (
        len(path) >= 2
        and path[-2:] in {("customIDs", "sessionId"), ("customIDs", "session_id")}
        and "statsig_session_id" in mappings
    ):
        old_value, new_value = mappings["statsig_session_id"]
        if value == old_value:
            return new_value

    return value


def _mapping_summary_message(old: IdentifierSnapshot, current: IdentifierSnapshot, old_source: Path) -> str:
    parts = ["旧来源：%s" % old_source]
    if old.user_id and current.user_id:
        parts.append("userID: %s -> %s" % (_short(old.user_id), _short(current.user_id)))
    if old.stable_id and current.stable_id:
        parts.append("stableID: %s -> %s" % (_short(old.stable_id), _short(current.stable_id)))
    if old.statsig_session_id and current.statsig_session_id:
        parts.append(
            "statsig session: %s -> %s"
            % (_short(old.statsig_session_id), _short(current.statsig_session_id))
        )
    return "；".join(parts)


def _short(value: str) -> str:
    if len(value) <= 14:
        return value
    return "%s...%s" % (value[:6], value[-4:])


def _ensure_backup_root(paths: ClaudePaths, current: Optional[Path], options: RunOptions) -> Optional[Path]:
    """Delegate to services._ensure_backup_root for the meta sidecar +
    microsecond+uuid suffix machinery.

    The previous local helper used second-resolution stamps and
    skipped the metadata write; two history-remap runs in the same
    wall-clock second collided into the same directory and lost the
    config_root anchor needed by ``restore``. Sharing the services
    factory removes both regressions in one move.
    """
    return _services_ensure_backup_root(paths, current, options)
