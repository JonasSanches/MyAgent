from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

from .config import Config
from .core import PersonalDevAgent
from .github import GitHubAppClient
from .auth import SessionAuth
from .permissions import ActionRisk
from .security import redact_secrets


class AgentWebService:
    """Adaptador HTTP fino: toda regra de autonomia continua no núcleo do agente."""

    def __init__(self, agent: PersonalDevAgent):
        self.agent = agent
        self.github = GitHubAppClient(agent.config.github_app_id, agent.config.github_installation_id,
                                      agent.config.github_app_private_key_b64)

    def state(self) -> Dict[str, Any]:
        requests, tokens = self.agent.memory.usage_today()
        return {
            "usage": {
                "requests": requests,
                "request_limit": self.agent.config.daily_request_limit,
                "tokens": tokens,
                "token_budget": self.agent.config.daily_token_budget,
            },
            "knowledge": [
                {"id": item.id, "title": item.title, "success_count": item.success_count}
                for item in self.agent.memory.all_knowledge(10)
            ],
            "history": [
                {"id": item.id, "prompt": item.prompt, "source": item.source,
                 "status": item.status, "approval_status": item.approval_status}
                for item in self.agent.memory.history(10)
            ],
            "permissions": [
                {"action": item.action.value, "confirmation": item.requires_confirmation}
                for item in self.agent.permissions.describe()
            ],
            "routing": {
                "routine_enabled": self.agent.config.routine_enabled,
                "routine_model": self.agent.config.routine_model,
                "expert_model": self.agent.config.model,
            },
            "maturity": self.agent.memory.maturity_stats(),
        }

    def chat(self, message: str, authorize_codex: bool = False, images: Optional[list[str]] = None) -> Dict[str, Any]:
        message = message.strip()
        safe_images = _validate_images(images or [])
        if not message and not safe_images:
            raise ValueError("Escreva uma tarefa ou anexe um print.")
        if not message:
            message = "Analise o(s) print(s) anexado(s) e descreva os próximos passos."
        has_link = bool(re.search(r"https?://\S+", message))
        result = self.agent.solve(message, use_codex=authorize_codex, image_data_urls=safe_images,
                                  use_web_search=has_link and authorize_codex)
        if result.route == "needs_codex" and _is_repository_lookup(message):
            if not self.github.enabled:
                return {
                    "kind": "solution", "attempt_id": result.attempt_id, "source": "github_gap",
                    "message": "Para procurar nos seus projetos, conecte a GitHub App com acesso somente-leitura.",
                    "solution": "Nenhum modelo externo foi consultado e nenhum crédito foi usado.",
                }
            files = self.github.find_files(message)
            solution = "\n".join(
                f"- {item.repository}: {item.path} — {item.confidence}% provável ({item.reason})\n  {item.url}"
                for item in files
            )
            if not solution:
                audit = "\n".join(f"- {item}" for item in self.github.last_diagnostics)
                solution = "Não encontrei arquivos correspondentes nos repositórios autorizados.\n\nBusca auditada:\n" + (audit or "- Nenhum repositório foi retornado pela instalação GitHub.")
            attempt = self.agent.memory.create_attempt(message, solution, "github", status="completed")
            mapped_directories = any(item.kind == "directory" for item in files)
            return {"kind": "solution", "attempt_id": attempt.id, "source": "github",
                    "message": ("Não encontrei um arquivo específico; mapeei diretórios de código que podem carregar os textos da interface."
                                if mapped_directories else "Busca somente-leitura concluída no GitHub; candidatos ordenados por evidências de tradução."), "solution": solution}
        if result.route == "needs_codex":
            route = self.agent.specialist_route(message, safe_images, has_link)
            return {
                "kind": "confirmation",
                "attempt_id": result.attempt_id,
                "message": "Não encontrei conhecimento validado suficiente. Posso consultar a próxima camada de raciocínio?",
                "detail": ("A tarefa parece de rotina: usarei o modelo de rotina com raciocínio alto. A consulta envia o texto à API e pode gerar custo."
                           if route == "routine" else
                           "A tarefa exige o Codex como especialista de alta qualidade. A consulta envia o texto e os prints anexados à API e pode gerar custo."
                           + (" O link informado poderá acionar pesquisa web." if has_link else "")),
            }
        return {
            "kind": "solution",
            "attempt_id": result.attempt_id,
            "source": result.route,
            "message": result.message,
            "solution": result.solution,
        }

    def complete(self, attempt_id: int, test_command: str, workdir: str) -> Dict[str, Any]:
        if not test_command.strip():
            raise ValueError("Informe o comando de teste.")
        safe_workdir = self._safe_workdir(workdir)
        safe_environment = {
            key: value for key, value in os.environ.items()
            if key not in {"OPENAI_API_KEY", "DEV_AGENT_APP_PASSWORD", "DEV_AGENT_SESSION_SECRET"}
        }
        try:
            completed = subprocess.run(
                shlex.split(test_command), cwd=safe_workdir, text=True, capture_output=True,
                timeout=300, check=False, env=safe_environment,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"Não foi possível executar o teste: {error}") from error
        output = redact_secrets((completed.stdout + completed.stderr).strip())
        result = self.agent.memory.record_test_result(
            attempt_id, completed.returncode == 0, output, test_command
        )
        response = {
            "passed": result.passed,
            "attempt_id": attempt_id,
            "output": output[-4000:],
            "status": result.attempt.status,
        }
        if result.passed and result.attempt.status == "awaiting_approval":
            response["summary"] = result.learning_summary
            response["requires_approval"] = self.agent.permissions.decide(ActionRisk.PERMANENT_KNOWLEDGE).requires_confirmation
        return response

    def _safe_workdir(self, workdir: str) -> Path:
        workspace = self.agent.config.workspace
        requested = Path(workdir)
        candidate = (requested if requested.is_absolute() else workspace / requested).resolve()
        if candidate != workspace and workspace not in candidate.parents:
            raise ValueError("O diretório de teste precisa estar dentro de DEV_AGENT_WORKSPACE.")
        if not candidate.is_dir():
            raise ValueError("O diretório de teste informado não existe.")
        return candidate

    def approve(self, attempt_id: int) -> Dict[str, Any]:
        identifier = self.agent.memory.approve_attempt(attempt_id)
        return {"knowledge_id": identifier, "message": f"Conhecimento #{identifier} salvo permanentemente."}

    def reject(self, attempt_id: int) -> Dict[str, Any]:
        self.agent.memory.reject_attempt(attempt_id)
        return {"message": "A solução continua no histórico, mas não será reutilizada como conhecimento permanente."}


class AgentRequestHandler(SimpleHTTPRequestHandler):
    service: AgentWebService
    static_dir: Path
    auth: SessionAuth

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        if path == "/api/session":
            self._json(HTTPStatus.OK, {"authenticated": self._authenticated(), "required": self.auth.enabled})
            return
        if path.startswith("/api/") and not self._authenticated():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Faça login para acessar o agente."})
            return
        if path == "/api/state":
            self._json(HTTPStatus.OK, self.service.state())
            return
        self._static()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._body()
            if path == "/api/login":
                token = self.auth.login(str(payload.get("password", "")))
                if self.auth.enabled and not token:
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "Senha incorreta."})
                    return
                headers = {"Set-Cookie": self.auth.cookie(token or "")}
                self._json(HTTPStatus.OK, {"authenticated": True}, headers)
                return
            if path == "/api/logout":
                self._json(HTTPStatus.OK, {"authenticated": False}, {"Set-Cookie": self.auth.cookie("", expires=False)})
                return
            if path.startswith("/api/") and not self._authenticated():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "Faça login para acessar o agente."})
                return
            routes: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
                "/api/chat": lambda data: self.service.chat(data.get("message", ""), bool(data.get("authorize_codex")), data.get("images", [])),
                "/api/complete": lambda data: self.service.complete(int(data["attempt_id"]), data.get("test_command", ""), data.get("workdir", ".")),
                "/api/approve": lambda data: self.service.approve(int(data["attempt_id"])),
                "/api/reject": lambda data: self.service.reject(int(data["attempt_id"])),
            }
            if path not in routes:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
                return
            self._json(HTTPStatus.OK, routes[path](payload))
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": redact_secrets(str(error))})

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _authenticated(self) -> bool:
        return self.auth.authenticated(self.headers.get("Cookie", ""))

    def _json(self, status: HTTPStatus, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        super().end_headers()

    def _static(self) -> None:
        requested = urlparse(self.path).path.lstrip("/") or "index.html"
        candidate = (self.static_dir / requested).resolve()
        if self.static_dir not in candidate.parents and candidate != self.static_dir:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = "text/html; charset=utf-8" if candidate.suffix == ".html" else "text/css; charset=utf-8" if candidate.suffix == ".css" else "application/javascript; charset=utf-8"
        content = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        return


def make_server(host: str, port: int, root: Optional[Path] = None) -> ThreadingHTTPServer:
    config = Config.load(root)
    config.validate_for_host(host)
    agent = PersonalDevAgent(config)
    static_dir = Path(__file__).resolve().parent / "web"
    handler = type("ConfiguredAgentRequestHandler", (AgentRequestHandler,), {
        "service": AgentWebService(agent), "static_dir": static_dir,
        "auth": SessionAuth(config.app_password, config.session_secret, config.cookie_secure),
    })
    return ThreadingHTTPServer((host, port), handler)


def _validate_images(images: list[str]) -> list[str]:
    if len(images) > 4:
        raise ValueError("Anexe no máximo 4 prints por mensagem.")
    accepted = re.compile(r"^data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")
    for image in images:
        if not isinstance(image, str) or not accepted.match(image) or len(image) > 7_000_000:
            raise ValueError("Use prints PNG, JPEG ou WebP de até aproximadamente 5 MB.")
    return images


def _is_repository_lookup(message: str) -> bool:
    words = message.lower()
    return any(term in words for term in ("arquivo", "repositório", "repositorio", "projeto", "localize", "localizar", "encontre", "encontrar"))


def main() -> int:
    parser = argparse.ArgumentParser(prog="dev-agent-web")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8787")))
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f"Meu Agente disponível em http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
