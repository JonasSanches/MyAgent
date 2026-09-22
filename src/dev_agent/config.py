from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Config:
    root: Path
    model: str
    max_output_tokens: int
    daily_request_limit: int
    daily_token_budget: int
    reasoning_effort: str = "high"
    text_verbosity: str = "medium"
    routine_model: str = "gpt-5.6-terra"
    routine_enabled: bool = True
    database_path: Optional[Path] = None
    workspace_path: Optional[Path] = None
    app_password: str = ""
    session_secret: str = ""
    cookie_secure: bool = False
    github_app_id: str = ""
    github_installation_id: str = ""
    github_app_private_key_b64: str = ""

    @property
    def database(self) -> Path:
        return self.database_path or self.root / "data" / "agent.db"

    @property
    def workspace(self) -> Path:
        return (self.workspace_path or self.root).resolve()

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    def validate_for_host(self, host: str) -> None:
        """Evita publicar uma instância sem uma barreira de acesso real."""
        local_hosts = {"127.0.0.1", "localhost", "::1"}
        if self.app_password and len(self.session_secret) < 32:
            raise ValueError("DEV_AGENT_SESSION_SECRET precisa ter pelo menos 32 caracteres quando a senha está ativa.")
        if host not in local_hosts:
            if not self.app_password:
                raise ValueError("DEV_AGENT_APP_PASSWORD é obrigatório para publicar o agente.")
            if not self.cookie_secure:
                raise ValueError("DEV_AGENT_COOKIE_SECURE=true é obrigatório para publicar o agente por HTTPS.")

    @classmethod
    def load(cls, root: Optional[Path] = None) -> "Config":
        project_root = root or Path(__file__).resolve().parents[2]
        database = os.getenv("DEV_AGENT_DATABASE_PATH", "").strip()
        workspace = os.getenv("DEV_AGENT_WORKSPACE", "").strip()
        return cls(
            root=project_root,
            model=os.getenv("DEV_AGENT_MODEL", "gpt-5.3-codex"),
            max_output_tokens=int(os.getenv("DEV_AGENT_MAX_OUTPUT_TOKENS", "4000")),
            daily_request_limit=int(os.getenv("DEV_AGENT_DAILY_REQUEST_LIMIT", "5")),
            daily_token_budget=int(os.getenv("DEV_AGENT_DAILY_TOKEN_BUDGET", "30000")),
            reasoning_effort=os.getenv("DEV_AGENT_REASONING_EFFORT", "high"),
            text_verbosity=os.getenv("DEV_AGENT_TEXT_VERBOSITY", "medium"),
            routine_model=os.getenv("DEV_AGENT_ROUTINE_MODEL", "gpt-5.6-terra"),
            routine_enabled=os.getenv("DEV_AGENT_ROUTINE_ENABLED", "true").strip().lower() == "true",
            database_path=Path(database).expanduser() if database else None,
            workspace_path=Path(workspace).expanduser() if workspace else None,
            app_password=os.getenv("DEV_AGENT_APP_PASSWORD", ""),
            session_secret=os.getenv("DEV_AGENT_SESSION_SECRET", ""),
            cookie_secure=os.getenv("DEV_AGENT_COOKIE_SECURE", "false").strip().lower() == "true",
            github_app_id=os.getenv("GITHUB_APP_ID", "").strip(),
            github_installation_id=os.getenv("GITHUB_APP_INSTALLATION_ID", "").strip(),
            github_app_private_key_b64=os.getenv("GITHUB_APP_PRIVATE_KEY_B64", "").strip(),
        )
