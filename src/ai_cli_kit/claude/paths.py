"""Path helpers for Claude local cleanup."""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

# cc resolves its config root via two parallel rules — see notes in
# ``resolve_default_paths`` for the divergent fallbacks.
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"


def _nfc(path: Path) -> Path:
    """Normalize ``path`` to Unicode NFC form to match cc's resolver.

    cc applies ``.normalize('NFC')`` to the FULL config home string in
    ``src/utils/envUtils.ts:7-14`` regardless of whether the env var is
    set. macOS HFS+ stores filenames in NFD; if our path stays NFD while
    cc normalizes to NFC, downstream computations diverge — most
    visibly the keychain ``dirHash`` (sha256 of the path string) lands
    on a different service-name and we fail to delete cc's actual entry.
    """
    return Path(unicodedata.normalize("NFC", str(path)))


@dataclass(frozen=True)
class ClaudePaths:
    # ``home`` is the user's actual $HOME — used as the anchor for the
    # backup mirror tree AND for ``backup_root_base`` storage location.
    # Even when ``CLAUDE_CONFIG_DIR`` redirects cc data elsewhere, we keep
    # backups in the user's real home so wiping the cc dir doesn't also
    # destroy the cleanup escape hatch.
    home: Path
    # ``config_root`` is what cc calls ``getClaudeConfigHomeDir()`` —
    # equals ``$CLAUDE_CONFIG_DIR`` when set, else ``home``. It is used as
    # the parent for state file lookups and an additional anchor for the
    # backup mirror tree (so files at ``$CLAUDE_CONFIG_DIR/.claude.json``
    # round-trip cleanly through backup → restore).
    config_root: Path
    claude_dir: Path
    state_file: Path
    state_backup_glob: str
    legacy_state_file: Path
    settings_file: Path
    credentials_file: Path
    telemetry_dir: Path
    statsig_dir: Path
    projects_dir: Path
    history_file: Path
    sessions_dir: Path
    session_env_dir: Path
    shell_snapshots_dir: Path
    ide_dir: Path
    teams_dir: Path
    paste_cache_dir: Path
    plugins_dir: Path
    debug_dir: Path
    # cc subdirs / files added in R6 after independent audit found we
    # missed >20 real persistence points. Comments below cite the cc
    # source location for each so future regressions are easy to spot.
    usage_data_dir: Path           # commands/insights.ts:421 — model usage by org
    agents_dir: Path               # components/agents/agentFileUtils.ts:65
    skills_dir: Path               # skills/loadSkillsDir.ts:640
    plans_dir: Path                # utils/plans.ts:94 — saved /plan files
    rules_dir: Path                # utils/config.ts:1806
    user_claude_md: Path           # utils/config.ts:1784 — user-level memory
    keybindings_file: Path         # keybindings/loadUserBindings.ts:116
    cache_dir: Path                # model/modelCapabilities.ts:39
    local_install_dir: Path        # utils/localInstaller.ts:20
    jobs_dir: Path                 # utils/permissions/filesystem.ts:1523
    tasks_dir: Path                # utils/permissions/filesystem.ts:1728
    mcp_auth_cache_file: Path      # services/mcp/client.ts:262
    magic_docs_dir: Path           # services/MagicDocs/prompts.ts:68 — parent
    chrome_dir: Path               # utils/claudeInChrome/setup.ts:310
    image_store_dir: Path          # utils/imageStore.ts:131
    stats_cache_file: Path         # utils/statsCache.ts:78
    startup_perf_dir: Path         # utils/startupProfiler.ts:152
    update_lock_file: Path         # utils/autoUpdater.ts:169
    npm_cache_marker: Path         # utils/cleanup.ts:439
    version_cleanup_marker: Path   # utils/cleanup.ts:544
    upload_bridge_dir: Path        # bridge/inboundAttachments.ts:61
    # R6 audit pass 2 additions
    policy_limits_file: Path       # services/policyLimits/index.ts:55
    remote_settings_file: Path     # services/remoteManagedSettings/syncCacheState.ts:32
    computer_use_lock_file: Path   # utils/computerUse/computerUseLock.ts:10
    state_corrupted_glob: str      # cc utils/config.ts:1548 — ``<base>.corrupted.<ts>``
    # R6 audit pass 3 additions
    traces_dir: Path                # utils/telemetry/perfettoTracing.ts:273
    file_history_dir: Path          # utils/fileHistory.ts:737,955 — whole-file edit backups
    session_memory_dir: Path        # services/SessionMemory/prompts.ts:88,113
    deep_link_failure_marker: Path  # utils/deepLink/registerProtocol.ts:316
    user_commands_dir: Path         # skills/loadSkillsDir.ts:80 (parallel to skills/)
    # R6 audit pass 4 additions
    agent_memory_dir: Path          # tools/AgentTool/agentMemory.ts:48 — user scope
    plugin_cache_dir_env: Optional[str]  # CLAUDE_CODE_PLUGIN_CACHE_DIR resolved value
    # R6 audit pass 5 additions
    dump_prompts_dir: Path          # services/api/dumpPrompts.ts:59 — full prompt dumps
    cowork_plugins_dir: Path        # plugins/pluginDirectories.ts:42 — cowork variant
    remote_memory_base_env: Optional[str]  # CLAUDE_CODE_REMOTE_MEMORY_DIR raw value
    # R7 audit pass 1 additions
    workflows_dir: Path             # markdownConfigLoader.ts:29-36 / hooks/fileSuggestions.ts:447
    output_styles_dir: Path         # already used inline; promoted to field for consistency
    completion_glob: str            # cc utils/completionCache.ts: completion.{bash,zsh,fish}
    claude_backups_dir: Path
    backup_root_base: Path


def default_paths(home: Optional[Path] = None) -> ClaudePaths:
    """Build a ClaudePaths anchored on ``home`` ignoring environment.

    Kept for tests and callers that want full control. CLI users go
    through :func:`resolve_default_paths` so ``CLAUDE_CONFIG_DIR`` is
    honoured the way cc itself reads it.

    Both ``config_root`` and ``claude_dir`` are NFC-normalized so the
    string forms cc and cc-clean compute (e.g. for keychain hashes)
    stay byte-identical even on macOS where HFS+ defaults to NFD.
    """
    home_dir = Path.home() if home is None else Path(home).expanduser()
    return _build_paths(
        _nfc(home_dir),
        config_root=_nfc(home_dir),
        claude_dir=_nfc(home_dir / ".claude"),
        plugin_cache_env=None,
        remote_memory_env=None,
    )


_PLUGIN_CACHE_DIR_ENV = "CLAUDE_CODE_PLUGIN_CACHE_DIR"
_REMOTE_MEMORY_BASE_ENV = "CLAUDE_CODE_REMOTE_MEMORY_DIR"


def resolve_default_paths(
    home: Optional[Path] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> ClaudePaths:
    """Build a ClaudePaths matching cc's runtime resolution.

    cc has two parallel resolvers that diverge on the fallback rule:

    * ``getClaudeConfigHomeDir()`` (``src/utils/envUtils.ts``):
        ``CLAUDE_CONFIG_DIR ?? ~/.claude``
        — the data directory holding ``telemetry/`` ``statsig/`` etc.
    * ``getGlobalClaudeFile()`` (``src/utils/env.ts``):
        ``join(CLAUDE_CONFIG_DIR || homedir(), '.claude<suffix>.json')``
        — the state file (NOTE: fallback is ``homedir`` not the data dir).

    When ``CLAUDE_CONFIG_DIR`` is set both share the same root; when unset
    they DIVERGE: data lives in ``~/.claude`` but the state file is the
    sibling ``~/.claude.json``. We mirror that semantic so cleanup
    discovers the same files cc wrote regardless of the env layout.
    """
    home_dir = Path.home() if home is None else Path(home).expanduser()
    if env is None:
        env = os.environ
    raw_override = env.get(CLAUDE_CONFIG_DIR_ENV)
    if raw_override:
        config_root = Path(raw_override).expanduser()
        claude_dir = config_root
    else:
        config_root = home_dir
        claude_dir = home_dir / ".claude"
    # cc applies ``.normalize('NFC')`` to the FULL config home string
    # — both the env-overridden form and the legacy fallback form. We
    # do the same on the resulting paths so keychain hashes and any
    # other string-equality checks line up byte-for-byte with cc.
    plugin_cache_env = env.get(_PLUGIN_CACHE_DIR_ENV)
    remote_memory_env = env.get(_REMOTE_MEMORY_BASE_ENV)
    return _build_paths(
        _nfc(home_dir),
        config_root=_nfc(config_root),
        claude_dir=_nfc(claude_dir),
        plugin_cache_env=plugin_cache_env,
        remote_memory_env=remote_memory_env,
    )


def _build_paths(
    home_dir: Path,
    *,
    config_root: Path,
    claude_dir: Path,
    plugin_cache_env: Optional[str] = None,
    remote_memory_env: Optional[str] = None,
) -> ClaudePaths:
    return ClaudePaths(
        home=home_dir,
        config_root=config_root,
        claude_dir=claude_dir,
        # State file is anchored on ``config_root`` (= cc's
        # ``getGlobalClaudeFile`` parent). The oauth-suffixed variants
        # (``.claude-staging-oauth.json``, ``.claude-local-oauth.json``,
        # ``.claude-custom-oauth.json`` from ``src/constants/oauth.ts``
        # ``fileSuffixForOauthConfig``) are matched via an explicit
        # tuple in ``services.build_targets`` rather than a broad
        # ``.claude*.json`` glob — the explicit list avoids sweeping
        # unrelated user files like ``.claudeextra.json``.
        state_file=config_root / ".claude.json",
        # cc itself drops corruption-recovery snapshots next to the live
        # state file, named ``.claude<suffix>.json.backup.<NNN>``. They
        # mirror the live state and therefore retain whatever PII
        # (userID, oauthAccount, ...) was current when cc rotated them.
        state_backup_glob=".claude*.json.backup.*",
        # Legacy fallback path. ``getGlobalClaudeFile`` falls through
        # to ``<claude_dir>/.config.json`` if it pre-exists from an
        # older cc install (cc reads via ``getClaudeConfigHomeDir()``
        # which IS our ``claude_dir``, NOT ``config_root``) — cleanup
        # must scrub it too.
        legacy_state_file=claude_dir / ".config.json",
        settings_file=claude_dir / "settings.json",
        credentials_file=claude_dir / ".credentials.json",
        telemetry_dir=claude_dir / "telemetry",
        statsig_dir=claude_dir / "statsig",
        projects_dir=claude_dir / "projects",
        history_file=claude_dir / "history.jsonl",
        # NOTE: cc ``~/.claude/sessions/`` is a concurrent-session PID
        # tracker (``src/utils/concurrentSessions.ts``), NOT chat
        # session storage. Chat sessions live under ``projects_dir``. We
        # keep the field name for backwards compat but the cleanup
        # target's label/description disambiguate the semantic.
        sessions_dir=claude_dir / "sessions",
        session_env_dir=claude_dir / "session-env",
        # ``~/.claude/shell-snapshots`` (kebab-case in cc — see
        # ``src/utils/bash/ShellSnapshot.ts:439``) contains bash/zsh state
        # captures from every session — includes cwd history and
        # partial command lines.
        shell_snapshots_dir=claude_dir / "shell-snapshots",
        # ``~/.claude/ide`` holds IDE-side handshake state. Removing forces
        # the IDE plugin to re-enroll on next launch.
        ide_dir=claude_dir / "ide",
        # ``~/.claude/teams`` — team config (members, UUIDs, emails).
        teams_dir=claude_dir / "teams",
        # ``~/.claude/paste-cache`` — sha256-named files containing user
        # pasted text in plaintext (``src/utils/pasteStore.ts``). Privacy.
        paste_cache_dir=claude_dir / "paste-cache",
        # ``~/.claude/plugins`` holds ``known_marketplaces.json`` (URL +
        # potentially private git endpoints) and ``installed_plugins.json``
        # (versioned cache state). cc reads/writes these via
        # ``src/utils/plugins/marketplaceManager.ts:103`` and
        # ``installedPluginsManager.ts``.
        plugins_dir=claude_dir / "plugins",
        # ``~/.claude/debug`` is where cc writes per-session debug logs
        # under ``--debug``. Files include verbatim prompts/responses.
        # cc itself prunes old ones (``src/utils/cleanup.ts:391``) but
        # the live session log persists between runs.
        debug_dir=claude_dir / "debug",
        # R6: dirs/files surfaced by independent audit. Most are PII or
        # workflow data; some (CLAUDE.md / rules / skills / agents) are
        # user-authored content so we mark targets as default_selected=False.
        usage_data_dir=claude_dir / "usage-data",
        agents_dir=claude_dir / "agents",
        skills_dir=claude_dir / "skills",
        plans_dir=claude_dir / "plans",
        rules_dir=claude_dir / "rules",
        user_claude_md=claude_dir / "CLAUDE.md",
        keybindings_file=claude_dir / "keybindings.json",
        cache_dir=claude_dir / "cache",
        local_install_dir=claude_dir / "local",
        jobs_dir=claude_dir / "jobs",
        tasks_dir=claude_dir / "tasks",
        mcp_auth_cache_file=claude_dir / "mcp-needs-auth-cache.json",
        magic_docs_dir=claude_dir / "magic-docs",
        chrome_dir=claude_dir / "chrome",
        # cc constant ``IMAGE_STORE_DIR = 'image-cache'`` (kebab-case)
        # at ``utils/imageStore.ts:9``. Earlier R6 review wrote
        # ``imageStore`` (camelCase) which silently never matches the
        # real cc directory.
        image_store_dir=claude_dir / "image-cache",
        stats_cache_file=claude_dir / "stats-cache.json",
        startup_perf_dir=claude_dir / "startup-perf",
        update_lock_file=claude_dir / ".update.lock",
        npm_cache_marker=claude_dir / ".npm-cache-cleanup",
        version_cleanup_marker=claude_dir / ".version-cleanup",
        upload_bridge_dir=claude_dir / "uploads",
        policy_limits_file=claude_dir / "policy-limits.json",
        remote_settings_file=claude_dir / "remote-settings.json",
        computer_use_lock_file=claude_dir / "computer-use.lock",
        # cc emits BOTH ``.backup.<ts>`` AND ``.corrupted.<ts>`` next to
        # state files (utils/config.ts:1548). Corrupted snapshots carry
        # the same userID/oauth content that triggered corruption — must
        # be swept by the same target.
        state_corrupted_glob=".claude*.json.corrupted.*",
        traces_dir=claude_dir / "traces",
        file_history_dir=claude_dir / "file-history",
        # cc loads loadSessionMemoryTemplate from ``session-memory/config/template.md``
        # and prompt.md siblings under the same parent — sweep the dir.
        session_memory_dir=claude_dir / "session-memory",
        deep_link_failure_marker=claude_dir / ".deep-link-register-failed",
        user_commands_dir=claude_dir / "commands",
        # ``~/.claude/agent-memory/<agentType>/`` — cc agent persistent
        # state at user scope (tools/AgentTool/agentMemory.ts:48).
        agent_memory_dir=claude_dir / "agent-memory",
        # cc reads ``CLAUDE_CODE_PLUGIN_CACHE_DIR`` at runtime; when
        # set, plugins live there instead of ``claude_dir/plugins``.
        # We carry the raw env value (not a Path) so callers can show
        # users the literal env-set string in diagnostics.
        plugin_cache_dir_env=plugin_cache_env,
        # cc dumpPrompts.ts:59 — `<config_dir>/dump-prompts/<sid>.jsonl`
        # contains FULL prompts + system + tool catalog when the dump
        # mode is enabled. High PII.
        dump_prompts_dir=claude_dir / "dump-prompts",
        # cc cowork plugins variant — alternate plugins dir when
        # CLAUDE_CODE_USE_COWORK_PLUGINS is truthy or session state
        # toggles cowork mode (plugins/pluginDirectories.ts:42).
        cowork_plugins_dir=claude_dir / "cowork_plugins",
        # cc memdir/paths.ts: when set, agent-memory + auto-memory
        # live under this base instead of claude_dir.
        remote_memory_base_env=remote_memory_env,
        # cc loads workflows/*.md as user-level configurable workflows
        # (markdownConfigLoader.ts:29-36 alongside commands/agents/skills/output-styles).
        workflows_dir=claude_dir / "workflows",
        # Promoted from inline use to a real field so target build sites
        # don't have to hand-construct the path each time.
        output_styles_dir=claude_dir / "output-styles",
        # cc completion cache files (bash/zsh/fish) under claude_dir.
        completion_glob="completion.*",
        claude_backups_dir=claude_dir / "backups",
        # backup_root_base is INTENTIONALLY anchored on the user's real
        # ``home``, not ``config_root``. If cc data lives at
        # ``CLAUDE_CONFIG_DIR=/srv/x``, we still want backups under
        # ``$HOME/.claude-clean-backups`` so wiping the cc data dir
        # doesn't also destroy the rollback path.
        backup_root_base=home_dir / ".claude-clean-backups",
    )
