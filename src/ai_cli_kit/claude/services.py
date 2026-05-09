"""Planning and execution helpers for local Claude cleanup."""

from __future__ import annotations

import errno
import json
import os
import shutil
import threading
import time
import unicodedata
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from ..core.support import atomic_write, file_lock, long_path, safe_copy2
from .models import CleanupTarget, ExecutionRecord, ExecutionSummary, PlanItem, RunOptions
from .paths import ClaudePaths


# Windows error code for sharing violations (ERROR_SHARING_VIOLATION = 32).
# AV / indexer / IDE file watcher transient holds surface as plain OSError
# rather than PermissionError; we still want to retry those.
_WIN_SHARING_VIOLATION = 32

# errno values for transient remove/move failures we should retry.
# EBUSY: path is mountpoint or held by another process.
# EACCES / EPERM: permission race (AV scanner briefly holds file).
# ENOTEMPTY: directory has freshly-created children (e.g. cc rewriting a
#   session file while we rmtree the session dir). Retry lets the writer
#   finish and the second pass succeeds.
_TRANSIENT_REMOVE_ERRNOS = {errno.EBUSY, errno.EACCES, errno.EPERM, errno.ENOTEMPTY}


def _is_transient_remove_error(exc: BaseException) -> bool:
    """True if ``exc`` is a recoverable transient remove/move failure."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        if exc.errno in _TRANSIENT_REMOVE_ERRNOS:
            return True
        # Windows surfaces sharing violations as OSError(winerror=32). The
        # plain ``errno`` field can be ENOENT or EACCES depending on Python
        # version, so check ``winerror`` explicitly when available.
        if getattr(exc, "winerror", None) == _WIN_SHARING_VIOLATION:
            return True
    return False


def _remove_with_retry(path: Path) -> None:
    """``shutil.rmtree``/``Path.unlink`` with bounded retry across platforms.

    Windows AV scanners, IDE watchers, and indexers briefly hold files open
    after we touch them — those surface as transient ``PermissionError`` or
    ``OSError(WinError 32)``. POSIX has its own transient cases (ENOTEMPTY
    when a writer races us, EBUSY for tmpfs mountpoints), so we retry on
    both platforms now. Genuine ownership errors still surface after the
    retry budget is exhausted.

    Symlinks are removed via plain ``os.unlink`` — never ``shutil.rmtree``,
    which raises on symlinks and would also recurse into the target if we
    naively dereferenced. The link itself is what cleanup wants to drop;
    the target (which may live on a different filesystem entirely) stays.
    """
    if path.is_symlink():
        remover = lambda p=path: os.unlink(long_path(p))
    elif path.is_dir():
        remover = lambda p=path: shutil.rmtree(long_path(p))
    else:
        remover = lambda p=path: os.unlink(long_path(p))

    last_exc: Optional[BaseException] = None
    base_delay = 0.02
    for attempt in range(5):
        try:
            remover()
            return
        except (PermissionError, OSError) as exc:
            if not _is_transient_remove_error(exc):
                raise
            last_exc = exc
            time.sleep(base_delay * (2 ** attempt))
    if last_exc is not None:
        raise last_exc


def _move_with_retry(src: Path, dst: Path) -> None:
    """``shutil.move`` honouring Windows long paths + transient lock retry."""
    src_str = long_path(src)
    dst_str = long_path(dst)

    last_exc: Optional[BaseException] = None
    base_delay = 0.02
    for attempt in range(5):
        try:
            shutil.move(src_str, dst_str)
            return
        except (PermissionError, OSError) as exc:
            if not _is_transient_remove_error(exc):
                raise
            last_exc = exc
            time.sleep(base_delay * (2 ** attempt))
    if last_exc is not None:
        raise last_exc


def _same_filesystem(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` share a device id.

    ``shutil.move`` falls back to copy-then-unlink across filesystems, but
    that fallback is NOT atomic: a SIGKILL between the copy and unlink
    leaves both locations with content. We detect cross-fs up front and
    take the explicit copy-then-verify path so the failure mode is the
    user noticing a duplicated backup, not a half-deleted source.

    NIT #12: when stat fails (OSError), we fall back to ``True`` —
    meaning the caller takes the FAST shutil.move path. That's NOT the
    safer choice (less verification), but it's what the existing call
    sites expect: if stat fails, the source is probably gone or
    unreadable and the move will fail anyway. Returning False would
    force an unnecessary copytree attempt that also fails. Either way
    the operation surfaces the underlying error; the True path is
    chosen for consistency with the same-fs hot path.
    """
    try:
        return os.stat(a, follow_symlinks=False).st_dev == os.stat(b, follow_symlinks=False).st_dev
    except OSError:
        return True

AUTH_ENV_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")

# Identity / fingerprint fields cc writes into ~/.claude.json.
#
# ``state_user_id`` keeps the original minimal scrub (userID only) for users
# who ran cc-clean before and want a stable diff. ``state_full_identity``
# extends it across every PII / fingerprint field cc actually persists, with
# ``deep_scrub`` enabled so nested copies (``projects.<cwd>.userID``,
# ``mcpServers.<*>.apiKey``, ...) are also removed. Both targets share the
# same physical file (~/.claude.json) and are mutually compatible: the deep
# scrub strictly supersets the userID-only scrub.
STATE_PII_FIELDS = (
    "userID",
    "oauthAccount",
    "primaryApiKey",
    "primaryEmail",
    "organizationUuid",
    "organizationUuids",
    "customApiKeyResponses",
    "firstStartTime",
    "numStartups",
    "installMethod",
    "lastReleaseNotesSeen",
    "tipsHistory",
    "subscriptionNoticeCount",
    "recommendedSubscription",
    "loginInProgress",
    "loginInProgressUserId",
    "verifiedSelf",
    "cachedChangelog",
    "bypassPermissionsModeAccepted",
)

# Deep-scrub keys also strip these wherever they appear nested. ``apiKey``
# in particular sits under ``mcpServers.<name>.env`` for some MCP server
# configs and under ``mcpServers.<name>.headers`` for others.
STATE_PII_DEEP_KEYS = (
    "apiKey",
    "api_key",
    "Authorization",
)

TARGET_ORDER = (
    "state_user_id",
    "state_full_identity",
    "legacy_state_file",
    "telemetry_dir",
    "statsig_dir",
    "credentials_file",
    "macos_keychain",
    "paste_cache_dir",
    "settings_auth_env",
    "shell_snapshots_dir",
    "ide_dir",
    "teams_dir",
    "output_styles_dir",
    "session_env_dir",
    "claude_backups_dir",
    "plugins_dir",
    "debug_dir",
    # R6 additions — PII / fingerprint data
    "usage_data_dir",
    "stats_cache_file",
    "startup_perf_dir",
    "image_store_dir",
    "upload_bridge_dir",
    "magic_docs_dir",
    "chrome_dir",
    "cache_dir",
    # R6 additions — internal state (no auth content but cc-managed)
    "mcp_auth_cache_file",
    "jobs_dir",
    "tasks_dir",
    "plans_dir",
    "local_install_dir",
    "update_lock_file",
    "npm_cache_marker",
    "version_cleanup_marker",
    # R6 additions — user-authored (default off)
    "agents_dir",
    "skills_dir",
    "rules_dir",
    "user_claude_md",
    "keybindings_file",
    "completion_cache",
    # R7 pass-1: groups with the user-authored siblings above
    "workflows_dir",
    # R8 pass-1 additions
    "mcp_refresh_locks",
    "xdg_data_claude",
    "xdg_cache_claude",
    "xdg_state_claude",
    "ccr_dir",
    # Existing
    "json_state_backups",
    "json_state_backups_legacy_home",
    # R6 pass 2 additions
    "policy_limits_file",
    "remote_settings_file",
    "computer_use_lock_file",
    # R6 pass-3 additions
    "traces_dir",
    "file_history_dir",
    "session_memory_dir",
    "deep_link_failure_marker",
    "user_commands_dir",
    # R6 pass-4 additions
    "agent_memory_dir",
    "plugin_cache_env_redirect",
    # R6 pass-5 additions
    "dump_prompts_dir",
    "cowork_plugins_dir",
    "remote_memory_base_redirect",
    "scratchpad_tmp_dir",
    "auto_memory_override",
    "projects_dir",
    "history_file",
    "sessions_dir",
)

SAFE_TARGET_KEYS = (
    "state_user_id",
    "legacy_state_file",
    "telemetry_dir",
    "statsig_dir",
    "credentials_file",
    "macos_keychain",
    "paste_cache_dir",
    "debug_dir",
    # R6 SAFE additions — high PII content, low user-disruption.
    "usage_data_dir",
    "stats_cache_file",
    "startup_perf_dir",
    "image_store_dir",
    "upload_bridge_dir",
    "policy_limits_file",
    "remote_settings_file",
    "traces_dir",
    "file_history_dir",
    # Pass-5 audit: ``agent_memory_dir`` REMOVED from SAFE. cc agent
    # memory is user-authored persistent reasoning state, NOT PII
    # telemetry; deleting on the "low-risk" preset surprised users.
    # ``dump_prompts_dir`` IS SAFE — verbatim prompt + tool-catalog
    # dumps are pure debug artifacts with high PII.
    "dump_prompts_dir",
    "json_state_backups",
    "json_state_backups_legacy_home",
    # R8 SAFE additions: mcp-refresh locks (stale crash residue),
    # XDG cache + state (transient native-installer artifacts).
    # XDG data dir holds binaries — deselected by default.
    "mcp_refresh_locks",
    "xdg_cache_claude",
    "xdg_state_claude",
)

FULL_TARGET_KEYS = TARGET_ORDER


_AUTO_MEMORY_ENV_VAR = "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"


def _validate_memory_path(raw: Optional[str], *, expand_tilde: bool) -> Optional[Path]:
    """Mirror cc's ``validateMemoryPath`` (``src/memdir/paths.ts``).

    Reject relative, root-or-near-root, Windows drive-root, UNC, and
    paths containing NUL bytes. Returns the validated NFC-normalised
    Path or None when the input is unsafe / unset.
    """
    if not raw:
        return None
    candidate = raw
    if expand_tilde and (candidate.startswith("~/") or candidate.startswith("~\\")):
        # cc explicitly disallows bare "~" or "~/" — those expand to $HOME
        # or $HOME-adjacent which is too broad for an allowlist root.
        rest = candidate[2:]
        if not rest or rest in (".", "..") or rest.startswith(("/", "\\", ".", "..")):
            return None
        candidate = str(Path("~").expanduser() / rest)
    if "\x00" in candidate:
        return None
    normalised = os.path.normpath(candidate)
    if not os.path.isabs(normalised):
        return None
    if len(normalised) < 3:
        return None
    # Windows drive-root after normpath.
    if len(normalised) <= 3 and normalised[1:].startswith(":"):
        return None
    if normalised.startswith("\\\\") or normalised.startswith("//"):
        return None
    return Path(unicodedata.normalize("NFC", normalised))


def _read_auto_memory_setting(paths: ClaudePaths) -> Optional[str]:
    """Read ``autoMemoryDirectory`` from cc's user settings.json.

    cc consults policy/flag/local/user layers; this scope only reads
    the user layer since policy/flag/local sit elsewhere on disk and
    aren't part of cc-clean's normal target set. Best-effort: any IO
    or parse failure returns None.

    Routed through the shared ``_load_json_dict`` cache so TUI keypress
    storms don't re-read the settings file every plan rebuild.
    """
    settings_path = paths.settings_file
    if not settings_path.is_file():
        return None
    payload, _ = _load_json_dict(settings_path)
    if payload is None:
        return None
    raw = payload.get("autoMemoryDirectory")
    if isinstance(raw, str):
        return raw
    return None


@dataclass(frozen=True)
class AutoMemoryOverride:
    """Resolved auto-memory override state.

    Three states distinguished:
    * ``valid_path`` set, ``rejected_raw`` None — user configured a
      valid override; we should clean the named directory.
    * ``valid_path`` None, ``rejected_raw`` set — user configured
      something but cc's validator rejected it; cc IS using the
      default location, AND the user's intent failed silently. We
      surface a warning so they can fix their settings/env.
    * Both None — no override configured; default location only.
    """

    valid_path: Optional[Path] = None
    rejected_raw: Optional[str] = None
    rejected_source: Optional[str] = None  # "env" or "settings"


def resolve_auto_memory_override_state(
    paths: ClaudePaths,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> AutoMemoryOverride:
    """Tri-state version of :func:`resolve_auto_memory_override`.

    Distinguishes "unset" from "set-but-rejected" so the planner can
    warn users whose intended redirect is silently ignored by cc.

    R10 pass-2 M1: cc's resolver is ``getAutoMemPathOverride() ??
    getAutoMemPathSetting()`` (memdir/paths.ts:225). The ``??`` falls
    through when env is undefined OR returns rejected (validateMemoryPath
    returns undefined for invalid). My R9 tri-state refactor early-returned
    env-rejected without consulting settings — diverging from cc. If the
    user has both an INVALID env AND a VALID settings.autoMemoryDirectory,
    cc uses settings; we'd report rejected and miss the actual location.
    Fall through to settings on env-reject; only report env rejection
    when settings is also unset/rejected (so the user still gets the
    earlier-signal warning).
    """
    if env is None:
        env = os.environ
    raw_env = env.get(_AUTO_MEMORY_ENV_VAR)
    env_rejected_raw: Optional[str] = None
    if raw_env:
        validated = _validate_memory_path(raw_env, expand_tilde=False)
        if validated is not None:
            return AutoMemoryOverride(valid_path=validated)
        env_rejected_raw = raw_env
    raw_setting = _read_auto_memory_setting(paths)
    if raw_setting:
        validated = _validate_memory_path(raw_setting, expand_tilde=True)
        if validated is not None:
            return AutoMemoryOverride(valid_path=validated)
        if env_rejected_raw is None:
            return AutoMemoryOverride(rejected_raw=raw_setting, rejected_source="settings")
    if env_rejected_raw is not None:
        return AutoMemoryOverride(rejected_raw=env_rejected_raw, rejected_source="env")
    return AutoMemoryOverride()


def resolve_auto_memory_override(
    paths: ClaudePaths,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[Path]:
    """Return the user-redirected auto-memory dir, or None if default.

    Backwards-compat thin wrapper around the tri-state version.
    """
    return resolve_auto_memory_override_state(paths, env=env).valid_path


# URL-style sentinel: contains ``://`` which is illegal in filesystem
# paths on every supported OS, so it cannot collide with a literal
# user-set ``CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`` value (cc's own
# ``_validate_memory_path`` already rejects relative / drive-root /
# UNC / NUL-byte paths, and a `://` substring would cause normpath
# to mangle into a useless relative path that we also reject).
_AUTO_MEMORY_TARGET_PLACEHOLDER = "cc-clean://placeholder/auto-memory-unset"
_PLUGIN_CACHE_REDIRECT_PLACEHOLDER = "cc-clean://placeholder/plugin-cache-not-redirected"
_REMOTE_MEMORY_REDIRECT_PLACEHOLDER = "cc-clean://placeholder/remote-memory-not-redirected"


def _expand_tilde(raw: str) -> str:
    """Mirror cc's ``expandTilde`` (utils/permissions/pathValidation.ts:80).

    cc gates ``~\\`` (backslash form) on ``process.platform === 'win32'``.
    Linux MUST leave ``~\\foo`` untouched — ``Path("~\\foo").expanduser()``
    raises ``RuntimeError`` because the segment after ``~`` reads as a
    username. Pass-5 fix omitted this platform gate and pass-6 audit
    caught the regression.
    """
    if not raw:
        return raw
    if raw == "~":
        return str(Path("~").expanduser())
    if raw == "~/":
        return str(Path("~").expanduser())
    # Backslash forms are Windows-only in cc.
    if os.name == "nt" and (raw == "~\\" or raw.startswith("~\\")):
        try:
            return str(Path(raw).expanduser())
        except RuntimeError:
            return raw
    if raw.startswith("~/"):
        try:
            return str(Path(raw).expanduser())
        except RuntimeError:
            return raw
    return raw


def _build_remote_memory_redirect_targets(paths: ClaudePaths) -> Tuple[CleanupTarget, ...]:
    """CLAUDE_CODE_REMOTE_MEMORY_DIR redirect: agent-memory + projects.

    cc memdir/paths.ts uses this env value as the base for both auto-
    memory (``<base>/projects/...``) and agent-memory (``<base>/agent-memory``).
    When unset, ``claude_dir`` is the base (already covered by static
    targets). When set, those subtrees move outside ``~/.claude/`` and
    we need an explicit target.
    """
    raw = paths.remote_memory_base_env
    if raw:
        # Pass-6 M1: cc's ``getMemoryBaseDir`` (memdir/paths.ts:85)
        # uses the env value RAW — no validateMemoryPath, no
        # expandTilde. We must match: a literal ``~/foo`` becomes a
        # cwd-relative ``~/foo`` directory cc actually wrote to.
        base = Path(raw)
        target_path = str(base)
        if base.is_absolute():
            label = "删除 CLAUDE_CODE_REMOTE_MEMORY_DIR 重定向目录（%s）" % base
        else:
            # Pass-7 H1: cwd-relative env value means cc wrote to
            # ``<cc-cwd>/<raw>`` and our cleanup hits ``<cleaner-cwd>/<raw>``.
            # Surface that via the label so users notice the cwd dependency.
            label = (
                "删除 CLAUDE_CODE_REMOTE_MEMORY_DIR 重定向目录（%s，相对当前工作目录）" % base
            )
    else:
        target_path = _REMOTE_MEMORY_REDIRECT_PLACEHOLDER
        label = "删除 CLAUDE_CODE_REMOTE_MEMORY_DIR 重定向（仅在 env 设置时生效）"
    return (
        CleanupTarget(
            key="remote_memory_base_redirect",
            label=label,
            description=(
                "用户通过 CLAUDE_CODE_REMOTE_MEMORY_DIR 把 agent-memory + auto-memory "
                "重定向到 ~/.claude 之外。未设置时 agent_memory_dir + projects_dir 已覆盖默认路径。"
            ),
            action="remove_path",
            target_path=target_path,
            default_selected=False,
            danger=True,
        ),
    )


def _build_plugin_cache_redirect_targets(paths: ClaudePaths) -> Tuple[CleanupTarget, ...]:
    """Emit the plugin-cache redirect target with conditional applicability.

    cc reads ``CLAUDE_CODE_PLUGIN_CACHE_DIR`` (utils/plugins/pluginDirectories.ts:58)
    and when set, ALL plugin state lives under that path instead of
    ``~/.claude/plugins``. We always emit the target so it shows up in
    ``list-targets`` / TUI, but mark inapplicable when env unset.
    """
    raw = paths.plugin_cache_dir_env
    if raw:
        # Pass-5 audit M7: cc's expandTilde also accepts bare `~`,
        # `~/`, `~\\`. ``_expand_tilde`` handles all three forms so
        # we follow cc byte-for-byte.
        redirect_path = _expand_tilde(raw)
        target_path = redirect_path
        label = "删除 plugin cache 重定向目录（%s）" % redirect_path
    else:
        target_path = _PLUGIN_CACHE_REDIRECT_PLACEHOLDER
        label = "删除 CLAUDE_CODE_PLUGIN_CACHE_DIR 重定向（仅在 env 设置时生效）"
    return (
        CleanupTarget(
            key="plugin_cache_env_redirect",
            label=label,
            description=(
                "用户通过 CLAUDE_CODE_PLUGIN_CACHE_DIR 把插件缓存重定向出 ~/.claude/plugins。"
                "未设置时 plugins_dir target 已覆盖默认路径。"
            ),
            action="remove_path",
            target_path=target_path,
            default_selected=False,
            danger=True,
        ),
    )


def _build_dynamic_targets(paths: ClaudePaths) -> Tuple[CleanupTarget, ...]:
    """Targets whose target_path depends on env / settings at plan time.

    ``auto_memory_override`` is ALWAYS emitted so it appears uniformly
    in ``target_keys()`` / ``--list-targets`` / TUI / argparse validation.
    When no override is detected, ``target_path`` is a placeholder
    sentinel (``_AUTO_MEMORY_TARGET_PLACEHOLDER``) and the inspector
    reports ``applicable=False`` with a clear "no override set" hint —
    the user gets actionable feedback instead of a silent drop.
    """
    state = resolve_auto_memory_override_state(paths)
    if state.valid_path is not None:
        target_path = str(state.valid_path)
        label = "删除 auto-memory 重定向目录（%s）" % state.valid_path
    elif state.rejected_raw is not None:
        # Use a per-state placeholder so _inspect_remove_path can
        # tell the user "you set this, but cc rejected it" rather
        # than the generic "no redirect configured" message.
        target_path = "%s/rejected" % _AUTO_MEMORY_TARGET_PLACEHOLDER
        label = (
            "auto-memory 重定向被 cc 拒绝（来源 %s，原值 %r）" %
            (state.rejected_source, state.rejected_raw)
        )
    else:
        target_path = _AUTO_MEMORY_TARGET_PLACEHOLDER
        label = "删除 auto-memory 重定向目录（仅在用户设置 env/settings 重定向时生效）"
    return (
        CleanupTarget(
            key="auto_memory_override",
            label=label,
            description=(
                "用户通过 CLAUDE_COWORK_MEMORY_PATH_OVERRIDE 或 settings.autoMemoryDirectory "
                "把 auto-memory 重定向到了项目默认路径之外；额外清理这个目录。未设置重定向时不可执行。"
            ),
            action="remove_path",
            target_path=target_path,
            default_selected=False,
            danger=True,
            may_remove_sessions=True,
        ),
    )


def build_targets(paths: ClaudePaths) -> Tuple[CleanupTarget, ...]:
    # ``state_oauth_globs`` covers every state file cc may have written:
    # the prod ``~/.claude.json`` plus the three per-channel variants
    # (staging/local/custom oauth). When ``CLAUDE_CONFIG_DIR`` is set, all
    # of these live under ``config_root`` instead of ``home``.
    state_oauth_globs = (
        ".claude.json",
        ".claude-staging-oauth.json",
        ".claude-local-oauth.json",
        ".claude-custom-oauth.json",
    )
    return (
        CleanupTarget(
            key="state_user_id",
            label="清理 .claude*.json 中的 userID 字段（含 oauth 后缀变体）",
            description="对所有可能的 cc 状态文件（prod / staging / local / custom-oauth）扫一遍并移除顶层 userID。",
            action="scrub_json_fields",
            target_path=str(paths.config_root),
            json_fields=("userID",),
            glob_patterns=state_oauth_globs,
            default_selected=True,
        ),
        CleanupTarget(
            key="state_full_identity",
            label="深度清理 .claude*.json 中的全部身份指纹字段",
            description="移除 userID/oauthAccount/customApiKeyResponses/numStartups 等全部 PII，并递归扫描嵌套层（含 mcpServers 内的 apiKey）。覆盖所有 oauth 后缀变体。",
            action="scrub_json_fields",
            target_path=str(paths.config_root),
            json_fields=STATE_PII_FIELDS,
            glob_patterns=state_oauth_globs,
            default_selected=False,
            deep_scrub=True,
        ),
        CleanupTarget(
            key="legacy_state_file",
            label="清理 ~/.claude/.config.json 中的 PII（旧版 cc 安装位置）",
            description="若用户从旧版 cc 升级，state file 可能残留在 ~/.claude/.config.json；扫一遍并深度移除身份字段。",
            action="scrub_json_fields",
            target_path=str(paths.legacy_state_file),
            json_fields=STATE_PII_FIELDS,
            default_selected=True,
            deep_scrub=True,
        ),
        CleanupTarget(
            key="telemetry_dir",
            label="删除 ~/.claude/telemetry",
            description="清理失败遥测缓存。",
            action="remove_path",
            target_path=str(paths.telemetry_dir),
            default_selected=True,
        ),
        CleanupTarget(
            key="statsig_dir",
            label="删除 ~/.claude/statsig",
            description="清理 Statsig 稳定 ID、会话 ID 和缓存评估结果。",
            action="remove_path",
            target_path=str(paths.statsig_dir),
            default_selected=True,
        ),
        CleanupTarget(
            key="credentials_file",
            label="删除 ~/.claude/.credentials.json",
            description="如果存在这个回退凭据文件，就删除其中的明文本地凭据。",
            action="remove_path",
            target_path=str(paths.credentials_file),
            default_selected=True,
        ),
        CleanupTarget(
            key="macos_keychain",
            label="删除 macOS Keychain 中的 cc OAuth 凭据",
            description="darwin 平台 cc 把 oauth token 写到系统 keychain，cc-clean 调用 `security delete-generic-password` 清除（含 legacy + suffixed service name）。非 macOS 平台自动跳过。",
            action="purge_keychain",
            target_path="darwin-keychain",
            default_selected=True,
        ),
        CleanupTarget(
            key="paste_cache_dir",
            label="删除 ~/.claude/paste-cache",
            description="清理 cc 存的用户粘贴文本快照（sha256 命名但内容明文）。",
            action="remove_path",
            target_path=str(paths.paste_cache_dir),
            default_selected=True,
        ),
        CleanupTarget(
            key="settings_auth_env",
            label="清理 settings.json 中的自定义鉴权环境变量",
            description="从 env 中移除 ANTHROPIC_AUTH_TOKEN 和 ANTHROPIC_BASE_URL。",
            action="scrub_settings_env",
            target_path=str(paths.settings_file),
            env_keys=AUTH_ENV_KEYS,
            default_selected=False,
        ),
        CleanupTarget(
            key="shell_snapshots_dir",
            label="删除 ~/.claude/shell-snapshots",
            description="清理 cc 在每个会话录制的 shell 状态快照（包含 cwd 历史）。",
            action="remove_path",
            target_path=str(paths.shell_snapshots_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="ide_dir",
            label="删除 ~/.claude/ide",
            description="清理 IDE 插件握手状态；下次启动会自动重新注册。",
            action="remove_path",
            target_path=str(paths.ide_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="teams_dir",
            label="删除 ~/.claude/teams",
            description="清理团队配置目录（成员邮箱、UUID 等）。",
            action="remove_path",
            target_path=str(paths.teams_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="output_styles_dir",
            label="删除 ~/.claude/output-styles",
            description="清理用户级 output style markdown 文件（用户自定义；非 PII，默认不勾选）。",
            action="remove_path",
            target_path=str(paths.output_styles_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="completion_cache",
            label="删除 ~/.claude/completion.{bash,zsh,fish}",
            description="cc shell 自动补全缓存。重启 cc 后会再生。",
            action="remove_glob",
            target_path=str(paths.claude_dir),
            glob_patterns=(paths.completion_glob,),
            default_selected=False,
        ),
        CleanupTarget(
            key="mcp_refresh_locks",
            label="删除 ~/.claude/mcp-refresh-*.lock",
            description=(
                "cc 每个 MCP server 的 OAuth 刷新锁文件。崩溃后残留会"
                "阻塞下次启动 token 刷新。"
            ),
            action="remove_glob",
            target_path=str(paths.claude_dir),
            glob_patterns=(paths.mcp_refresh_glob,),
            default_selected=True,
        ),
        CleanupTarget(
            key="xdg_data_claude",
            label="删除 $XDG_DATA_HOME/claude（默认 ~/.local/share/claude）",
            description=(
                "cc native installer 把多版本二进制安装到 XDG_DATA_HOME 下。"
                "包括 versions/ 子目录，可能含旧版二进制。"
            ),
            action="remove_path",
            target_path=str(paths.xdg_data_claude),
            default_selected=False,
        ),
        CleanupTarget(
            key="xdg_cache_claude",
            label="删除 $XDG_CACHE_HOME/claude（默认 ~/.cache/claude）",
            description="cc native installer staging cache (~/.cache/claude/staging/)。",
            action="remove_path",
            target_path=str(paths.xdg_cache_claude),
            default_selected=True,
        ),
        CleanupTarget(
            key="xdg_state_claude",
            label="删除 $XDG_STATE_HOME/claude（默认 ~/.local/state/claude）",
            description="cc native installer 锁文件（locks/ 子目录）。",
            action="remove_path",
            target_path=str(paths.xdg_state_claude),
            default_selected=True,
        ),
        CleanupTarget(
            key="ccr_dir",
            label="删除 ~/.ccr",
            description=(
                "cc upstreamproxy 自定义 CA bundle (ca-bundle.crt)。"
                "仅当 ANTHROPIC_BASE_URL 走代理需要自签证书时存在。默认不勾。"
            ),
            action="remove_path",
            target_path=str(paths.ccr_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="workflows_dir",
            label="删除 ~/.claude/workflows",
            description="清理用户级 workflow markdown 定义（与 commands/agents/skills 并列；用户自定义内容）。",
            action="remove_path",
            target_path=str(paths.workflows_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="session_env_dir",
            label="删除 ~/.claude/session-env",
            description="清理 cc 写入的 session env 指纹。",
            action="remove_path",
            target_path=str(paths.session_env_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="claude_backups_dir",
            label="删除 ~/.claude/backups",
            description="清理 cc 自己生成的 ~/.claude.json 历史快照（仍包含旧 PII）。",
            action="remove_path",
            target_path=str(paths.claude_backups_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="plugins_dir",
            label="删除 ~/.claude/plugins",
            description=(
                "清理插件 marketplace 元数据 + 安装缓存。"
                "known_marketplaces.json 含用户配置的 marketplace URL（可能为私有 git 仓），"
                "installed_plugins.json 含本地安装路径。"
            ),
            action="remove_path",
            target_path=str(paths.plugins_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="debug_dir",
            label="删除 ~/.claude/debug",
            description=(
                "清理 `claude --debug` 模式下产生的会话调试日志（含完整 prompt / response 文本）。"
                "正常使用 cc 时此目录可能不存在；只有显式打开 debug 才写入。"
            ),
            action="remove_path",
            target_path=str(paths.debug_dir),
            default_selected=True,
        ),
        # ---- R6 PII / fingerprint targets (default in SAFE preset) ----
        CleanupTarget(
            key="usage_data_dir",
            label="删除 ~/.claude/usage-data",
            description="清理按组织维度统计的模型使用数据（含 org-id、token 计数）。",
            action="remove_path",
            target_path=str(paths.usage_data_dir),
            default_selected=True,
        ),
        CleanupTarget(
            key="stats_cache_file",
            label="删除 ~/.claude/stats-cache.json",
            description="cc 内部统计缓存（含会话计数 / 工具调用计数）。",
            action="remove_path",
            target_path=str(paths.stats_cache_file),
            default_selected=True,
        ),
        CleanupTarget(
            key="startup_perf_dir",
            label="删除 ~/.claude/startup-perf",
            description="cc 启动性能 trace（每会话写一份，含会话 ID）。",
            action="remove_path",
            target_path=str(paths.startup_perf_dir),
            default_selected=True,
        ),
        CleanupTarget(
            key="image_store_dir",
            label="删除 ~/.claude/image-cache",
            description="cc 图像存储目录（每会话粘贴/上传图片 — 可能含截图敏感内容）。",
            action="remove_path",
            target_path=str(paths.image_store_dir),
            default_selected=True,
        ),
        CleanupTarget(
            key="upload_bridge_dir",
            label="删除 ~/.claude/uploads",
            description="cc bridge 入站附件缓存（每会话写到 uploads/<sid>/）。",
            action="remove_path",
            target_path=str(paths.upload_bridge_dir),
            default_selected=True,
        ),
        CleanupTarget(
            key="magic_docs_dir",
            label="删除 ~/.claude/magic-docs",
            description="MagicDocs 服务的 prompt 模板缓存。",
            action="remove_path",
            target_path=str(paths.magic_docs_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="chrome_dir",
            label="删除 ~/.claude/chrome",
            description="Claude in Chrome native host 安装位（含 chrome 通信状态）。",
            action="remove_path",
            target_path=str(paths.chrome_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="cache_dir",
            label="删除 ~/.claude/cache",
            description="cc 模型能力 / changelog 缓存。删除后下次启动会重新拉取。",
            action="remove_path",
            target_path=str(paths.cache_dir),
            default_selected=False,
        ),
        # ---- R6 internal cc-managed state (default off, full preset only) ----
        CleanupTarget(
            key="mcp_auth_cache_file",
            label="删除 ~/.claude/mcp-needs-auth-cache.json",
            description="MCP 服务认证状态缓存（哪些 server 还需要授权）。",
            action="remove_path",
            target_path=str(paths.mcp_auth_cache_file),
            default_selected=False,
        ),
        CleanupTarget(
            key="jobs_dir",
            label="删除 ~/.claude/jobs",
            description="cc 后台 job 状态目录。运行中清理可能干扰活跃 job。",
            action="remove_path",
            target_path=str(paths.jobs_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="tasks_dir",
            label="删除 ~/.claude/tasks",
            description="cc 任务状态目录（agent 任务持久化）。",
            action="remove_path",
            target_path=str(paths.tasks_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="plans_dir",
            label="删除 ~/.claude/plans",
            description="用户保存的 /plan 文档（含完整 plan 内容）。",
            action="remove_path",
            target_path=str(paths.plans_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="local_install_dir",
            label="删除 ~/.claude/local",
            description="native installer 本地安装缓存。删除等于卸载 local 安装的 cc。",
            action="remove_path",
            target_path=str(paths.local_install_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="update_lock_file",
            label="删除 ~/.claude/.update.lock",
            description="autoUpdater 锁文件。cc 运行时不应删除。",
            action="remove_path",
            target_path=str(paths.update_lock_file),
            default_selected=False,
        ),
        CleanupTarget(
            key="npm_cache_marker",
            label="删除 ~/.claude/.npm-cache-cleanup 标记",
            description="cc 内部 npm cache 清理周期标记；删除会触发下次启动重清 npm cache。",
            action="remove_path",
            target_path=str(paths.npm_cache_marker),
            default_selected=False,
        ),
        CleanupTarget(
            key="version_cleanup_marker",
            label="删除 ~/.claude/.version-cleanup 标记",
            description="cc 内部版本清理周期标记；删除会触发下次启动重清旧版本。",
            action="remove_path",
            target_path=str(paths.version_cleanup_marker),
            default_selected=False,
        ),
        # ---- R6 user-authored content (default off — user contribution) ----
        CleanupTarget(
            key="agents_dir",
            label="删除 ~/.claude/agents",
            description="用户自定义 agent 定义。删除丢失自定义工作流。",
            action="remove_path",
            target_path=str(paths.agents_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="skills_dir",
            label="删除 ~/.claude/skills",
            description="用户级 skill 定义。删除丢失 /skill 命令。",
            action="remove_path",
            target_path=str(paths.skills_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="rules_dir",
            label="删除 ~/.claude/rules",
            description="用户级规则文件（行为约束）。",
            action="remove_path",
            target_path=str(paths.rules_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="user_claude_md",
            label="删除 ~/.claude/CLAUDE.md",
            description="用户级 CLAUDE.md（全局记忆/偏好）。删除丢失 cc 对你的所有学习。",
            action="remove_path",
            target_path=str(paths.user_claude_md),
            default_selected=False,
            danger=True,
        ),
        CleanupTarget(
            key="keybindings_file",
            label="删除 ~/.claude/keybindings.json",
            description="用户自定义 cc 键位绑定。",
            action="remove_path",
            target_path=str(paths.keybindings_file),
            default_selected=False,
        ),
        CleanupTarget(
            key="json_state_backups",
            label="删除 ~/.claude/backups/.claude*.json.backup.* 旧快照",
            description=(
                "cc 启动时会通过 getConfigBackupDir() 把 corruption recovery 备份"
                "写到 ~/.claude/backups/，其中仍残留旧 userID。覆盖 prod 与所有 oauth"
                "后缀变体。注意：早期 cc 版本可能写在 HOME 直下，所以也会扫一遍 ~ 兜底。"
            ),
            action="remove_glob",
            target_path=str(paths.claude_backups_dir),
            glob_patterns=(paths.state_backup_glob, paths.state_corrupted_glob),
            default_selected=True,
        ),
        # 兜底：扫旧版 cc 直接写在 HOME 的 .claude.json.backup.<NN> 文件。
        # 现代 cc 写到 ~/.claude/backups/，但用户可能从老版本升级。
        CleanupTarget(
            key="json_state_backups_legacy_home",
            label="删除 ~/.claude*.json.backup.* 旧版 HOME 直下快照",
            description="兼容老版 cc：corruption 备份直接落在 HOME 目录的情况。",
            action="remove_glob",
            target_path=str(paths.config_root),
            glob_patterns=(paths.state_backup_glob, paths.state_corrupted_glob),
            default_selected=True,
        ),
        # ---- R6 pass-2 audit additions ----
        CleanupTarget(
            key="policy_limits_file",
            label="删除 ~/.claude/policy-limits.json",
            description="cc 缓存的账号策略数据（含组织限额信息）。",
            action="remove_path",
            target_path=str(paths.policy_limits_file),
            default_selected=True,
        ),
        CleanupTarget(
            key="remote_settings_file",
            label="删除 ~/.claude/remote-settings.json",
            description="cc 同步的远端 managed-config 缓存（含组织级策略快照）。",
            action="remove_path",
            target_path=str(paths.remote_settings_file),
            default_selected=True,
        ),
        CleanupTarget(
            key="computer_use_lock_file",
            label="删除 ~/.claude/computer-use.lock",
            description="cc computer-use 锁文件（含会话 ID + PID）。仅在 cc 未运行时清理安全。",
            action="remove_path",
            target_path=str(paths.computer_use_lock_file),
            default_selected=False,
        ),
        # ---- R6 pass-3 audit additions ----
        CleanupTarget(
            key="traces_dir",
            label="删除 ~/.claude/traces",
            description="cc Perfetto trace JSON dumps（含 prompt 内容；仅在 CLAUDE_CODE_PERFETTO_TRACE 真值时写）。",
            action="remove_path",
            target_path=str(paths.traces_dir),
            default_selected=True,
        ),
        CleanupTarget(
            key="file_history_dir",
            label="删除 ~/.claude/file-history",
            description="cc Edit 工具会把每次写入文件前的快照（sha256 命名）存这里——含完整文件内容。高 PII。",
            action="remove_path",
            target_path=str(paths.file_history_dir),
            default_selected=True,
        ),
        CleanupTarget(
            key="session_memory_dir",
            label="删除 ~/.claude/session-memory",
            description="cc session-memory template + prompt 模板（用户自定义）。",
            action="remove_path",
            target_path=str(paths.session_memory_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="deep_link_failure_marker",
            label="删除 ~/.claude/.deep-link-register-failed 标记",
            description="cc deep-link 注册失败标记（24h backoff）。删除会让下次启动重试注册。",
            action="remove_path",
            target_path=str(paths.deep_link_failure_marker),
            default_selected=False,
        ),
        CleanupTarget(
            key="user_commands_dir",
            label="删除 ~/.claude/commands",
            description="用户级 slash-command markdown 目录（与 skills/ 平行）。用户自定义。",
            action="remove_path",
            target_path=str(paths.user_commands_dir),
            default_selected=False,
        ),
        CleanupTarget(
            key="agent_memory_dir",
            label="删除 ~/.claude/agent-memory",
            description=(
                "cc agent 持久化记忆（user scope，按 agentType 分子目录）。"
                "用户自定义内容；删除后丢失 agent 累积的 reasoning state。"
                "默认不勾选；偏向用户作品保留。"
            ),
            action="remove_path",
            target_path=str(paths.agent_memory_dir),
            default_selected=False,
        ),
        # CLAUDE_CODE_PLUGIN_CACHE_DIR redirect — cc 把插件缓存写到 env-set
        # 路径而非 ~/.claude/plugins。dynamic-style sentinel：仅在 env 设置
        # 时 applicable=True。
        *_build_plugin_cache_redirect_targets(paths),
        CleanupTarget(
            key="dump_prompts_dir",
            label="删除 ~/.claude/dump-prompts",
            description=(
                "cc dump-prompts 模式下产生的完整 prompt + system + tool catalog 转储 (JSONL)。"
                "高 PII；若 cc 未启用 dump 此目录不存在。"
            ),
            action="remove_path",
            target_path=str(paths.dump_prompts_dir),
            default_selected=True,
        ),
        CleanupTarget(
            key="cowork_plugins_dir",
            label="删除 ~/.claude/cowork_plugins",
            description=(
                "cc cowork 插件变体目录（CLAUDE_CODE_USE_COWORK_PLUGINS 真值时启用）。"
                "与 plugins_dir 互斥。"
            ),
            action="remove_path",
            target_path=str(paths.cowork_plugins_dir),
            default_selected=False,
        ),
        # CLAUDE_CODE_REMOTE_MEMORY_DIR redirect for agent-memory + auto-memory.
        *_build_remote_memory_redirect_targets(paths),
        CleanupTarget(
            key="scratchpad_tmp_dir",
            label="清理 ${CLAUDE_CODE_TMPDIR}/claude scratchpad 目录",
            description=(
                "cc 在每会话 scratchpad 目录里暂存工具/prompt 副本。POSIX: "
                "${CLAUDE_CODE_TMPDIR or /tmp}/claude-<uid>/。Windows: "
                "${CLAUDE_CODE_TMPDIR or %TEMP%}/claude/。R8 起跨平台。"
                "正常退出会清，崩溃后残留。"
            ),
            action="purge_scratchpad",
            # Magic-string placeholder; the executor reads via
            # ``_resolve_scratchpad_root`` instead of this field.
            target_path="<scratchpad-resolver>",
            default_selected=False,
        ),
        # ``auto_memory_override`` is dynamic: only present when the user
        # has redirected memory via env or settings. ``_build_dynamic_*``
        # below does the lookup so the constant TARGET_ORDER can still
        # advertise the key for argparse / TUI registration.
        *_build_dynamic_targets(paths),
        CleanupTarget(
            key="projects_dir",
            label="删除 ~/.claude/projects",
            description="删除项目对话历史，可能会丢失旧的项目会话。",
            action="remove_path",
            target_path=str(paths.projects_dir),
            default_selected=False,
            danger=True,
            may_remove_sessions=True,
        ),
        CleanupTarget(
            key="history_file",
            label="删除 ~/.claude/history.jsonl",
            description="删除全局命令/历史日志。",
            action="remove_path",
            target_path=str(paths.history_file),
            default_selected=False,
            danger=True,
            may_remove_sessions=True,
        ),
        CleanupTarget(
            key="sessions_dir",
            label="删除 ~/.claude/sessions（cc 进程跟踪 PID 文件）",
            description="此目录是 cc 用于并发会话进程跟踪的 PID 文件（不是聊天会话；后者在 projects/）。仅在 cc 完全未运行时清理是安全的。",
            action="remove_path",
            target_path=str(paths.sessions_dir),
            default_selected=False,
            danger=True,
        ),
    )


def resolve_selection(
    preset: str = "safe",
    include_keys: Optional[Sequence[str]] = None,
    exclude_keys: Optional[Sequence[str]] = None,
) -> Set[str]:
    if preset not in {"safe", "full", "none"}:
        raise ValueError("preset must be one of: safe, full, none")

    selected: Set[str]
    if preset == "safe":
        selected = set(SAFE_TARGET_KEYS)
    elif preset == "full":
        selected = set(FULL_TARGET_KEYS)
    else:
        selected = set()

    include_keys = tuple(include_keys or ())
    exclude_keys = tuple(exclude_keys or ())
    unknown = (set(include_keys) | set(exclude_keys)) - set(TARGET_ORDER)
    if unknown:
        unknown_text = ", ".join(sorted(unknown))
        raise ValueError("unknown cleanup target(s): %s" % unknown_text)

    selected.update(include_keys)
    selected.difference_update(exclude_keys)
    return selected


def build_plan(paths: ClaudePaths, selected_keys: Optional[Iterable[str]] = None) -> Tuple[PlanItem, ...]:
    selected = set(selected_keys if selected_keys is not None else SAFE_TARGET_KEYS)
    plan: List[PlanItem] = []
    for target in build_targets(paths):
        if target.action == "remove_path":
            item = _inspect_remove_path(target, selected)
        elif target.action == "remove_glob":
            item = _inspect_remove_glob(target, selected)
        elif target.action == "scrub_json_fields":
            if target.glob_patterns:
                item = _inspect_json_fields_glob(target, selected)
            else:
                item = _inspect_json_fields(target, selected)
        elif target.action == "scrub_settings_env":
            item = _inspect_settings_env(target, selected)
        elif target.action == "purge_keychain":
            item = _inspect_purge_keychain(target, selected)
        elif target.action == "purge_scratchpad":
            item = _inspect_purge_scratchpad(target, selected)
        else:
            raise ValueError("unsupported cleanup action: %s" % target.action)
        plan.append(item)
    return tuple(plan)


_CC_CLEAN_LOCK_FILENAME = ".cc-clean.lock"


def _cc_clean_lock_path(paths: ClaudePaths) -> Path:
    return paths.backup_root_base / _CC_CLEAN_LOCK_FILENAME


@contextmanager
def _try_cc_clean_lock(paths: ClaudePaths) -> Iterator[bool]:
    """Acquire the cross-process cc-clean lock with graceful fallback.

    R7 pass-2 H3: if ``backup_root_base`` is read-only or the lock
    file is owned by another user, ``file_lock``'s ``os.open(...)``
    raises and the bare context manager would crash CLI/TUI. We try
    to ensure the directory + acquire the lock; on failure we yield
    False so callers can record a warning and continue without lock.

    Yielded value indicates whether the lock was actually held.
    """
    try:
        paths.backup_root_base.mkdir(parents=True, exist_ok=True)
    except OSError:
        # No way to create the lock parent — proceed unlocked.
        yield False
        return
    try:
        with file_lock(_cc_clean_lock_path(paths)):
            yield True
    except OSError:
        yield False


def execute_plan(paths: ClaudePaths, plan: Sequence[PlanItem], options: RunOptions) -> ExecutionSummary:
    # R7 pass-1 M5: prevent concurrent cc-clean runs from racing the
    # same source paths. R7 pass-2 H3: tolerate read-only / cross-user
    # lock-file ownership by falling back to no-lock mode rather than
    # crashing. Lock spans the full ``execute_plan`` lifetime.
    with _try_cc_clean_lock(paths):
        return _execute_plan_locked(paths, plan, options)


def _execute_plan_locked(paths: ClaudePaths, plan: Sequence[PlanItem], options: RunOptions) -> ExecutionSummary:
    backup_root: Optional[Path] = None
    records: List[ExecutionRecord] = []

    for item in plan:
        if not item.selected:
            continue

        if not item.applicable:
            records.append(
                ExecutionRecord(
                    key=item.target.key,
                    status="skipped",
                    message=item.details,
                )
            )
            continue

        target_path = Path(item.target.target_path)
        if options.dry_run:
            records.append(
                ExecutionRecord(
                    key=item.target.key,
                    status="dry-run",
                    message="演练：%s → %s" % (item.target.action, item.target.target_path),
                )
            )
            continue

        try:
            if item.target.action == "remove_path":
                backup_root = _ensure_backup_root(paths, backup_root, options)
                record = _execute_remove_path(paths, target_path, item, backup_root, options)
            elif item.target.action == "remove_glob":
                backup_root = _ensure_backup_root(paths, backup_root, options)
                record = _execute_remove_glob(paths, item, backup_root, options)
            elif item.target.action == "scrub_json_fields":
                if item.target.glob_patterns:
                    backup_root = _ensure_backup_root(paths, backup_root, options)
                    record = _execute_scrub_json_fields_glob(paths, item, backup_root, options)
                else:
                    backup_root = _ensure_backup_root(paths, backup_root, options)
                    record = _execute_scrub_json_fields(paths, target_path, item, backup_root, options)
            elif item.target.action == "scrub_settings_env":
                backup_root = _ensure_backup_root(paths, backup_root, options)
                record = _execute_scrub_settings_env(paths, target_path, item, backup_root, options)
            elif item.target.action == "purge_keychain":
                # No backup possible — keychain is opaque.
                record = _execute_purge_keychain(paths, item)
            elif item.target.action == "purge_scratchpad":
                backup_root = _ensure_backup_root(paths, backup_root, options)
                record = _execute_purge_scratchpad(paths, item, backup_root, options)
            else:
                raise ValueError("unsupported cleanup action: %s" % item.target.action)
        except Exception as exc:
            # Pass-7 M2: ``str(exc)`` may embed absolute paths or
            # partial credentials (OSError typically formats as
            # "[Errno N] reason: '/path/to/secret'"). JSON output
            # gets piped to automation logs / shipped via webhooks
            # — keep the type name so debugging is possible without
            # leaking the path. Full traceback can be surfaced via a
            # debug log later if needed.
            record = ExecutionRecord(
                key=item.target.key,
                status="error",
                message="%s" % type(exc).__name__,
            )

        records.append(record)

    return ExecutionSummary(
        records=tuple(records),
        backup_root=str(backup_root) if backup_root is not None else None,
    )


def format_bytes(size_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(max(0, size_bytes))
    unit = units[0]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            break
        size /= 1024.0
    if unit == "B":
        return "%d %s" % (int(size), unit)
    return "%.1f %s" % (size, unit)


def target_keys() -> Tuple[str, ...]:
    return TARGET_ORDER


def list_backup_roots(paths: ClaudePaths) -> Tuple[Path, ...]:
    """Return backup roots under ``backup_root_base`` newest-first.

    Used by both ``aik claude restore`` (lets the user pick a snapshot to
    revert to) and the retention pruner. Sort by mtime so a system clock
    skew doesn't reorder the user's mental "newest" timestamp.

    Note on interface asymmetry: this function returns a plain tuple
    while ``prune_backup_roots`` returns a ``PruneOutcome`` dataclass.
    Intentional — listing has no fail mode worth structuring (a missing
    base dir simply yields an empty tuple), whereas pruning needs to
    distinguish removed roots from partially-failed ones so the user
    can chase the failures.
    """
    base = paths.backup_root_base
    if not base.exists() or not base.is_dir():
        return ()
    roots = [child for child in base.iterdir() if child.is_dir()]
    # M2 fix: stat may race with concurrent prune. Drop entries that
    # vanish between is_dir() and stat() rather than letting the
    # OSError escape.
    def _safe_mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return -1.0  # treat missing as oldest
    roots = [r for r in roots if _safe_mtime(r) >= 0]
    roots.sort(key=_safe_mtime, reverse=True)
    return tuple(roots)


def restore_from_backup(
    paths: ClaudePaths,
    backup_root: Path,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
) -> ExecutionSummary:
    """Mirror ``backup_root`` back into ``paths.home``.

    Walks ``backup_root`` and copies each file into the corresponding
    location under home. When ``overwrite`` is False (the default), any
    existing destination file is reported as ``skipped`` so the user can
    re-run with ``overwrite=True`` once they've reviewed the differences.

    The backup tree mirrors home structure produced by ``_relative_under_home``,
    so the inverse mapping is just ``destination = home / relative``.
    Symlinks in the backup are restored as symlinks (best-effort — Windows
    without dev mode falls back to copying the link target).
    """
    backup_root = Path(backup_root).expanduser()
    if not backup_root.exists() or not backup_root.is_dir():
        return ExecutionSummary(
            records=(
                ExecutionRecord(
                    key="restore",
                    status="error",
                    message="备份目录不存在或不是目录：%s" % backup_root.name,
                ),
            ),
            backup_root=str(backup_root),
        )

    # R7 pass-8 M3: hold the cc-clean lock for the full restore so
    # concurrent ``aik claude prune-backups`` can't rmtree the
    # backup_root mid-walk. Mirrors ``execute_plan`` and
    # ``prune_backup_roots`` which already lock.
    with _try_cc_clean_lock(paths):
        return _restore_from_backup_locked(paths, backup_root, dry_run=dry_run, overwrite=overwrite)


def _restore_from_backup_locked(
    paths: ClaudePaths,
    backup_root: Path,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
) -> ExecutionSummary:

    # When the backup was taken with ``CLAUDE_CONFIG_DIR`` redirecting cc
    # data outside ``$HOME``, files at relative ``.claude.json`` came
    # from ``$CONFIG_DIR/.claude.json``, NOT ``$HOME/.claude.json``.
    # The metadata sidecar tells us which anchor to use; we list
    # anchors home-first as the legacy default for backups without meta.
    #
    # R7 pass-4 H1 (security): the meta sidecar may come from an
    # untrusted backup tarball. An attacker who hands the user a
    # crafted backup containing meta with ``config_root: "/"`` could
    # cause restore to write to ``/etc/passwd`` via crafted relative
    # paths. We validate the meta-supplied anchor is actually one of
    # the user's known anchors (``paths.home`` or ``paths.config_root``)
    # before trusting it.
    meta = _read_backup_metadata(backup_root)
    anchors: List[Path] = [paths.home]
    # R9 M2: trust XDG parents too so backups taken with
    # ``XDG_DATA_HOME=/srv/share`` can restore. Symmetric with
    # ``_relative_under_anchors`` extension above.
    trusted_anchor_strs = {
        os.path.normcase(str(paths.home)),
        os.path.normcase(str(paths.config_root)),
        os.path.normcase(str(paths.xdg_data_claude.parent)),
        os.path.normcase(str(paths.xdg_cache_claude.parent)),
        os.path.normcase(str(paths.xdg_state_claude.parent)),
    }
    if meta is not None:
        meta_config_root = meta.get("config_root")
        if isinstance(meta_config_root, str) and meta_config_root:
            anchor_candidate = Path(meta_config_root)
            if (
                os.path.normcase(str(anchor_candidate)) in trusted_anchor_strs
                and anchor_candidate not in anchors
            ):
                # Trusted: matches one of the user's known anchors.
                anchors.insert(0, anchor_candidate)
            # else: untrusted meta value — silently ignore. Restore
            # falls back to ``paths.home`` anchor only. The user can
            # explicitly re-set CLAUDE_CONFIG_DIR before restore if
            # they have a legit redirected layout.
    elif paths.config_root != paths.home:
        # No meta but current paths suggest a redirected layout — try
        # config_root as a hint for backups taken before we shipped meta.
        anchors.insert(0, paths.config_root)

    # R10 pass-4 H1: extend `anchors` (used for ``chosen_anchor``
    # resolution at L1513) with XDG parents falling outside home.
    # Without this, files that backup as ``claude/...`` (anchored on
    # ``$XDG_DATA_HOME`` etc) restore to ``$HOME/claude/...`` instead
    # of the original XDG location — silent data misplacement.
    # Symmetric with `_relative_under_anchors` extension at the
    # backup-build site.
    for xdg in (paths.xdg_data_claude.parent, paths.xdg_cache_claude.parent, paths.xdg_state_claude.parent):
        try:
            xdg.relative_to(paths.home)
        except ValueError:
            if xdg not in anchors:
                anchors.append(xdg)

    records: List[ExecutionRecord] = []
    restored = 0
    skipped = 0
    errors = 0
    # R7 pass-7 M3: cache realpath() results within a single restore
    # call. Restoring 1000 files in an 8-deep tree previously made
    # ~8000 redundant realpath syscalls because we re-resolved the
    # same ancestors per file. Bounded by tree fanout × depth.
    realpath_cache: Dict[str, str] = {}

    def _cached_realpath(path_str: str) -> str:
        cached = realpath_cache.get(path_str)
        if cached is not None:
            return cached
        try:
            resolved = os.path.realpath(path_str)
        except OSError:
            resolved = path_str
        realpath_cache[path_str] = resolved
        return resolved

    for src_path in _iter_backup_files(backup_root):
        try:
            relative = src_path.relative_to(backup_root)
        except ValueError:
            continue
        # The "external/" prefix is how we encode files that originated
        # outside the home tree. Restoring those into home is unsafe (we
        # don't know the original absolute path), so skip them with a
        # clear note so the user can copy manually.
        if relative.parts and relative.parts[0] == "external":
            records.append(
                ExecutionRecord(
                    key="restore_file",
                    status="skipped",
                    message="external/ 前缀来自非主目录的源；请手动还原。",
                )
            )
            skipped += 1
            continue
        # Pick the anchor whose layout best matches this relative path.
        # ``.claude/...`` files only fit the home anchor (where cc puts
        # them when CLAUDE_CONFIG_DIR is unset). ``.claude.json`` (no
        # subdir) fits whichever anchor was active at backup time —
        # prefer the meta-recorded one, falling back to home.
        # ``claude/...`` (no leading dot) is XDG-anchored — prefer the
        # XDG parent matching the layout (R10 pass-4 H1).
        chosen_anchor = anchors[0]
        if relative.parts and relative.parts[0] == ".claude":
            chosen_anchor = paths.home
        elif relative.parts and relative.parts[0] == "claude":
            # XDG-redirected backup. R10 pass-6 H1: when MORE THAN ONE
            # of XDG_{DATA,CACHE,STATE}_HOME is outside ``$HOME``, the
            # naive "first outside-home XDG parent" picks data for
            # everything — a cache file from
            # ``XDG_CACHE_HOME=/srv/cache/claude/staging/x`` lands at
            # ``/srv/data/claude/staging/x``, invisible to cc.
            # Route by cc's native-installer subdir convention
            # (verified against ``installer.ts``):
            #   ``claude/versions/`` -> data
            #   ``claude/staging/``  -> cache
            #   ``claude/locks/``    -> state
            # Unknown subdirs (or no subdir) fall back to data — cc's
            # most-populated XDG dir, matching pre-fix behaviour.
            #
            # R10 pass-5 H1: when none of the three XDG parents sits
            # outside home (default layout), restore to the picked
            # type's ``$HOME/.local/share|cache|state/claude/...`` —
            # the right cc-discoverable location instead of bare
            # ``$HOME/claude/``.
            xdg_picks = (
                paths.xdg_data_claude.parent,
                paths.xdg_cache_claude.parent,
                paths.xdg_state_claude.parent,
            )
            second = relative.parts[1] if len(relative.parts) >= 2 else None
            if second == "staging":
                chosen_anchor = xdg_picks[1]
            elif second == "locks":
                chosen_anchor = xdg_picks[2]
            else:
                chosen_anchor = xdg_picks[0]
        dst_path = chosen_anchor / relative
        # R7 pass-4 H1 (security): defense-in-depth path containment.
        # Even after anchor validation, the relative path may use
        # ``..`` segments to escape via the resolve() expansion. Reject
        # any dst that resolves outside one of the known anchors.
        #
        # R7 pass-5 H1: legit cc layouts symlink ~/.claude → /var/lib/...
        # The realpath of dst_path resolves through that link, but the
        # raw anchor (paths.home) doesn't. Build an EXTENDED anchor
        # set that includes both the raw form and the realpath form
        # for each known anchor, plus the realpath of claude_dir
        # (where most files actually land).
        dst_real = _cached_realpath(str(dst_path))
        anchor_reals: List[str] = []
        for anchor in anchors:
            anchor_reals.append(str(anchor))
            anchor_reals.append(_cached_realpath(str(anchor)))
        # Also include claude_dir in case it's a symlink to elsewhere.
        anchor_reals.append(str(paths.claude_dir))
        anchor_reals.append(_cached_realpath(str(paths.claude_dir)))
        # R7 pass-6 H1: nested-subdir symlinks (e.g. ~/.claude/projects
        # → /srv/external) make dst_real resolve through the inner
        # symlink to a path that doesn't share a prefix with any anchor.
        # Walk the dst's parent chain and compute realpath at each level
        # — if ANY ancestor's realpath sits under an anchor_real, the
        # restore destination is contained via that ancestor.
        dst_obj = Path(dst_path)
        ancestor_reals: List[str] = []
        cursor = dst_obj
        for _ in range(64):  # cap to avoid pathological loops
            ancestor_reals.append(_cached_realpath(str(cursor)))
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        contained = False
        for anchor_real in anchor_reals:
            for candidate in (dst_real, *ancestor_reals):
                try:
                    common = os.path.commonpath([candidate, anchor_real])
                except (OSError, ValueError):
                    continue
                if os.path.normcase(common) == os.path.normcase(anchor_real):
                    contained = True
                    break
            if contained:
                break
        if not contained:
            records.append(
                ExecutionRecord(
                    key="restore_file",
                    status="error",
                    message="拒绝还原：目标路径 escapes anchors（疑似路径穿越攻击）。",
                )
            )
            errors += 1
            continue
        if dst_path.exists() and not overwrite:
            records.append(
                ExecutionRecord(
                    key="restore_file",
                    status="skipped",
                    message="目标已存在，未启用 overwrite；保持现状（%s）。" % dst_path.name,
                )
            )
            skipped += 1
            continue
        # NIT #15: explicit type-mismatch precheck so the user gets a
        # clear actionable error instead of a cryptic shutil.copy2 OSError
        # ("IsADirectoryError" / "NotADirectoryError"). The case below
        # only triggers with overwrite=True since we'd already short-
        # circuit otherwise.
        if dst_path.exists() and not src_path.is_symlink():
            if dst_path.is_dir() and src_path.is_file():
                records.append(
                    ExecutionRecord(
                        key="restore_file",
                        status="error",
                        message="目标是目录但源是文件（%s）；请先删除/移开后重试还原。" % dst_path.name,
                    )
                )
                errors += 1
                continue
            if dst_path.is_file() and src_path.is_dir():
                records.append(
                    ExecutionRecord(
                        key="restore_file",
                        status="error",
                        message="目标是文件但源是目录（%s）；请先删除/移开后重试还原。" % dst_path.name,
                    )
                )
                errors += 1
                continue
        if dry_run:
            records.append(
                ExecutionRecord(
                    key="restore_file",
                    status="dry-run",
                    message="演练：将还原 %s" % dst_path.name,
                )
            )
            continue
        try:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if src_path.is_symlink():
                try:
                    target = os.readlink(src_path)
                except OSError:
                    target = ""
                # Refuse to recreate a symlink whose resolved target
                # escapes the user's home / config_root. A crafted
                # backup tarball could otherwise drop a link like
                # ``~/.claude/projects → /etc/passwd`` so that cc's
                # next write goes to attacker-controlled paths.
                # Per-anchor allowlist mirrors _relative_under_anchors
                # (R10 pass-3 H1: include XDG parents too — without
                # them, a legit symlink whose target points under
                # ``XDG_DATA_HOME=/srv/share`` is rejected as path
                # traversal, breaking the R9 M2 XDG round-trip).
                allowed_anchors = [paths.home]
                if paths.config_root != paths.home:
                    allowed_anchors.append(paths.config_root)
                for xdg in (paths.xdg_data_claude.parent, paths.xdg_cache_claude.parent, paths.xdg_state_claude.parent):
                    try:
                        xdg.relative_to(paths.home)
                    except ValueError:
                        if xdg not in allowed_anchors:
                            allowed_anchors.append(xdg)
                target_safe = False
                if target:
                    try:
                        # Resolve relative to dst's parent because
                        # readlink may return a relative path.
                        if os.path.isabs(target):
                            resolved_target = os.path.realpath(target)
                        else:
                            resolved_target = os.path.realpath(
                                os.path.join(str(dst_path.parent), target)
                            )
                        # H1 fix: equality-on-commonpath, NOT prefix-string
                        # match. ``startswith`` falsely accepts siblings
                        # like ``/tmp/x/alice_evil/...`` when the anchor
                        # is ``/tmp/x/alice``. ``commonpath`` returns the
                        # longest path that is a parent of BOTH inputs,
                        # which equals the anchor only when the target
                        # is genuinely under it. ``normcase`` matches
                        # case-insensitive FS behaviour without breaking
                        # POSIX (it's the identity there).
                        for anchor in allowed_anchors:
                            try:
                                anchor_resolved = os.path.realpath(str(anchor))
                                common = os.path.commonpath(
                                    [resolved_target, anchor_resolved]
                                )
                            except ValueError:
                                # Different drives on Windows, or empty
                                # path edge case. Treat as unsafe.
                                continue
                            if os.path.normcase(common) == os.path.normcase(anchor_resolved):
                                target_safe = True
                                break
                    except OSError:
                        target_safe = False
                if not target_safe:
                    records.append(
                        ExecutionRecord(
                            key="restore_file",
                            status="error",
                            message=(
                                "拒绝恢复符号链接：链接目标指向 home / config_root 之外，"
                                "可能为路径穿越攻击。请人工核查后再操作。"
                            ),
                        )
                    )
                    errors += 1
                    continue
                # Pre-overwrite guard: previously we unconditionally
                # unlink dst_path here even when overwrite=False —
                # the early "exists() and not overwrite -> skip" check
                # short-circuits regular files but NOT broken symlinks
                # (exists() returns False for dangling links).
                if dst_path.is_symlink() or dst_path.exists():
                    if not overwrite:
                        records.append(
                            ExecutionRecord(
                                key="restore_file",
                                status="skipped",
                                message="目标符号链接已存在（%s），未启用 overwrite；保持现状。" % dst_path.name,
                            )
                        )
                        skipped += 1
                        continue
                    os.unlink(dst_path)
                # Pass-3 M2 + pass-4 M1: write the FULLY-RESOLVED
                # target by default to close the TOCTOU intermediate-
                # link-swap window. EXCEPTION: if the original was
                # relative AND the resolved target stays under the
                # SAME validated anchor as the dst, preserve the
                # relative form so portable links (`projects → ./shared`)
                # survive a host migration. This trade-off favors
                # security AND portability when both are achievable.
                emit_target = resolved_target
                if not os.path.isabs(target):
                    relative_link_root = str(dst_path.parent.resolve())
                    try:
                        common = os.path.commonpath([resolved_target, relative_link_root])
                    except ValueError:
                        common = ""
                    if os.path.normcase(common) == os.path.normcase(relative_link_root):
                        # Resolved target sits under dst's parent.
                        # Safe to keep the relative form — same
                        # validation outcome either way.
                        emit_target = target
                os.symlink(emit_target, dst_path)
            else:
                safe_copy2(src_path, dst_path)
            # Re-apply secure perms on sensitive restored files in case
            # the source backup was relaxed (user opened it, perms got
            # widened, etc.).
            _post_copy_lockdown(paths.home, dst_path)
            records.append(
                ExecutionRecord(
                    key="restore_file",
                    status="updated",
                    message="已还原到 %s。" % dst_path.name,
                    backup_path=str(src_path),
                )
            )
            restored += 1
        except OSError as exc:
            # Pass-7 M2: scrub absolute paths from error messages.
            records.append(
                ExecutionRecord(
                    key="restore_file",
                    status="error",
                    message="还原失败：%s" % type(exc).__name__,
                )
            )
            errors += 1

    summary_msg = "已还原 %d 个 / 跳过 %d 个 / 错误 %d 个。" % (restored, skipped, errors)
    records.append(
        ExecutionRecord(
            key="restore_summary",
            status="updated" if restored else "skipped",
            message=summary_msg,
        )
    )
    return ExecutionSummary(records=tuple(records), backup_root=str(backup_root))


def _is_windows_reparse_point(entry: "os.DirEntry[str]") -> bool:
    """True when ``entry`` is a Windows reparse point (junction/mount point).

    Junctions on NTFS are not symlinks (``entry.is_symlink()`` returns
    False for them) but they ARE reparse points. Walking into one can
    take us into an unrelated directory tree (or back into our own —
    a self-referential junction would loop). Detect via
    ``stat.FILE_ATTRIBUTE_REPARSE_POINT`` and skip on Windows.

    No-op on POSIX since reparse-point semantics don't exist there.
    """
    if os.name != "nt":
        return False
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False
    # ``FILE_ATTRIBUTE_REPARSE_POINT`` = 0x400 — defined in stdlib ``stat``
    # but only on Windows builds; importing unconditionally would fail on
    # POSIX so we use the literal value (it's part of the stable Win32 ABI).
    return bool(attrs & 0x400)


def _iter_backup_files(root: Path) -> Iterable[Path]:
    """Yield each regular file (or symlink) under ``root``.

    Excludes our own ``_cc_clean_meta.json`` from iteration — that file
    is metadata for restore anchor disambiguation, not user data. If we
    yielded it, restore would copy it back into the user's home dir.

    The exclusion uses a depth counter rather than ``Path.parent``
    equality. Reason: on Windows, ``long_path(root)`` adds a
    backslash-question-mark-backslash prefix and ``os.scandir``
    returns entries that inherit the prefix. The prefixed path's
    ``Path.parent`` does NOT compare equal to the un-prefixed root,
    so a parent-equality test silently fails to exclude the meta
    file on Windows long-path layouts. Tracking depth dodges all
    path-string normalization questions.
    """
    stack: List[Tuple[str, int]] = [(long_path(root), 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        # Sidecar lives only at the backup root (depth=0).
                        if depth == 0 and entry.name == _BACKUP_META_FILENAME:
                            continue
                        # Windows junctions / mount points: not symlinks
                        # but reparse points that could redirect us out
                        # of the backup tree (or back into it, looping).
                        if _is_windows_reparse_point(entry):
                            continue
                        candidate = Path(entry.path)
                        if entry.is_symlink():
                            yield candidate
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append((entry.path, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            yield candidate
                    except OSError:
                        continue
        except OSError:
            continue


_BACKUP_META_SUPPORTED_VERSIONS = (1,)


def _read_backup_metadata(backup_root: Path) -> Optional[Dict[str, object]]:
    """Read ``_cc_clean_meta.json`` from a backup root, if present.

    Rejects metadata whose ``version`` field is missing or unsupported
    by this build. Treating an unknown version as "no meta" lets
    restore fall back to the current ``paths`` anchors instead of
    misinterpreting a future schema. Future revisions can opt in by
    extending ``_BACKUP_META_SUPPORTED_VERSIONS``.
    """
    meta_path = backup_root / _BACKUP_META_FILENAME
    if not meta_path.is_file():
        return None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    raw_version = payload.get("version")
    try:
        version = int(raw_version)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if version not in _BACKUP_META_SUPPORTED_VERSIONS:
        return None
    return payload


@dataclass(frozen=True)
class PruneOutcome:
    """Result of :func:`prune_backup_roots`.

    ``removed`` is the list of roots fully deleted (rmtree returned 0).
    ``failed`` is roots whose rmtree raised — they may have been
    partially deleted, so the caller should surface the path so the
    user can finish manually.

    NIT #14: previous version silently swallowed OSError and dropped
    the partially-deleted root from the report, leaving the user with
    half-empty backup dirs and no way to know.
    """

    removed: Tuple[Path, ...]
    failed: Tuple[Tuple[Path, str], ...] = ()


def prune_backup_roots(paths: ClaudePaths, *, keep_last: int = 5) -> PruneOutcome:
    """Drop oldest backup roots once count exceeds ``keep_last``.

    Returns a :class:`PruneOutcome` so callers can report fully-removed
    AND partially-failed roots. ``keep_last <= 0`` is a no-op safeguard
    against truncating the entire backup history by accident.

    R7 pass-2 H2: holds the same cc-clean lock as ``execute_plan`` so
    concurrent runs can't have their backup_root rmtree'd mid-write
    by a parallel prune. Falls back to lockless mode if the lock can't
    be acquired (read-only mount, cross-user owner).
    """
    with _try_cc_clean_lock(paths):
        return _prune_backup_roots_locked(paths, keep_last=keep_last)


def _prune_backup_roots_locked(paths: ClaudePaths, *, keep_last: int = 5) -> PruneOutcome:
    if keep_last <= 0:
        return PruneOutcome(removed=(), failed=())
    roots = list_backup_roots(paths)
    if len(roots) <= keep_last:
        return PruneOutcome(removed=(), failed=())
    to_remove = roots[keep_last:]
    removed: List[Path] = []
    failed: List[Tuple[Path, str]] = []
    for old in to_remove:
        try:
            shutil.rmtree(long_path(old), ignore_errors=False)
            removed.append(old)
        except OSError as exc:
            # Pass-7 M1: ``str(exc)`` for OSError embeds the path.
            # Match pass-7 M2 scrubbing in execute_plan and surface
            # only the exception type so JSON consumers / logs don't
            # leak filesystem secrets.
            failed.append((old, type(exc).__name__))
    return PruneOutcome(removed=tuple(removed), failed=tuple(failed))


def _inspect_remove_path(target: CleanupTarget, selected: Set[str]) -> PlanItem:
    # Placeholder targets (e.g. ``auto_memory_override`` when no env/
    # settings redirect is set) report inapplicable so the user gets
    # an explicit "redirect not configured" hint rather than a silent
    # no-op when they --select an inert target.
    if target.target_path == _AUTO_MEMORY_TARGET_PLACEHOLDER:
        return PlanItem(
            target=target,
            selected=target.key in selected,
            exists=False,
            applicable=False,
            size_bytes=0,
            details="未检测到 auto-memory 重定向（CLAUDE_COWORK_MEMORY_PATH_OVERRIDE / settings.autoMemoryDirectory 未设置）。",
        )
    if target.target_path == _AUTO_MEMORY_TARGET_PLACEHOLDER + "/rejected":
        # User configured a redirect but cc's validator rejected it
        # (relative path, NUL byte, etc.). cc is using the default
        # location AND the user's intent failed silently — surface it.
        return PlanItem(
            target=target,
            selected=target.key in selected,
            exists=False,
            applicable=False,
            size_bytes=0,
            details=target.label,
            warnings=("用户设置了 auto-memory 重定向但被 cc 拒绝；请修正环境变量/settings.json 后重试。",),
        )
    if target.target_path == _PLUGIN_CACHE_REDIRECT_PLACEHOLDER:
        return PlanItem(
            target=target,
            selected=target.key in selected,
            exists=False,
            applicable=False,
            size_bytes=0,
            details="未设置 CLAUDE_CODE_PLUGIN_CACHE_DIR；插件缓存默认走 ~/.claude/plugins，由 plugins_dir 处理。",
        )
    if target.target_path == _REMOTE_MEMORY_REDIRECT_PLACEHOLDER:
        return PlanItem(
            target=target,
            selected=target.key in selected,
            exists=False,
            applicable=False,
            size_bytes=0,
            details="未设置 CLAUDE_CODE_REMOTE_MEMORY_DIR；agent-memory + auto-memory 走默认路径。",
        )
    path = Path(target.target_path)
    exists = path.exists()
    size_bytes = _path_size(path) if exists else 0
    details = "路径存在，可以执行清理。" if exists else "路径已不存在。"
    warnings: Tuple[str, ...] = ("该目标可能删除旧会话。",) if target.may_remove_sessions else ()
    # Symlink targets pointing OUTSIDE the home directory are likely the
    # user wiring ~/.claude/projects → /mnt/external/claude-data. Removing
    # would unlink the symlink (good) but ``shutil.rmtree`` follows the
    # symlink only if exposed via ``Path.is_dir`` — surface the warning
    # so the user can drop the selection if they actually want the link
    # itself replaced rather than the underlying data wiped.
    if exists and path.is_symlink():
        warnings = warnings + ("路径是符号链接；只会删除链接本身，不会动指向的目标。",)
    return PlanItem(
        target=target,
        selected=target.key in selected,
        exists=exists,
        applicable=exists,
        size_bytes=size_bytes,
        details=details,
        warnings=warnings,
    )


def _glob_matches(target: CleanupTarget) -> List[Path]:
    """Resolve all files matching this target's glob patterns."""
    parent = Path(target.target_path)
    if not parent.exists() or not parent.is_dir():
        return []
    matches: List[Path] = []
    for pattern in target.glob_patterns:
        try:
            for candidate in parent.glob(pattern):
                if candidate.is_file() or candidate.is_dir():
                    matches.append(candidate)
        except OSError:
            continue
    # Deduplicate while preserving order so backup mirror tree never
    # writes the same destination twice.
    seen: Set[str] = set()
    deduped: List[Path] = []
    for match in matches:
        key = str(match)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(match)
    return deduped


def _inspect_remove_glob(target: CleanupTarget, selected: Set[str]) -> PlanItem:
    matches = _glob_matches(target)
    exists = bool(matches)
    size_bytes = sum(_path_size(match) for match in matches)
    if matches:
        details = "命中 %d 个文件。" % len(matches)
    else:
        details = "没有匹配到任何文件。"
    return PlanItem(
        target=target,
        selected=target.key in selected,
        exists=exists,
        applicable=exists,
        size_bytes=size_bytes,
        details=details,
    )


def _inspect_json_fields(target: CleanupTarget, selected: Set[str]) -> PlanItem:
    path = Path(target.target_path)
    if not path.exists():
        return PlanItem(
            target=target,
            selected=target.key in selected,
            exists=False,
            applicable=False,
            size_bytes=0,
            details="文件不存在。",
        )

    payload, warnings = _load_json_dict(path)
    matches: List[str] = []
    deep_extra: Set[str] = set()
    if payload is not None:
        matches = [field for field in target.json_fields if field in payload]
        if target.deep_scrub:
            # Surface nested-only matches in the plan view so the user
            # doesn't see "字段已不存在" for a target that the executor
            # would actually find buried inside ``mcpServers``/``projects``.
            target_set = set(target.json_fields)
            deep_keys = set(STATE_PII_DEEP_KEYS)
            covered_top = set(matches)

            def visit(node: object) -> None:
                if isinstance(node, dict):
                    for key, value in node.items():
                        if (key in target_set and key not in covered_top) or key in deep_keys:
                            deep_extra.add(key)
                        visit(value)
                elif isinstance(node, list):
                    for child in node:
                        visit(child)

            visit(payload)

    if matches and deep_extra:
        details = "命中顶层字段：%s；嵌套层另发现：%s。" % (
            ", ".join(matches),
            ", ".join(sorted(deep_extra)),
        )
    elif matches:
        details = "命中字段：%s。" % ", ".join(matches)
    elif deep_extra:
        details = "顶层无字段，嵌套层发现：%s。" % ", ".join(sorted(deep_extra))
    else:
        details = "字段已不存在。"

    applicable = bool(matches) or bool(deep_extra)
    return PlanItem(
        target=target,
        selected=target.key in selected,
        exists=True,
        applicable=applicable,
        size_bytes=_path_size(path),
        details=details,
        warnings=tuple(warnings),
    )


def _state_files_for_glob(target: CleanupTarget) -> List[Path]:
    """Resolve glob_patterns against ``target_path`` parent dir to actual files."""
    parent = Path(target.target_path)
    if not parent.exists() or not parent.is_dir():
        return []
    matches: List[Path] = []
    seen: Set[str] = set()
    for pattern in target.glob_patterns:
        try:
            for candidate in parent.glob(pattern):
                if not candidate.is_file():
                    continue
                key = str(candidate)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(candidate)
        except OSError:
            continue
    return matches


def _inspect_json_fields_glob(target: CleanupTarget, selected: Set[str]) -> PlanItem:
    """Multi-file variant of ``_inspect_json_fields`` for glob targets.

    Aggregates applicability and details across every matching file so
    the planner reports the correct disposition even when the user has
    a non-prod cc build (e.g. only ``.claude-staging-oauth.json`` exists,
    not ``.claude.json``).
    """
    matches = _state_files_for_glob(target)
    if not matches:
        return PlanItem(
            target=target,
            selected=target.key in selected,
            exists=False,
            applicable=False,
            size_bytes=0,
            details="没有匹配到任何 cc 状态文件。",
        )

    applicable_count = 0
    total_size = 0
    matched_names: List[str] = []
    deep_extras: Set[str] = set()
    target_set = set(target.json_fields)
    deep_keys = set(STATE_PII_DEEP_KEYS) if target.deep_scrub else set()

    for path in matches:
        total_size += _path_size(path)
        payload, _ = _load_json_dict(path)
        if payload is None:
            continue
        top_hits = [field for field in target.json_fields if field in payload]
        local_extras: Set[str] = set()
        if target.deep_scrub:
            covered = set(top_hits)

            def visit(node: object) -> None:
                if isinstance(node, dict):
                    for key, value in node.items():
                        if (key in target_set and key not in covered) or key in deep_keys:
                            local_extras.add(key)
                        visit(value)
                elif isinstance(node, list):
                    for child in node:
                        visit(child)

            visit(payload)
        if top_hits or local_extras:
            applicable_count += 1
            matched_names.append(path.name)
            deep_extras.update(local_extras)

    if applicable_count == 0:
        details = "扫描了 %d 个 cc 状态文件，均无目标字段。" % len(matches)
    elif target.deep_scrub and deep_extras:
        details = "覆盖 %d/%d 个文件（%s）；嵌套层另发现：%s。" % (
            applicable_count,
            len(matches),
            ", ".join(matched_names),
            ", ".join(sorted(deep_extras)),
        )
    else:
        details = "覆盖 %d/%d 个文件（%s）。" % (
            applicable_count,
            len(matches),
            ", ".join(matched_names),
        )
    return PlanItem(
        target=target,
        selected=target.key in selected,
        exists=True,
        applicable=applicable_count > 0,
        size_bytes=total_size,
        details=details,
    )


def _inspect_purge_keychain(target: CleanupTarget, selected: Set[str]) -> PlanItem:
    """Plan-step inspector for the macOS keychain target.

    Only applicable on darwin with the ``security`` CLI on PATH. We don't
    actually probe the keychain (would prompt for the user's password and
    spam the planner) — the executor handles missing entries gracefully.
    """
    import sys as _sys

    if _sys.platform != "darwin":
        return PlanItem(
            target=target,
            selected=target.key in selected,
            exists=False,
            applicable=False,
            size_bytes=0,
            details="非 macOS 平台，跳过。",
        )
    if shutil.which("security") is None:
        return PlanItem(
            target=target,
            selected=target.key in selected,
            exists=True,
            applicable=False,
            size_bytes=0,
            details="找不到 macOS `security` 命令；无法清理 keychain。",
            warnings=("macOS 系统 PATH 缺少 security 命令。",),
        )
    return PlanItem(
        target=target,
        selected=target.key in selected,
        exists=True,
        applicable=True,
        size_bytes=0,
        details="将清理 cc 写入 keychain 的 oauth 凭据（含 legacy + suffix 两条）。",
        warnings=("keychain 删除不可备份；执行后无法回滚。",),
    )


def _resolve_scratchpad_root() -> Optional[Path]:
    """Locate cc's per-session scratchpad root.

    cc resolves the base via ``process.env.CLAUDE_CODE_TMPDIR ||
    tmpdir()`` (utils/permissions/filesystem.ts:333). Sandboxed CI
    workers and Cowork SDK set the env var to redirect cc's tmp.

    R8 pass-2 M1: cc DOES write a scratchpad on Windows too —
    ``getClaudeTempDirName()`` returns ``"claude"`` and the full path
    is ``tmpdir()/claude/`` (typically ``%LOCALAPPDATA%\\Temp\\claude``).
    Earlier audits assumed Windows had no scratchpad; that was wrong.
    POSIX uses ``${TMPDIR or /tmp}/claude-<uid>/``, Windows uses
    ``${CLAUDE_CODE_TMPDIR or %TEMP%}/claude/`` (no uid suffix —
    Windows already partitions %TEMP% per user via the profile dir).
    """
    import tempfile

    if os.name == "nt":
        base = os.environ.get("CLAUDE_CODE_TMPDIR") or tempfile.gettempdir()
        return Path(base) / "claude"
    try:
        uid = os.getuid()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None
    base = os.environ.get("CLAUDE_CODE_TMPDIR") or "/tmp"
    return Path(base) / ("claude-%d" % uid)


def _inspect_purge_scratchpad(target: CleanupTarget, selected: Set[str]) -> PlanItem:
    """Scratchpad cleanup planner.

    cc writes per-session scratchpads under
    ``${CLAUDE_CODE_TMPDIR || tmpdir()}/claude[-uid]/`` on both POSIX
    and Windows (see ``src/utils/permissions/filesystem.ts:307-347``).
    Normal exit cleans them; crashes leave residue.

    R8 pass-2 M1: previously we early-returned on Windows under the
    false belief that cc didn't write a scratchpad there. cc's
    ``getClaudeTempDirName() = 'claude'`` on win32 too — fixed.
    """
    scratch_root = _resolve_scratchpad_root()
    if scratch_root is None:
        return PlanItem(
            target=target,
            selected=target.key in selected,
            exists=False,
            applicable=False,
            size_bytes=0,
            details="无法读取 uid。",
        )
    if not scratch_root.exists():
        return PlanItem(
            target=target,
            selected=target.key in selected,
            exists=False,
            applicable=False,
            size_bytes=0,
            details="没有发现 %s（cc 未崩溃残留）。" % scratch_root,
        )
    size_bytes = _path_size(scratch_root)
    return PlanItem(
        target=target,
        selected=target.key in selected,
        exists=True,
        applicable=True,
        size_bytes=size_bytes,
        details="将删除 %s (%d 字节)。" % (scratch_root, size_bytes),
    )


def _inspect_settings_env(target: CleanupTarget, selected: Set[str]) -> PlanItem:
    path = Path(target.target_path)
    if not path.exists():
        return PlanItem(
            target=target,
            selected=target.key in selected,
            exists=False,
            applicable=False,
            size_bytes=0,
            details="settings.json 不存在。",
        )

    payload, warnings = _load_json_dict(path)
    env = payload.get("env") if isinstance(payload, dict) else None
    matches = [key for key in target.env_keys if isinstance(env, dict) and key in env]
    details = "命中环境变量：%s。" % ", ".join(matches) if matches else "没有匹配到鉴权环境变量。"
    return PlanItem(
        target=target,
        selected=target.key in selected,
        exists=True,
        applicable=bool(matches),
        size_bytes=_path_size(path),
        details=details,
        warnings=tuple(warnings),
    )


_BACKUP_META_FILENAME = "_cc_clean_meta.json"


def _ensure_backup_root(paths: ClaudePaths, current: Optional[Path], options: RunOptions) -> Optional[Path]:
    if not options.backup_enabled:
        return current
    if current is not None:
        return current
    # Microsecond + 4-char uuid suffix avoids collisions when CLI + TUI
    # run within the same wall-clock second (they don't today, but the
    # cost of widening is one extra path component, and it future-proofs
    # the layout for parallel cleanups).
    stamp = "%s-%s" % (datetime.now().strftime("%Y%m%d-%H%M%S-%f"), uuid.uuid4().hex[:4])
    backup_root = paths.backup_root_base / stamp
    backup_root.mkdir(parents=True, exist_ok=True)
    # Backup tree contains ``.credentials.json`` copies and historical
    # ``.claude.json`` snapshots — both leak auth tokens and PII if
    # group/world-readable. POSIX 0o700 keeps them owner-only; Windows
    # ignores chmod so we rely on per-user profile ACLs (NTFS default).
    if os.name != "nt":
        try:
            os.chmod(backup_root, 0o700)
        except OSError:
            pass
    # Drop a metadata file so restore can disambiguate which anchor each
    # relative path belongs to. Without this, a backup taken when
    # ``CLAUDE_CONFIG_DIR=/srv/x`` would restore to ``$HOME/.claude.json``
    # because restore would default to home as the anchor.
    meta = {
        "version": 1,
        "home": str(paths.home),
        "config_root": str(paths.config_root),
        "claude_dir": str(paths.claude_dir),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with atomic_write(backup_root / _BACKUP_META_FILENAME) as fh:
            fh.write(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        # Metadata is best-effort; the backup is still useful without it
        # (restore falls back to home anchor for old/missing meta).
        pass
    return backup_root


def _post_copy_lockdown(_home: Path, destination: Path) -> None:
    """Lock down freshly-mirrored backup files to owner-only on POSIX.

    The ``_home`` argument is retained for call-site signature
    stability (multiple call sites pass ``paths.home`` already) but
    is intentionally unused — the chmod walk is rooted at
    ``destination`` so passing ``home`` could only be misleading.
    A future caller could re-introduce a containment assert on
    ``destination`` being under ``home`` without changing call sites.

    Defense in depth: ``backup_root`` is already 0o700, but if the user
    later relaxes the dir permissions (e.g. ``chmod 0o755`` to peek at
    a backup over SSH) the inner files would inherit the wider mode
    bits from their source. ``~/.claude.json`` and ``settings.json``
    typically ship as 0o644 — wide enough that other users on a
    multi-tenant box could read PII once the dir is open.

    We chmod EVERY mirrored regular file to 0o600 (and dirs to 0o700)
    so the layered protection survives an accidental dir-perm bump.
    Best-effort: chmod failures are silently ignored — backup integrity
    matters more than perfect permissions, and many filesystems
    (e.g. SMB mounts, FAT) don't support POSIX chmod anyway.
    """
    if os.name == "nt":
        return
    try:
        if destination.is_symlink():
            return
        if destination.is_dir():
            # Pass-6 H2: don't early-return on root chmod failure.
            # Mixed mounts (root on FS that rejects chmod, children
            # on a different mount that accepts) need the walk to
            # continue so child chmods still apply. Each per-entry
            # chmod has its own except-OSError-continue so a futile
            # whole-tree walk on a uniformly-rejecting FS costs N
            # cheap syscalls — not a correctness issue.
            try:
                os.chmod(destination, 0o700)
            except OSError:
                pass
            for root, dirs, files in os.walk(destination):
                for d in dirs:
                    try:
                        os.chmod(os.path.join(root, d), 0o700)
                    except OSError:
                        continue
                for f in files:
                    fpath = os.path.join(root, f)
                    try:
                        if not os.path.islink(fpath):
                            os.chmod(fpath, 0o600)
                    except OSError:
                        continue
        elif destination.is_file():
            os.chmod(destination, 0o600)
    except OSError:
        pass


def _execute_remove_path(
    paths: ClaudePaths,
    path: Path,
    item: PlanItem,
    backup_root: Optional[Path],
    options: RunOptions,
) -> ExecutionRecord:
    if options.backup_enabled:
        if backup_root is None:
            raise RuntimeError("backup root was not created")
        destination = _backup_destination(paths, backup_root, path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        warning = _move_or_copy_then_delete(path, destination)
        _post_copy_lockdown(paths.home, destination)
        message = "已移入备份，并从原位置移除。"
        if warning:
            message += " 警告：%s。" % warning
        return ExecutionRecord(
            key=item.target.key,
            status="moved",
            message=message,
            backup_path=str(destination),
        )

    _remove_with_retry(path)
    return ExecutionRecord(
        key=item.target.key,
        status="deleted",
        message="未备份，已直接删除。",
    )


def _move_or_copy_then_delete(src: Path, dst: Path) -> Optional[str]:
    """Backup-then-remove that survives cross-filesystem source/dest layouts.

    ``shutil.move`` falls back to copy-then-unlink across filesystems, but
    that fallback is NOT atomic across a SIGKILL — the source and the
    destination can both be partially populated. We force the explicit
    sequence: copy fully, verify size, then unlink. A crash between copy
    and unlink leaves a complete backup AND the original — far better
    than a torn move.

    Same-filesystem moves still go through ``shutil.move`` so the rename
    syscall stays atomic.

    Returns ``None`` on a complete (lossless) backup, or a Chinese
    warning string when the symlink mirror could not be reproduced
    (Windows without dev mode, etc.) — caller is expected to surface
    that via the ExecutionRecord message so the user knows the backup
    won't fully round-trip on restore.
    """
    if src.is_symlink():
        # Don't follow symlinks: dump the link itself into the backup
        # tree so the user can put it back, then unlink the link without
        # touching the target. ``os.readlink`` returns the literal
        # target string, which is what ``os.symlink`` expects.
        try:
            link_target = os.readlink(src)
        except OSError:
            link_target = ""
        dst.parent.mkdir(parents=True, exist_ok=True)
        warning: Optional[str] = None
        symlink_mirrored = False
        if link_target:
            try:
                if dst.exists() or dst.is_symlink():
                    os.unlink(dst)
                os.symlink(link_target, dst)
                symlink_mirrored = True
            except OSError as exc:
                # Symlink mirror failed (Windows without dev mode, target FS
                # without symlink support, ...). Fall through to plain unlink
                # of the source. Surface a warning so the caller's record
                # tells the user the backup is incomplete.
                # R7 pass-4 L2: don't leak link_target (could be absolute
                # path) — exception class name is enough.
                warning = (
                    "符号链接未能在备份中重现（%s）；源已删除但 restore 时不会自动恢复链接。"
                    % exc.__class__.__name__
                )
        else:
            warning = "无法读取符号链接的目标，备份未保留链接信息"
        os.unlink(src)
        if not symlink_mirrored and warning is None:
            warning = "符号链接信息未在备份中保留"
        return warning

    same_fs = _same_filesystem(src, dst.parent if dst.parent.exists() else dst.parent.parent)
    if same_fs:
        _move_with_retry(src, dst)
        return None

    # Cross-filesystem: copy first, verify, then remove.
    if src.is_dir():
        shutil.copytree(long_path(src), long_path(dst), symlinks=True, dirs_exist_ok=False)
    else:
        safe_copy2(src, dst)

    # Bypass the LRU cache here. ``_path_size`` is called per TUI
    # render and may have cached an older src total; using that for
    # post-copy verification could mask a concurrent cc write that
    # extended src between cache fill and copy. Live ``_scandir_size``
    # / direct stat reads catch the actual on-disk byte counts.
    if src.is_file():
        try:
            src_size = src.stat().st_size
        except OSError:
            src_size = 0
        try:
            dst_size = dst.stat().st_size
        except OSError:
            dst_size = 0
    else:
        src_size = _scandir_size(src)
        dst_size = _scandir_size(dst)
    if dst_size < src_size:
        raise RuntimeError(
            "跨文件系统备份校验失败：源 %d 字节 / 备份 %d 字节" % (src_size, dst_size)
        )
    _remove_with_retry(src)
    return None


def _scrub_payload_fields(
    payload: Dict[str, object],
    fields: Sequence[str],
    *,
    deep: bool,
) -> int:
    """Strip ``fields`` from ``payload`` (top-level always; recursive when ``deep``).

    Returns the number of removals so the caller can report whether the
    target produced any actual change. Deep-scrub also strips the keys in
    ``STATE_PII_DEEP_KEYS`` wherever they appear so secrets cc places
    inside ``mcpServers.<name>.headers.Authorization`` etc. are caught.
    """
    removed = 0
    target_set = set(fields)
    deep_extra = set(STATE_PII_DEEP_KEYS) if deep else set()

    def visit(node: object) -> None:
        nonlocal removed
        if isinstance(node, dict):
            for key in list(node.keys()):
                if key in target_set or key in deep_extra:
                    node.pop(key, None)
                    removed += 1
                    continue
                visit(node[key])
        elif isinstance(node, list):
            for child in node:
                visit(child)

    if deep:
        visit(payload)
    else:
        for field in fields:
            if field in payload:
                payload.pop(field, None)
                removed += 1
    return removed


def _execute_scrub_json_fields(
    paths: ClaudePaths,
    path: Path,
    item: PlanItem,
    backup_root: Optional[Path],
    options: RunOptions,
) -> ExecutionRecord:
    payload, _ = _load_json_dict(path)
    if payload is None:
        raise RuntimeError("无法解析 JSON：%s" % path)
    backup_path = None
    if options.backup_enabled:
        if backup_root is None:
            raise RuntimeError("backup root was not created")
        backup_path = _backup_file_copy(paths, backup_root, path)
    removed = _scrub_payload_fields(payload, item.target.json_fields, deep=item.target.deep_scrub)
    _write_json(path, payload)
    if removed == 0:
        message = "字段已不存在，未发生变更。"
    else:
        suffix = "（含嵌套）" if item.target.deep_scrub else ""
        message = "已移除 %d 处字段%s：%s。" % (removed, suffix, ", ".join(item.target.json_fields))
    return ExecutionRecord(
        key=item.target.key,
        status="updated",
        message=message,
        backup_path=str(backup_path) if backup_path is not None else None,
    )


def _execute_scrub_json_fields_glob(
    paths: ClaudePaths,
    item: PlanItem,
    backup_root: Optional[Path],
    options: RunOptions,
) -> ExecutionRecord:
    """Multi-file json scrub for glob targets (covers oauth suffix variants)."""
    matches = _state_files_for_glob(item.target)
    if not matches:
        return ExecutionRecord(
            key=item.target.key,
            status="skipped",
            message="没有匹配到任何 cc 状态文件。",
        )
    if options.backup_enabled and backup_root is None:
        raise RuntimeError("backup root was not created")

    total_removed = 0
    files_touched = 0
    backup_paths: List[str] = []
    file_errors: List[str] = []
    for path in matches:
        payload, _ = _load_json_dict(path)
        if payload is None:
            file_errors.append(path.name)
            continue
        backup_path: Optional[Path] = None
        if options.backup_enabled:
            backup_path = _backup_file_copy(paths, backup_root, path)  # type: ignore[arg-type]
            backup_paths.append(str(backup_path))
        removed = _scrub_payload_fields(payload, item.target.json_fields, deep=item.target.deep_scrub)
        if removed > 0:
            files_touched += 1
            total_removed += removed
            _write_json(path, payload)

    if file_errors:
        suffix = "（部分文件 JSON 解析失败：%s）" % ", ".join(file_errors)
    else:
        suffix = ""
    if total_removed == 0:
        message = "扫描了 %d 个文件，均无目标字段%s。" % (len(matches), suffix)
        status = "skipped"
    else:
        depth_hint = "（含嵌套）" if item.target.deep_scrub else ""
        message = "在 %d 个文件中移除 %d 处字段%s%s。" % (
            files_touched,
            total_removed,
            depth_hint,
            suffix,
        )
        status = "updated"

    aggregated_backup_path: Optional[str] = None
    if backup_paths:
        aggregated_backup_path = backup_paths[0] if len(backup_paths) == 1 else "; ".join(backup_paths)
    return ExecutionRecord(
        key=item.target.key,
        status=status,
        message=message,
        backup_path=aggregated_backup_path,
    )


def _enumerate_keychain_service_names(paths: ClaudePaths) -> Tuple[str, ...]:
    """All cc keychain service names that could exist on this machine.

    Mirrors cc's ``getMacOsKeychainStorageServiceName`` from
    ``src/utils/secureStorage/macOsKeychainHelpers.ts:29-41``::

        return `Claude Code${OAUTH_FILE_SUFFIX}${serviceSuffix}${dirHash}`

    Where:
    * ``OAUTH_FILE_SUFFIX`` is one of ``''``/``-staging``/``-local``/
      ``-custom`` based on the cc build's oauth config.
    * ``serviceSuffix`` is ``''`` (legacy API-key entry) or
      ``-credentials`` (oauth token entry).
    * ``dirHash`` is empty for the default ``~/.claude`` data dir,
      else ``-<sha256(config_dir)[:8]>``.

    We enumerate every plausible combination and let the executor try
    each — non-existent entries are a benign rc=44 from
    ``security delete-generic-password``.
    """
    from hashlib import sha256

    oauth_variants = ("", "-staging", "-local", "-custom")
    suffix_variants = ("", "-credentials")
    dir_hashes = [""]
    # cc gates the hash on ``isDefaultDir = !process.env.CLAUDE_CONFIG_DIR``
    # — i.e. the hash exists IFF the env var was set, regardless of
    # whether its VALUE happens to equal the legacy default.
    # Primary signal: env var presence. Fallback: claude_dir diverges
    # from the legacy default (covers callers using ``default_paths``
    # in tests without env access).
    env_set = bool(os.environ.get("CLAUDE_CONFIG_DIR"))
    legacy_default = paths.home / ".claude"
    if env_set or paths.claude_dir != legacy_default:
        digest = sha256(str(paths.claude_dir).encode("utf-8")).hexdigest()[:8]
        dir_hashes.append("-" + digest)

    services: List[str] = []
    for oauth in oauth_variants:
        for suffix in suffix_variants:
            for dh in dir_hashes:
                name = "Claude Code%s%s%s" % (oauth, suffix, dh)
                if name not in services:
                    services.append(name)
    return tuple(services)


def _execute_purge_keychain(paths: ClaudePaths, item: PlanItem) -> ExecutionRecord:
    """Drop cc's oauth credentials from the macOS keychain.

    Enumerates every plausible service name (oauth flavor × credentials
    suffix × CLAUDE_CONFIG_DIR hash) and runs
    ``security delete-generic-password -s <name>`` for each. Missing
    entries return rc=44 / "could not be found" — counted as no-ops
    rather than errors.
    """
    import subprocess
    import sys as _sys

    if _sys.platform != "darwin":
        return ExecutionRecord(
            key=item.target.key,
            status="skipped",
            message="非 macOS 平台，跳过 keychain 清理。",
        )
    security = shutil.which("security")
    if security is None:
        return ExecutionRecord(
            key=item.target.key,
            status="error",
            message="找不到 `security` 命令；无法清理 keychain。",
        )

    service_names = _enumerate_keychain_service_names(paths)
    deleted_services: List[str] = []
    errored_services: List[str] = []
    missing_count = 0
    locked_detected = False
    for service in service_names:
        try:
            result = subprocess.run(
                [security, "delete-generic-password", "-s", service],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            # R7 pass-9 H1: don't include str(exc) (TimeoutExpired
            # embeds the security binary path; OSError embeds errno
            # location). Service name + exception type only.
            errored_services.append("%s (%s)" % (service, type(exc).__name__))
            continue
        stderr_lower = (result.stderr or "").lower()
        # rc=51 / "user interaction is not allowed" surfaces when the
        # keychain is locked. Bail early with a clear unlock hint —
        # if rc=51 fires once it'll fire 16 more times, no point
        # spamming the user with identical errors.
        if result.returncode == 51 or "user interaction is not allowed" in stderr_lower:
            locked_detected = True
            break
        if result.returncode == 0:
            deleted_services.append(service)
        elif "could not be found" in stderr_lower or result.returncode == 44:
            missing_count += 1
        else:
            stderr = (result.stderr or "").strip() or "rc=%d" % result.returncode
            errored_services.append("%s: %s" % (service, stderr))

    if locked_detected:
        # Surfaced as ``error`` so JSON consumers see non-zero status,
        # but message tells humans the actionable next step.
        return ExecutionRecord(
            key=item.target.key,
            status="error",
            message=(
                "macOS keychain 当前处于锁定状态，无法删除条目。"
                "请先在终端执行 `security unlock-keychain ~/Library/Keychains/login.keychain-db`"
                "（或 GUI 解锁）后再次运行 cc-clean。"
            ),
        )

    if errored_services and not deleted_services:
        # R7 pass-8 L1: errored_services entries embed `security` stderr
        # which often contains keychain DB absolute paths. Surface only
        # the count + first error type to avoid leaking those paths.
        return ExecutionRecord(
            key=item.target.key,
            status="error",
            message="keychain 删除失败：%d 个服务名变体出错（请运行 `security` 命令排查）。" % len(errored_services),
        )
    if not deleted_services:
        return ExecutionRecord(
            key=item.target.key,
            status="skipped",
            message="扫描了 %d 个服务名变体，keychain 中没有匹配的 cc 凭据条目。" % len(service_names),
        )
    msg_parts = ["已删除：%s" % ", ".join(deleted_services)]
    if missing_count:
        msg_parts.append("无需删除：%d 个变体" % missing_count)
    if errored_services:
        # R7 pass-9 H1: mixed-branch (some deleted, some errored) was
        # leaking `security` stderr text — same path leak class as the
        # error-only branch fixed in pass-8 L1. Surface only the count.
        msg_parts.append("失败：%d 个变体（详情请运行 `security` 排查）" % len(errored_services))
    return ExecutionRecord(
        key=item.target.key,
        status="deleted",
        message="；".join(msg_parts),
    )


def _execute_purge_scratchpad(
    paths: ClaudePaths,
    item: PlanItem,
    backup_root: Optional[Path],
    options: RunOptions,
) -> ExecutionRecord:
    """Wipe the per-uid scratchpad root in /tmp."""
    # R8 pass-2 M1: drop the Windows early-skip; cc DOES write a
    # ``${TMPDIR}/claude/`` dir on Windows.
    scratch_root = _resolve_scratchpad_root()
    if scratch_root is None:
        return ExecutionRecord(
            key=item.target.key,
            status="error",
            message="无法读取 uid。",
        )
    if not scratch_root.exists():
        return ExecutionRecord(
            key=item.target.key,
            status="skipped",
            message="没有发现 %s。" % scratch_root,
        )
    if options.backup_enabled:
        if backup_root is None:
            raise RuntimeError("backup root was not created")
        destination = _backup_destination(paths, backup_root, scratch_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        warning = _move_or_copy_then_delete(scratch_root, destination)
        message = "已移入备份并从 %s 移除。" % scratch_root
        if warning:
            message += " 警告：%s。" % warning
        return ExecutionRecord(
            key=item.target.key,
            status="moved",
            message=message,
            backup_path=str(destination),
        )
    _remove_with_retry(scratch_root)
    # R7 pass-10 LOW-2: drop the literal scratch_root from message
    # for cross-cutting path-scrub consistency. The path is well-known
    # (/tmp/claude-<uid>) but uniformity matters.
    return ExecutionRecord(
        key=item.target.key,
        status="deleted",
        message="未备份，已直接删除 scratchpad 目录。",
    )


def _execute_remove_glob(
    paths: ClaudePaths,
    item: PlanItem,
    backup_root: Optional[Path],
    options: RunOptions,
) -> ExecutionRecord:
    matches = _glob_matches(item.target)
    if not matches:
        return ExecutionRecord(
            key=item.target.key,
            status="skipped",
            message="没有匹配到任何文件。",
        )

    if options.backup_enabled and backup_root is None:
        raise RuntimeError("backup root was not created")

    backed_up = 0
    deleted = 0
    backup_destinations: List[str] = []
    glob_warnings: List[str] = []
    for match in matches:
        if options.backup_enabled:
            destination = _backup_destination(paths, backup_root, match)
            destination.parent.mkdir(parents=True, exist_ok=True)
            warning = _move_or_copy_then_delete(match, destination)
            if warning:
                glob_warnings.append("%s: %s" % (match.name, warning))
            _post_copy_lockdown(paths.home, destination)
            backup_destinations.append(str(destination))
            backed_up += 1
        else:
            _remove_with_retry(match)
            deleted += 1

    status = "moved" if options.backup_enabled else "deleted"
    if options.backup_enabled:
        message = "已移入备份并从原位置移除 %d 个文件。" % backed_up
        if glob_warnings:
            message += " 警告：%s。" % "; ".join(glob_warnings)
    else:
        message = "未备份，已直接删除 %d 个文件。" % deleted
    # Multi-match glob targets join all backup destinations so the
    # user (and JSON consumers) can audit every byte we relocated.
    aggregated_backup_path: Optional[str] = None
    if backup_destinations:
        aggregated_backup_path = (
            backup_destinations[0]
            if len(backup_destinations) == 1
            else "; ".join(backup_destinations)
        )
    return ExecutionRecord(
        key=item.target.key,
        status=status,
        message=message,
        backup_path=aggregated_backup_path,
    )


def _execute_scrub_settings_env(
    paths: ClaudePaths,
    path: Path,
    item: PlanItem,
    backup_root: Optional[Path],
    options: RunOptions,
) -> ExecutionRecord:
    payload, _ = _load_json_dict(path)
    if payload is None:
        # JSON parse failure during execute window — usually a race
        # with the user editing settings.json. Skip cleanly so a
        # batch run continues; the user can re-target later.
        return ExecutionRecord(
            key=item.target.key,
            status="skipped",
            message="settings.json 解析失败（可能正在编辑）。",
        )
    env = payload.get("env")
    if not isinstance(env, dict):
        # Planner saw env when inspect ran; user removed it before
        # execute fires. Skip rather than crash so JSON-mode batch
        # runs don't abort with status=error on a benign race.
        return ExecutionRecord(
            key=item.target.key,
            status="skipped",
            message="settings.json 中已无 env 对象（可能用户刚编辑过），跳过。",
        )

    keys_present = [key for key in item.target.env_keys if key in env]
    if not keys_present:
        # Same race: env exists but our target keys disappeared since
        # planning. Skip without writing a no-op backup.
        return ExecutionRecord(
            key=item.target.key,
            status="skipped",
            message="目标环境变量在 settings.json 中已不存在。",
        )

    backup_path = None
    if options.backup_enabled:
        if backup_root is None:
            raise RuntimeError("backup root was not created")
        backup_path = _backup_file_copy(paths, backup_root, path)

    for key in keys_present:
        env.pop(key, None)
    if not env:
        payload.pop("env", None)
    _write_json(path, payload)
    return ExecutionRecord(
        key=item.target.key,
        status="updated",
        message="已移除 settings 环境变量：%s。" % ", ".join(keys_present),
        backup_path=str(backup_path) if backup_path is not None else None,
    )


# (path_str, mtime_ns, size) → (parsed_dict_OR_None, warnings_list, parse_failed_bool).
#
# TUI rebuilds plan on every keypress; ``_inspect_json_fields_glob`` /
# ``_inspect_settings_env`` re-parse multi-MB ``~/.claude.json`` files on
# every rebuild without this. Keying on (path, mtime_ns, size) catches
# in-place edits even when only mtime updates AND in-place rewrites that
# preserve mtime but change length. Cache miss falls through to live read.
#
# Parsed payloads are stored as JSON strings then re-parsed per call so
# downstream mutations (popping fields during scrub) never bleed back
# into the cache. Mutating a cached dict directly would let one scrub
# affect the next plan rebuild's view.
_JSON_PARSE_CACHE: "OrderedDict[Tuple[str, int, int], Tuple[Optional[str], Tuple[str, ...], bool]]" = OrderedDict()
_JSON_PARSE_CACHE_LOCK = threading.Lock()
_JSON_PARSE_CACHE_MAX = 16


def _load_json_dict(path: Path) -> Tuple[Optional[Dict[str, object]], List[str]]:
    warnings: List[str] = []
    try:
        stat_result = path.stat()
    except OSError:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            # Pass-7 M3: scrub absolute paths from JSON-mode warnings.
            warnings.append("JSON 读取失败：%s" % type(exc).__name__)
            return None, warnings
        return _parse_uncached(text, warnings)
    cache_key = (str(path), stat_result.st_mtime_ns, stat_result.st_size)
    with _JSON_PARSE_CACHE_LOCK:
        cached = _JSON_PARSE_CACHE.get(cache_key)
        if cached is not None:
            _JSON_PARSE_CACHE.move_to_end(cache_key)
    if cached is not None:
        cached_text, cached_warnings, parse_failed = cached
        warnings_local = list(cached_warnings)
        if parse_failed or cached_text is None:
            return None, warnings_local
        try:
            payload = json.loads(cached_text)
        except Exception:
            # Cache entry corrupted; fall through to live read.
            pass
        else:
            if isinstance(payload, dict):
                return payload, warnings_local
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        # Pass-7 M3: scrub absolute paths.
        warnings.append("JSON 读取失败：%s" % type(exc).__name__)
        return None, warnings
    payload, warnings = _parse_uncached(text, warnings)
    parse_failed = payload is None
    cached_text: Optional[str] = None if parse_failed else text
    with _JSON_PARSE_CACHE_LOCK:
        _JSON_PARSE_CACHE[cache_key] = (cached_text, tuple(warnings), parse_failed)
        _JSON_PARSE_CACHE.move_to_end(cache_key)
        while len(_JSON_PARSE_CACHE) > _JSON_PARSE_CACHE_MAX:
            _JSON_PARSE_CACHE.popitem(last=False)
    return payload, warnings


def _parse_uncached(text: str, warnings: List[str]) -> Tuple[Optional[Dict[str, object]], List[str]]:
    try:
        payload = json.loads(text)
    except Exception as exc:
        # Pass-7 M3: JSONDecodeError's __str__ may include line/col
        # context that's safe; type name is consistent with the
        # rest of the scrubbing policy.
        warnings.append("JSON 解析失败：%s" % type(exc).__name__)
        return None, warnings
    if not isinstance(payload, dict):
        warnings.append("JSON 根节点不是对象。")
        return None, warnings
    return payload, warnings


def _write_json(path: Path, payload: Dict[str, object]) -> None:
    """Atomic JSON write — survives crash/SIGKILL/AC pull mid-write.

    The previous direct ``path.write_text`` could leave ``~/.claude.json`` or
    ``settings.json`` in a half-written state if the process was interrupted
    after truncate but before the full payload landed. ``atomic_write``
    funnels the payload through a tempfile + ``os.replace`` so observers
    only ever see the old content or the new content, never a torn write.

    ``newline=""`` keeps Python from translating ``\\n`` → ``\\r\\n`` on
    Windows, matching how Claude Code's own writers emit these files.
    """
    serialized = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
    with atomic_write(path) as fh:
        fh.write(serialized)


# (path_str, top_mtime_ns, child_mtime_signature) → cached total size.
#
# The earlier cache only watched the top-level mtime. That misses writes
# under ``~/.claude/projects/<X>/foo.jsonl`` because adding a file under
# ``X/`` bumps ``X``'s mtime, not ``projects_dir``'s — so the cached size
# stayed stale on every per-project rollout write. The new key folds in
# a lightweight signature of immediate-child mtimes (one stat per direct
# child, no recursion) so adding/removing files in any first-level
# subdirectory invalidates without forcing a full re-walk per render.
#
# An ``OrderedDict`` LRU replaces the old "drop everything at 64" rule:
# overflow now evicts the single oldest entry, so a working set just
# above the cap doesn't thrash to 0% hit rate.
_PATH_SIZE_CACHE_MAX = 64
_PATH_SIZE_CACHE: "OrderedDict[Tuple[str, int, int], int]" = OrderedDict()
_PATH_SIZE_CACHE_LOCK = threading.Lock()


def _child_mtime_signature(path: Path) -> int:
    """One-level mtime aggregate so subdir-only writes still invalidate.

    ``os.scandir`` returns ``DirEntry`` objects that already cache their
    stat; we don't need to recurse since a child's ``mtime`` bumps when
    files are added to or removed from it. A simple sum is enough — we
    only need a value that changes when the underlying tree changes.
    """
    total = 0
    try:
        with os.scandir(long_path(path)) as it:
            for entry in it:
                try:
                    total += entry.stat(follow_symlinks=False).st_mtime_ns
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _scandir_size(path: Path) -> int:
    """Recursive directory size via ``os.scandir`` (faster than rglob).

    Skips symlinks (``follow_symlinks=False``) so an out-of-tree symlink
    target — e.g. ``~/.claude/projects`` linked into a multi-TB scratch
    disk — does NOT explode the size column. Caller is the TUI so the
    walk is best-effort: any per-entry OSError is swallowed and the
    accumulated total returned.
    """
    total = 0
    stack: List[str] = [long_path(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        # Windows junctions can otherwise sum bytes from
                        # an unrelated tree (or loop on a self-pointing
                        # junction). ``follow_symlinks=False`` handles
                        # symlinks but NOT reparse points — guard here.
                        if _is_windows_reparse_point(entry):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _path_size(path: Path) -> int:
    """Return total on-disk size of ``path`` (file OR recursive directory).

    Claude's TUI rebuilds the plan on EVERY keypress that toggles a target
    (Space/Enter/1-9/a/f/n/b/d). Each rebuild calls ``_path_size`` for 8+
    separate targets, and ``projects_dir`` / ``sessions_dir`` can easily
    contain thousands of Claude Code rollout files. Without caching, each
    toggle triggered a full directory walk per target — on a loaded home
    directory that meant a visibly laggy TUI.

    Cache key is ``(str(path), st_mtime_ns, child_mtime_signature)``:
    * top-level mtime catches direct-child add/remove
    * child mtime sum catches **deeper** writes (e.g. cc rolling out a
      new ``~/.claude/projects/<cwd>/<file>`` — the parent ``projects/``
      mtime DOES NOT change, but the immediate child ``<cwd>/`` does).

    Cache mutations are wrapped in ``_PATH_SIZE_CACHE_LOCK``; the walk
    itself runs outside the lock so cache reads don't block on slow I/O.
    """
    # NIT #11: check is_symlink BEFORE exists() so dangling symlinks
    # report their own (small) lstat size rather than 0. The previous
    # ``exists()`` short-circuit followed the link, returning False for
    # dangling targets and skipping the symlink-size branch entirely.
    if path.is_symlink():
        # Don't follow: keep the size column honest about what cleanup
        # would actually delete. Removing a symlink only frees the bytes
        # of the link itself, not the target.
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    try:
        top_mtime_ns = path.stat().st_mtime_ns
    except OSError:
        top_mtime_ns = 0
    # R8 pass-1 M4 perf: try a cheap lookup keyed only on top mtime
    # FIRST. Computing ``_child_mtime_signature`` walks every immediate
    # child stat and is the dominant cost for large ``projects_dir``.
    # If any cache key with the same (path, top_mtime, *) hits we can
    # skip the child walk entirely.
    with _PATH_SIZE_CACHE_LOCK:
        for k, v in reversed(_PATH_SIZE_CACHE.items()):
            if k[0] == str(path) and k[1] == top_mtime_ns:
                _PATH_SIZE_CACHE.move_to_end(k)
                return v
    # Top mtime miss → child churn possible; compute the full signature.
    child_sig = _child_mtime_signature(path)
    cache_key = (str(path), top_mtime_ns, child_sig)

    with _PATH_SIZE_CACHE_LOCK:
        cached = _PATH_SIZE_CACHE.get(cache_key)
        if cached is not None:
            _PATH_SIZE_CACHE.move_to_end(cache_key)
            return cached

    total = _scandir_size(path)

    with _PATH_SIZE_CACHE_LOCK:
        _PATH_SIZE_CACHE[cache_key] = total
        _PATH_SIZE_CACHE.move_to_end(cache_key)
        # Evict the single oldest entry instead of clearing — a working
        # set slightly above the cap should still see most hits.
        while len(_PATH_SIZE_CACHE) > _PATH_SIZE_CACHE_MAX:
            _PATH_SIZE_CACHE.popitem(last=False)
    return total


_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {"COM%d" % i for i in range(1, 10)}
    | {"LPT%d" % i for i in range(1, 10)}
)


def _sanitize_windows_reserved(relative: Path) -> Path:
    """Rename basename collisions with Windows reserved names.

    Linux/WSL filesystems happily host directories named ``CON`` /
    ``PRN`` etc. When such a project gets backed up to a Windows
    host (or to a Windows-mounted drive), the reserved name causes
    ``CreateFileW`` to fail with ENOENT/ENAMETOOLONG even via the
    ``\\?\\`` long-path prefix. Suffix the offending segment with
    ``_reserved`` so the backup tree round-trips on every host.
    POSIX no-op since these names work fine there.
    """
    if os.name != "nt":
        return relative
    parts = list(relative.parts)
    changed = False
    for idx, part in enumerate(parts):
        # Reserved-name match is case-insensitive and ignores trailing
        # dot/extension on Windows.
        stem = part.split(".", 1)[0]
        if stem.upper() in _WINDOWS_RESERVED_BASENAMES:
            parts[idx] = part + "_reserved"
            changed = True
    if not changed:
        return relative
    # ``Path(*parts)`` reads ``os.name`` AT CONSTRUCTION to dispatch
    # PosixPath vs WindowsPath. Tests that patch ``services.os.name``
    # to "nt" on a POSIX runner trip ``NotImplementedError: cannot
    # instantiate 'WindowsPath' on your system``. Reuse the input's
    # concrete class so mocking the os.name reference here doesn't
    # leak into pathlib's class-selection logic.
    return type(relative)(*parts)


_BACKUP_DESTINATION_SUFFIX_CAP = 10_000


def _backup_destination(paths: ClaudePaths, backup_root: Path, source: Path) -> Path:
    relative = _relative_under_anchors(paths, source)
    relative = _sanitize_windows_reserved(relative)
    candidate = backup_root / relative
    if not candidate.exists():
        return candidate
    # Cap the suffix loop so a pathological FS that keeps reporting
    # ``exists()`` truthy doesn't hang us forever. 10k variants is
    # already wildly more than any legitimate collision count.
    for suffix in range(1, _BACKUP_DESTINATION_SUFFIX_CAP + 1):
        replacement = candidate.parent / ("%s.%d" % (candidate.name, suffix))
        if not replacement.exists():
            return replacement
    raise RuntimeError(
        "无法为备份目标找到唯一名称（已尝试 %d 个后缀）：%s"
        % (_BACKUP_DESTINATION_SUFFIX_CAP, candidate)
    )


def _backup_file_copy(paths: ClaudePaths, backup_root: Path, source: Path) -> Path:
    destination = _backup_destination(paths, backup_root, source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # safe_copy2 applies the \\?\ long-path prefix on Windows so backups of
    # deeply nested project files (>260 chars) don't fail with ENAMETOOLONG.
    safe_copy2(source, destination)
    # Defense in depth: lock the freshly-copied file to 0o600 on POSIX
    # so even a manually-loosened backup_root doesn't leak PII (state
    # files, settings, all may carry user identifiers).
    _post_copy_lockdown(paths.home, destination)
    return destination


def _relative_under_anchors(paths: ClaudePaths, source: Path) -> Path:
    """Strip a known anchor from ``source`` for the backup mirror tree.

    Anchor priority: ``home``, ``config_root`` (when differs), then any
    XDG redirect set via env (data / cache / state) when those parent
    dirs sit outside ``home``. Without the XDG entries, env-redirected
    XDG_DATA_HOME=/srv/share fell through to ``external/...`` and
    couldn't round-trip through restore (R9 M2).

    R10 M2: anchors are sorted by descending path length BEFORE the
    match so the most-specific anchor wins. Without this, overlapping
    custom XDG roots like ``XDG_DATA_HOME=/srv`` +
    ``XDG_CACHE_HOME=/srv/cache`` would have ``/srv`` (parent) match
    first for cache files, producing relative ``cache/claude/x``
    instead of ``claude/x`` and breaking restore-tree symmetry on
    a default-XDG host.
    """
    anchors: List[Path] = [paths.home]
    if paths.config_root != paths.home:
        anchors.append(paths.config_root)
    # R9 M2: include XDG parent dirs when they fall outside home so
    # ``XDG_DATA_HOME=/srv/share`` round-trips. We anchor on the
    # PARENT of the cc subdir (e.g. ``/srv/share`` not
    # ``/srv/share/claude``) so the relative path keeps the ``claude/``
    # segment for symmetry with the un-redirected layout.
    for xdg in (paths.xdg_data_claude.parent, paths.xdg_cache_claude.parent, paths.xdg_state_claude.parent):
        try:
            xdg.relative_to(paths.home)
        except ValueError:
            if xdg not in anchors:
                anchors.append(xdg)
    # R10 M2: sort by descending path length — longer/more-specific
    # anchors win first. Stable sort preserves insertion order for
    # ties (anchors of equal length are extremely rare).
    anchors.sort(key=lambda p: len(str(p)), reverse=True)
    return _relative_under_home(*anchors, source=source)


def _relative_under_home(*anchors: Path, source: Path) -> Path:
    """Strip the first matching ``anchor`` from ``source``.

    On Windows NTFS / macOS APFS-default the filesystem is case-insensitive
    but ``Path.relative_to`` does a literal compare — ``C:\\Users\\Foo`` and
    ``c:\\users\\foo`` would be treated as distinct, sending genuinely-local
    files into the ``external/`` escape branch. ``os.path.normcase`` collapses
    that on case-insensitive platforms; on POSIX it's the identity, so the
    behaviour is unchanged there.

    The variadic anchor list lets ``CLAUDE_CONFIG_DIR``-redirected layouts
    keep their data round-trippable through the backup mirror tree even
    when the cc data dir sits outside the user's $HOME.

    The fallback also tries Unicode NFC normalisation on both sides so
    HFS+ macOS (which stores filenames as NFD) can still match against
    our NFC-canonicalised anchors. Without this, any non-ASCII home
    directory on HFS+ would dump every cc file into ``external/``.
    """
    for anchor in anchors:
        try:
            return source.relative_to(anchor)
        except ValueError:
            continue

    for anchor in anchors:
        anchor_norm = os.path.normcase(str(anchor))
        source_norm = os.path.normcase(str(source))
        if source_norm.startswith(anchor_norm + os.sep):
            # Use the original (cased) tail so the backup mirror keeps the
            # filesystem's preferred capitalisation visible to the user.
            if str(source).lower().startswith(str(anchor).lower()):
                relative_str = str(source)[len(str(anchor)) + 1:]
            else:
                relative_str = source_norm[len(anchor_norm) + 1:]
            return Path(relative_str)

    # Final fallback: try matching after NFC-normalising both sides.
    # Catches HFS+ macOS where filesystem stores NFD but our anchors
    # are NFC.
    for anchor in anchors:
        anchor_nfc = unicodedata.normalize("NFC", os.path.normcase(str(anchor)))
        source_nfc = unicodedata.normalize("NFC", os.path.normcase(str(source)))
        if source_nfc.startswith(anchor_nfc + os.sep):
            tail = source_nfc[len(anchor_nfc) + 1:]
            return Path(tail)

    # R7 pass-8 L3: strip ``..`` and ``.`` segments defensively so a
    # source path containing ``..`` (e.g. via future glob target) can
    # NOT escape the ``external/`` namespace under backup_root.
    raw = str(source).replace(":", "").lstrip("/").replace("\\", "/")
    parts = [p for p in raw.split("/") if p and p not in ("..", ".")]
    cleaned = "/".join(parts) if parts else "unknown"
    return Path("external") / cleaned
