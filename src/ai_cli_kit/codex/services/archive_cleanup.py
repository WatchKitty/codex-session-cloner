"""Archived Codex thread cleanup services."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ..models import ArchivedCleanupResult
from ..paths import CodexPaths
from ..stores.index import remove_session_index_entries
from ..stores.session_files import read_session_payload, session_id_from_filename
from ..support import atomic_write, lock_path_for, long_path


def _archived_rollout_files(paths: CodexPaths) -> list[Path]:
    if not paths.archived_sessions_dir.exists():
        return []
    try:
        return sorted(paths.archived_sessions_dir.rglob("rollout-*.jsonl"))
    except OSError:
        return []


def _active_session_ids(paths: CodexPaths) -> set[str]:
    if not paths.sessions_dir.exists():
        return set()
    session_ids: set[str] = set()
    try:
        session_files = sorted(paths.sessions_dir.rglob("rollout-*.jsonl"))
    except OSError:
        return session_ids
    for session_file in session_files:
        session_id = session_id_from_filename(session_file)
        if session_id:
            session_ids.add(session_id)
    return session_ids


def _session_id_for_archived_file(path: Path) -> str:
    session_id = session_id_from_filename(path)
    if session_id:
        return session_id
    payload = read_session_payload(path)
    value = payload.get("id")
    return value if isinstance(value, str) else ""


def _archived_thread_ids_from_db(state_db: Path | None) -> tuple[list[str], list[str]]:
    if state_db is None or not state_db.exists():
        return [], []
    warnings: list[str] = []
    try:
        with closing(sqlite3.connect(long_path(state_db), timeout=30)) as conn:
            cur = conn.cursor()
            row = cur.execute("select name from sqlite_master where type='table' and name='threads'").fetchone()
            if not row:
                return [], []
            ids = [
                value
                for (value,) in cur.execute("select id from threads where archived = 1").fetchall()
                if isinstance(value, str) and value
            ]
            return sorted(set(ids)), []
    except sqlite3.Error as exc:
        warnings.append(f"failed to inspect archived rows in {state_db}: {exc}")
    return [], warnings


def _delete_rows_by_thread_id(cur: sqlite3.Cursor, table: str, column: str, session_ids: set[str]) -> int:
    row = cur.execute("select name from sqlite_master where type='table' and name=?", (table,)).fetchone()
    if not row:
        return 0
    columns = [info[1] for info in cur.execute(f"pragma table_info({table})").fetchall()]
    if column not in columns:
        return 0
    before = cur.execute(f"select count(*) from {table} where {column} in ({','.join('?' for _ in session_ids)})", tuple(session_ids)).fetchone()[0]
    cur.execute(f"delete from {table} where {column} in ({','.join('?' for _ in session_ids)})", tuple(session_ids))
    return int(before or 0)


def _delete_archived_threads(state_db: Path | None, session_ids: set[str]) -> tuple[int, list[str]]:
    if not session_ids or state_db is None or not state_db.exists():
        return 0, []
    warnings: list[str] = []
    placeholders = ",".join("?" for _ in session_ids)
    params = tuple(session_ids)
    try:
        with closing(sqlite3.connect(long_path(state_db), timeout=30)) as conn:
            cur = conn.cursor()
            total = 0
            row = cur.execute("select name from sqlite_master where type='table' and name='threads'").fetchone()
            if row:
                before = cur.execute(f"select count(*) from threads where id in ({placeholders}) and archived = 1", params).fetchone()[0]
                cur.execute(f"delete from threads where id in ({placeholders}) and archived = 1", params)
                total += int(before or 0)

            total += _delete_rows_by_thread_id(cur, "thread_dynamic_tools", "thread_id", session_ids)
            total += _delete_rows_by_thread_id(cur, "thread_goals", "thread_id", session_ids)
            total += _delete_rows_by_thread_id(cur, "stage1_outputs", "thread_id", session_ids)
            total += _delete_rows_by_thread_id(cur, "thread_spawn_edges", "parent_thread_id", session_ids)
            total += _delete_rows_by_thread_id(cur, "thread_spawn_edges", "child_thread_id", session_ids)

            conn.commit()
            return total, []
    except sqlite3.Error as exc:
        warnings.append(f"failed to delete archived rows from {state_db}: {exc}")
    return 0, warnings


def _prune_mapping(value: object, session_ids: set[str]) -> object:
    if not isinstance(value, dict):
        return value
    return {key: item for key, item in value.items() if key not in session_ids}


def _prune_id_list(value: object, session_ids: set[str]) -> object:
    if not isinstance(value, list):
        return value
    return [item for item in value if item not in session_ids]


def _prune_global_state(state_file: Path, session_ids: set[str]) -> tuple[bool, list[str]]:
    if not session_ids or not state_file.exists():
        return False, []
    warnings: list[str] = []
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"failed to read {state_file}: {exc}"]
    if not isinstance(data, dict):
        return False, [f"failed to prune {state_file}: expected JSON object"]

    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    data["thread-workspace-root-hints"] = _prune_mapping(data.get("thread-workspace-root-hints"), session_ids)
    data["projectless-thread-ids"] = _prune_id_list(data.get("projectless-thread-ids"), session_ids)

    atom_state = data.get("electron-persisted-atom-state")
    if isinstance(atom_state, dict):
        for key in list(atom_state.keys()):
            if any(session_id in key for session_id in session_ids):
                atom_state.pop(key, None)
        atom_state["thread-workspace-root-hints"] = _prune_mapping(atom_state.get("thread-workspace-root-hints"), session_ids)
        atom_state["heartbeat-thread-permissions-by-id"] = _prune_mapping(
            atom_state.get("heartbeat-thread-permissions-by-id"),
            session_ids,
        )
        atom_state["projectless-thread-ids"] = _prune_id_list(atom_state.get("projectless-thread-ids"), session_ids)
        prompt_history = atom_state.get("prompt-history")
        if isinstance(prompt_history, dict):
            for session_id in session_ids:
                prompt_history.pop(session_id, None)
        thread_titles = atom_state.get("thread-titles")
        if isinstance(thread_titles, dict):
            thread_titles["titles"] = _prune_mapping(thread_titles.get("titles"), session_ids)

    after = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if before == after:
        return False, []

    try:
        with atomic_write(state_file, lock_path=lock_path_for(state_file)) as fh:
            fh.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    except OSError as exc:
        warnings.append(f"failed to write {state_file}: {exc}")
        return False, warnings
    return True, warnings


def _remove_empty_archived_dirs(root: Path) -> None:
    if not root.exists():
        return
    for child in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            child.rmdir()
        except OSError:
            pass


def clean_archived_sessions(paths: CodexPaths, *, dry_run: bool = False) -> ArchivedCleanupResult:
    archived_files = _archived_rollout_files(paths)
    archived_thread_ids, db_warnings = _archived_thread_ids_from_db(paths.latest_state_db())
    warnings = list(db_warnings)
    errors: list[tuple[Path, str]] = []

    file_session_ids: list[str] = []
    for session_file in archived_files:
        try:
            session_id = _session_id_for_archived_file(session_file)
        except Exception as exc:
            session_id = ""
            warnings.append(f"failed to read session id from {session_file}: {exc}")
        if session_id:
            file_session_ids.append(session_id)

    active_ids = _active_session_ids(paths)
    metadata_session_ids = (set(file_session_ids) | set(archived_thread_ids)) - active_ids
    skipped_active_ids = sorted((set(file_session_ids) | set(archived_thread_ids)) & active_ids)
    if skipped_active_ids:
        warnings.append(
            "skipped metadata cleanup for active session id(s) also present under sessions/: "
            + ", ".join(skipped_active_ids[:10])
        )
    if dry_run or (not archived_files and not metadata_session_ids):
        return ArchivedCleanupResult(
            dry_run=dry_run,
            files_checked=len(archived_files),
            archived_files=archived_files,
            archived_thread_ids=sorted(metadata_session_ids),
            warnings=warnings,
        )

    deleted_files: list[Path] = []
    deleted_lock_files: list[Path] = []
    for session_file in archived_files:
        try:
            lock_file = lock_path_for(session_file)
            session_file.unlink()
            deleted_files.append(session_file)
            if lock_file.exists():
                lock_file.unlink()
                deleted_lock_files.append(lock_file)
        except Exception as exc:
            errors.append((session_file, str(exc)))

    deleted_session_ids = sorted(metadata_session_ids)
    if deleted_session_ids:
        remove_session_index_entries(paths.index_file, set(deleted_session_ids))
    threads_deleted, delete_warnings = _delete_archived_threads(paths.latest_state_db(), set(deleted_session_ids))
    warnings.extend(delete_warnings)
    global_state_pruned, state_warnings = _prune_global_state(paths.state_file, set(deleted_session_ids))
    warnings.extend(state_warnings)
    _remove_empty_archived_dirs(paths.archived_sessions_dir)

    return ArchivedCleanupResult(
        dry_run=False,
        files_checked=len(archived_files),
        archived_files=archived_files,
        archived_thread_ids=deleted_session_ids,
        deleted_session_ids=deleted_session_ids,
        deleted_files=deleted_files,
        deleted_lock_files=deleted_lock_files,
        threads_deleted=threads_deleted,
        global_state_pruned=global_state_pruned,
        errors=errors,
        warnings=warnings,
    )
