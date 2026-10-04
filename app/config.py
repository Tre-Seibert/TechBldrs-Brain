from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Pattern-matched answers that run before the LLM sees a question (app/agent/loop.py).
ROUTER_NAMES = ("merge_suggestion", "person_mail", "person_ticket", "tickets_about", "longest_time")


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
    llm_timeout_seconds: float = Field(default=600.0)
    agent_max_tool_iters: int = Field(default=8)
    # Comma-separated ROUTER_NAMES to skip, so those questions go to the LLM instead.
    # Empty keeps every router on.
    disabled_routers: str = Field(default="")
    # Qwen3-style models "think" before answering (about 9x slower here). true appends the
    # /no_think soft switch to the system prompt. Harmless for models that ignore it.
    llm_no_think: bool = Field(default=False)

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

    # --- Knowledge (SOP/runbook search). Empty QDRANT_URL disables search_knowledge
    # gracefully -- see app/knowledge/source.py NullKnowledgeSource.
    qdrant_url: str = Field(default="")
    knowledge_collection: str = Field(default="tb_knowledge")
    embedding_model: str = Field(default="nomic-embed-text")

    @field_validator("disabled_routers")
    @classmethod
    def _known_routers(cls, value: str) -> str:
        names = [name.strip().lower() for name in (value or "").split(",") if name.strip()]
        unknown = [name for name in names if name not in ROUTER_NAMES]
        if unknown:
            raise ValueError(f"unknown router(s) {unknown}; valid: {', '.join(ROUTER_NAMES)}")
        return ",".join(names)

    @property
    def disabled_router_set(self) -> frozenset[str]:
        return frozenset(name for name in self.disabled_routers.split(",") if name)

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
