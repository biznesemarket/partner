"""Локальная конфигурация agent-kit: ~/.config/partner/agent.yaml.

Файл не попадает в git. Токен API и секрет устройства хранятся здесь
с правами 0600 (на POSIX); переменные окружения переопределяют файл.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
from dataclasses import asdict, dataclass, field

DEFAULT_HEARTBEAT_SEC = 30
DEFAULT_IDLE_TIMEOUT_SEC = 300


def _home_config_dir() -> pathlib.Path:
    return pathlib.Path.home() / ".config" / "partner"


def default_config_path() -> pathlib.Path:
    env = os.getenv("PARTNER_AGENT_CONFIG")
    return pathlib.Path(env) if env else _home_config_dir() / "agent.yaml"


def default_buffer_path() -> pathlib.Path:
    env = os.getenv("PARTNER_AGENT_BUFFER")
    return pathlib.Path(env) if env else _home_config_dir() / "agent-buffer.jsonl"


def default_signals_path() -> pathlib.Path:
    env = os.getenv("PARTNER_AGENT_SIGNALS")
    return pathlib.Path(env) if env else _home_config_dir() / "signals.json"


@dataclass
class AgentConfig:
    """Параметры кита. Секреты — только локально, не в репозитории."""

    server_url: str = "http://localhost:8000"
    api_token: str = ""
    participant_id: str = ""
    project_id: str = "partner"
    timezone: str = "Europe/Moscow"
    heartbeat_sec: int = DEFAULT_HEARTBEAT_SEC
    idle_timeout_sec: int = DEFAULT_IDLE_TIMEOUT_SEC
    task_ref: str | None = None
    branch: str | None = None
    device_id: str | None = None
    device_secret: str | None = field(default=None, repr=False)
    buffer_path: str | None = None
    signals_path: str | None = None

    def resolved_buffer_path(self) -> pathlib.Path:
        path = pathlib.Path(self.buffer_path) if self.buffer_path else default_buffer_path()
        return path.expanduser()

    def resolved_signals_path(self) -> pathlib.Path:
        path = pathlib.Path(self.signals_path) if self.signals_path else default_signals_path()
        return path.expanduser()

    def validate(self) -> None:
        if not self.server_url:
            raise ValueError("server_url не задан")
        if not self.participant_id:
            raise ValueError("participant_id не задан")
        if not self.project_id:
            raise ValueError("project_id не задан")
        if self.heartbeat_sec < 1:
            raise ValueError("heartbeat_sec должен быть >= 1")

    def redacted(self) -> dict:
        data = asdict(self)
        if data.get("api_token"):
            data["api_token"] = "***"
        if data.get("device_secret"):
            data["device_secret"] = "***"
        return data


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None else value


def _env_opt(name: str, default: str | None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    return value or None


def load_config(path: pathlib.Path | None = None) -> AgentConfig:
    """Файл YAML (если есть) + env-переопределения."""
    import yaml

    target = (path or default_config_path()).expanduser()
    raw: dict = {}
    if target.exists():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    device = raw.get("device") if isinstance(raw.get("device"), dict) else {}
    cfg = AgentConfig(
        server_url=str(raw.get("server_url") or AgentConfig.server_url),
        api_token=str(raw.get("api_token") or ""),
        participant_id=str(raw.get("participant_id") or ""),
        project_id=str(raw.get("project_id") or AgentConfig.project_id),
        timezone=str(raw.get("timezone") or AgentConfig.timezone),
        heartbeat_sec=int(raw.get("heartbeat_sec") or DEFAULT_HEARTBEAT_SEC),
        idle_timeout_sec=int(raw.get("idle_timeout_sec") or DEFAULT_IDLE_TIMEOUT_SEC),
        task_ref=raw.get("task_ref") or None,
        branch=raw.get("branch") or None,
        device_id=device.get("device_id") or None,
        device_secret=device.get("secret") or None,
        buffer_path=raw.get("buffer_path") or None,
        signals_path=raw.get("signals_path") or None,
    )
    cfg.server_url = _env_str("PARTNER_SERVER_URL", cfg.server_url)
    cfg.api_token = _env_str("PARTNER_API_TOKEN", cfg.api_token)
    cfg.participant_id = _env_str("PARTNER_AGENT_PARTICIPANT", cfg.participant_id)
    cfg.project_id = _env_str("PARTNER_AGENT_PROJECT", cfg.project_id)
    cfg.timezone = _env_str("PARTNER_AGENT_TZ", cfg.timezone)
    cfg.heartbeat_sec = _env_int("PARTNER_AGENT_HEARTBEAT_SEC", cfg.heartbeat_sec)
    cfg.idle_timeout_sec = _env_int("PARTNER_AGENT_IDLE_TIMEOUT", cfg.idle_timeout_sec)
    cfg.task_ref = _env_opt("PARTNER_AGENT_TASK", cfg.task_ref)
    cfg.branch = _env_opt("PARTNER_AGENT_BRANCH", cfg.branch)
    cfg.device_id = _env_opt("PARTNER_AGENT_DEVICE_ID", cfg.device_id)
    cfg.device_secret = _env_opt("PARTNER_AGENT_DEVICE_SECRET", cfg.device_secret)
    cfg.buffer_path = _env_opt("PARTNER_AGENT_BUFFER", cfg.buffer_path)
    cfg.signals_path = _env_opt("PARTNER_AGENT_SIGNALS", cfg.signals_path)
    return cfg


def save_config(cfg: AgentConfig, path: pathlib.Path | None = None) -> pathlib.Path:
    """Атомарная запись с правами 0600; возвращает путь файла."""
    import yaml

    target = (path or default_config_path()).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "server_url": cfg.server_url,
        "api_token": cfg.api_token,
        "participant_id": cfg.participant_id,
        "project_id": cfg.project_id,
        "timezone": cfg.timezone,
        "heartbeat_sec": cfg.heartbeat_sec,
        "idle_timeout_sec": cfg.idle_timeout_sec,
        "task_ref": cfg.task_ref,
        "branch": cfg.branch,
        "buffer_path": cfg.buffer_path,
        "signals_path": cfg.signals_path,
        "device": {
            "device_id": cfg.device_id,
            "secret": cfg.device_secret,
        },
    }
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".agent-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return target
