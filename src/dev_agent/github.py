from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class RepositoryFile:
    repository: str
    path: str
    url: str


class GitHubAppClient:
    """Cliente somente-leitura para uma instalação da GitHub App."""

    api = "https://api.github.com"

    def __init__(self, app_id: str, installation_id: str, private_key_b64: str):
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_b64 = private_key_b64

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.private_key_b64)

    def find_files(self, prompt: str, limit: int = 20) -> list[RepositoryFile]:
        if not self.enabled:
            raise RuntimeError("A integração GitHub ainda não foi conectada.")
        token = self._installation_token()
        repositories = self._request("/installation/repositories?per_page=100", token).get("repositories", [])
        project_hint = _project_hint(prompt)
        if project_hint:
            repositories = [repo for repo in repositories if project_hint in repo["name"].lower()]
        matches: list[RepositoryFile] = []
        for repo in repositories:
            tree = self._request(f"/repos/{repo['full_name']}/git/trees/{quote(repo['default_branch'], safe='')}?recursive=1", token)
            for item in tree.get("tree", []):
                path = item.get("path", "")
                if item.get("type") == "blob" and _matches(path, prompt):
                    matches.append(RepositoryFile(repo["full_name"], path, f"{repo['html_url']}/blob/{repo['default_branch']}/{path}"))
                    if len(matches) >= limit:
                        return matches
        return matches

    def _installation_token(self) -> str:
        try:
            import jwt
        except ImportError as error:
            raise RuntimeError("A dependência de autenticação GitHub não foi instalada.") from error
        try:
            private_key = base64.b64decode(self.private_key_b64).decode("utf-8")
        except Exception as error:
            raise RuntimeError("A chave privada da GitHub App não está no formato esperado.") from error
        now = int(time.time())
        app_token = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, private_key, algorithm="RS256")
        installation_id = self.installation_id or self._discover_installation(app_token)
        data = self._request(f"/app/installations/{installation_id}/access_tokens", app_token, method="POST")
        return str(data["token"])

    def _discover_installation(self, app_token: str) -> str:
        installations = self._request("/app/installations", app_token)
        if not isinstance(installations, list) or not installations:
            raise RuntimeError("A GitHub App ainda não foi instalada em uma conta.")
        if len(installations) > 1:
            raise RuntimeError("Há mais de uma instalação da GitHub App. Defina GITHUB_APP_INSTALLATION_ID no Render.")
        return str(installations[0]["id"])

    def _request(self, path: str, token: str, method: str = "GET") -> Any:
        request = Request(f"{self.api}{path}", method=method, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "my-agent-web",
        })
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code in {401, 403}:
                raise RuntimeError("A GitHub App não tem acesso a este repositório. Revise a instalação e as permissões de leitura.") from error
            raise RuntimeError("Não foi possível consultar o GitHub agora.") from error
        except URLError as error:
            raise RuntimeError("Não foi possível conectar ao GitHub agora.") from error


def _project_hint(prompt: str) -> str:
    words = ["".join(char for char in word.lower() if char.isalnum() or char in "-_") for word in prompt.split()]
    ignored = {"arquivo", "projeto", "localize", "localizar", "encontre", "encontrar", "quero", "preciso", "que", "tenha", "com", "para", "pro", "por", "traducao", "ingles", "somente", "mude", "nada"}
    candidates = [word for word in words if len(word) >= 4 and word not in ignored]
    return candidates[0] if candidates else ""


def _matches(path: str, prompt: str) -> bool:
    normalized = "".join(char for char in prompt.lower() if char.isalnum() or char in " /._-")
    path_lower = path.lower()
    translation_request = any(word in normalized for word in ("traducao", "translation", "ingles", "english", "idioma"))
    if translation_request:
        return any(marker in path_lower for marker in ("i18n", "locale", "lang", "translation", "traducao", "/en.", "/en/", "english"))
    terms = [word for word in normalized.split() if len(word) >= 4]
    return any(term in path_lower for term in terms)
