"""Runtime path helpers derived from the active config context."""

from __future__ import annotations

from pathlib import Path

from nanobot.utils.helpers import ensure_dir


def get_config_path() -> Path:
    """Get the configuration file path (lazy import to break circular dependency).

    Delegates to ``nanobot.config.loader.get_config_path`` at call time so
    that importing this module never triggers a circular import during startup.
    """
    from nanobot.config.loader import get_config_path as _loader_get_config_path
    return _loader_get_config_path()


def get_data_dir() -> Path:
    """Return the instance-level runtime data directory."""
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """Return a named runtime subdirectory under the instance data dir."""
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None) -> Path:
    """Return the media directory, optionally namespaced per channel."""
    base = get_runtime_subdir("media")
    return ensure_dir(base / channel) if channel else base


def get_cron_dir() -> Path:
    """Return the cron storage directory."""
    return get_runtime_subdir("cron")


def get_logs_dir() -> Path:
    """Return the logs directory."""
    return get_runtime_subdir("logs")


def get_webui_dir() -> Path:
    """Return the directory for WebUI-only persisted display threads (JSON)."""
    return get_runtime_subdir("webui")


def get_instance_webui_file(filename: str, webui_dir: Path | None = None) -> Path:
    """Path of a WebUI state file that belongs to this instance only.

    Instances whose config files share one folder (``config.json``, ``config.testy.json`` ...)
    also share ``webui/``. Settings kept in one file there, such as the default access mode or
    the pinned/archived chats, then leaked from one bot's WebUI into another's. ``config.json``
    keeps the original name; ``config.<tag>.json`` uses ``<name>.<tag><ext>``, started as a copy
    of the shared file so nothing a user already set disappears.
    """
    import re
    import shutil

    shared = (webui_dir if webui_dir is not None else get_webui_dir()) / filename
    stem = get_config_path().stem
    tag = re.sub(r"[^A-Za-z0-9_-]+", "-", stem[len("config."):]).strip("-") if stem.startswith("config.") else ""
    if not tag:
        return shared
    own = shared.with_name(f"{shared.stem}.{tag}{shared.suffix}")
    if not own.exists() and shared.is_file():
        try:
            shutil.copyfile(shared, own)
        except OSError:
            return shared
    return own


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure the agent workspace path."""
    path = Path(workspace).expanduser() if workspace else Path.home() / ".nanobot" / "workspace"
    return ensure_dir(path)


def is_default_workspace(workspace: str | Path | None) -> bool:
    """Return whether a workspace resolves to nanobot's default workspace path."""
    current = Path(workspace).expanduser() if workspace is not None else Path.home() / ".nanobot" / "workspace"
    default = Path.home() / ".nanobot" / "workspace"
    return current.resolve(strict=False) == default.resolve(strict=False)


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return Path.home() / ".nanobot" / "history" / "cli_history"


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return Path.home() / ".nanobot" / "sessions"
