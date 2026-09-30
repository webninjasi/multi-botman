"""Atomic cursor/checkpoint persistence and restart-resume decisions."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class AppState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    cursor: str
    last_realtime_usec: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class StartDecision:
    mode: str  # "tail" or "cursor"
    cursor: str | None = None
    gap_marker: bool = False


def choose_start(
    state: AppState | None,
    *,
    subscription_id: str,
    now_realtime_usec: int,
    resume_max_age_sec: int,
) -> StartDecision:
    if state is None or state.subscription_id != subscription_id or not state.cursor:
        return StartDecision("tail")
    age_usec = max(0, now_realtime_usec - state.last_realtime_usec)
    if age_usec > resume_max_age_sec * 1_000_000:
        return StartDecision("tail", gap_marker=True)
    return StartDecision("cursor", cursor=state.cursor)


class StateStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)

    def path_for(self, app_name: str) -> Path:
        if not app_name or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-." for ch in app_name):
            raise ValueError("unsafe app key")
        return self.state_dir / f"{app_name}.json"

    def load(self, app_name: str) -> AppState | None:
        path = self.path_for(app_name)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return AppState.model_validate(raw)

    def save(self, app_name: str, state: AppState) -> None:
        path = self.path_for(app_name)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(path.parent, 0o700)
        except PermissionError:
            pass
        payload = json.dumps(state.model_dump(mode="json"), separators=(",", ":")) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            os.chmod(path, 0o600)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            tmp_path.unlink(missing_ok=True)
            raise
