from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "tb-brain"
    return Path.home() / ".local" / "share" / "tb-brain"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_base_url: str = Field(default="http://127.0.0.1:11434/v1")
    llm_model: str = Field(default="")
    llm_api_key: str = Field(default="ollama")
    llm_timeout_seconds: float = Field(default=180.0)
    agent_max_tool_iters: int = Field(default=8)

    brain_host: str = Field(default="127.0.0.1")
    brain_port: int = Field(default=8765)
    brain_actor: str = Field(default="lab-local")
    brain_data_dir: Path = Field(default_factory=_default_data_dir)

    # Must match Open WebUI's FORWARD_USER_INFO_HEADER_JWT_SECRET (compose env).
    # Empty means no signed identity is available — lab/dev only.
    brain_user_jwt_secret: str = Field(default="")
    oauth_email_claim: str = Field(default="email")

    flow_mode: str = Field(default="stub")
    flow_base_url: str = Field(default="http://127.0.0.1:5000")
    flow_api_token: str = Field(default="")
    flow_database_url: str = Field(default="")

    @property
    def audit_log_dir(self) -> Path:
        return self.brain_data_dir / "logs"

    @property
    def flow_mode_normalized(self) -> str:
        return (self.flow_mode or "stub").strip().lower()


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.brain_data_dir.mkdir(parents=True, exist_ok=True)
    settings.audit_log_dir.mkdir(parents=True, exist_ok=True)
    return settings
