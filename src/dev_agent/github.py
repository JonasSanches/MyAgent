from __future__ import annotations

import base64
import json
import re
import time
import unicodedata
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
    branch: str = "main"
    confidence: int = 0
    reason: str = ""
    kind: str = "file"


class GitHubAppClient:
    """Cliente somente-leitura para uma instalação da GitHub App."""

    api = "https://api.github.com"

    def __init__(self, app_id: str, installation_id: str, private_key_b64: str):
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_b64 = private_key_b64
        self.last_diagnostics: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.private_key_b64)

    def find_files(self, prompt: str, limit: int = 20) -> list[RepositoryFile]:
        if not self.enabled:
            raise RuntimeError("A integração GitHub ainda não foi conectada.")
        token = self._installation_token()
        self.last_diagnostics = []
        repositories = self._request("/installation/repositories?per_page=100", token).get("repositories", [])
        project_hint = _project_hint(prompt)
        if project_hint:
            matched_repositories = [repo for repo in repositories if project_hint in repo["name"].lower()]
            # Um apelido local não deve impedir a busca nos demais projetos autorizados.
            if matched_repositories:
                repositories = matched_repositories
        matches: list[RepositoryFile] = []
        source_directories: list[RepositoryFile] = []
        for repo in repositories:
            for item in self._search_code(repo, token, prompt):
                if item.url not in {existing.url for existing in matches} and len(matches) < limit:
                    matches.append(item)
            tree = self._request(f"/repos/{repo['full_name']}/git/trees/{quote(repo['default_branch'], safe='')}?recursive=1", token)
            self.last_diagnostics.append(f"{repo['full_name']}: {len(tree.get('tree', []))} arquivos examinados na branch {repo['default_branch']}.")
            source_directories.extend(_text_source_directories(repo, tree.get("tree", [])))
            for item in tree.get("tree", []):
                path = item.get("path", "")
                if item.get("type") == "blob" and _matches(path, prompt) and len(matches) < limit:
                    matches.append(RepositoryFile(repo["full_name"], path, f"{repo['html_url']}/blob/{repo['default_branch']}/{path}", repo["default_branch"]))
        ranked = self._rank_translation_candidates(matches, token, prompt)
        if ranked or not _needs_text_source_map(prompt):
            return ranked
        # Se não existe um catálogo óbvio de traduções, ainda é útil mostrar de
        # onde a interface pode estar lendo textos. Isso evita concluir que o
        # projeto não tem tradução apenas pelo nome dos arquivos.
        self.last_diagnostics.append("Nenhum arquivo específico foi identificado; diretórios de código foram mapeados como próxima pista.")
        return source_directories[:limit]

    def analyze_directory(self, prompt: str, limit: int = 30) -> list[RepositoryFile]:
        """Lista arquivos de uma pasta indicada, com papéis inferidos localmente."""
        directory = _directory_hint(prompt)
        if not directory:
            return []
        token = self._installation_token()
        self.last_diagnostics = []
        repositories = self._request("/installation/repositories?per_page=100", token).get("repositories", [])
        project_hint = _project_hint(prompt)
        selected = [repo for repo in repositories if project_hint in repo["name"].lower()] if project_hint else repositories
        if not selected:
            selected = repositories
        results: list[RepositoryFile] = []
        for repo in selected:
            tree = self._request(f"/repos/{repo['full_name']}/git/trees/{quote(repo['default_branch'], safe='')}?recursive=1", token)
            prefix = directory.lower().rstrip("/") + "/"
            for item in tree.get("tree", []):
                path = item.get("path", "")
                if item.get("type") != "blob" or not path.lower().startswith(prefix) or not _is_text_source_file(path):
                    continue
                results.append(RepositoryFile(
                    repo["full_name"], path, f"{repo['html_url']}/blob/{repo['default_branch']}/{path}", repo["default_branch"],
                    0, _file_role(path), "analysis",
                ))
                if len(results) >= limit:
                    break
            self.last_diagnostics.append(f"{repo['full_name']}: pasta {directory.rstrip('/')}/ analisada; {len(results)} arquivos de código/texto listados.")
        return results

    def analyze_file(self, prompt: str) -> list[RepositoryFile]:
        """Lê um arquivo explicitamente indicado e devolve um resumo sem expor o fonte inteiro."""
        path = _file_hint(prompt)
        if not path:
            return []
        token = self._installation_token()
        self.last_diagnostics = []
        repositories = self._request("/installation/repositories?per_page=100", token).get("repositories", [])
        project_hint = _project_hint(prompt)
        selected = [repo for repo in repositories if project_hint in repo["name"].lower()] if project_hint else repositories
        if not selected:
            selected = repositories
        for repo in selected:
            try:
                response = self._request(f"/repos/{repo['full_name']}/contents/{quote(path, safe='/')}?ref={quote(repo['default_branch'], safe='')}", token)
                content = base64.b64decode(response.get("content", "")).decode("utf-8", errors="ignore")
            except (RuntimeError, ValueError, TypeError):
                continue
            self.last_diagnostics.append(f"{repo['full_name']}: arquivo {path} lido somente para análise.")
            return [RepositoryFile(
                repo["full_name"], path, f"{repo['html_url']}/blob/{repo['default_branch']}/{path}", repo["default_branch"],
                0, _source_summary(path, content), "analysis",
            )]
        self.last_diagnostics.append(f"Arquivo {path} não foi localizado nos repositórios selecionados.")
        return []

    def create_approved_pull_request(self, repository: str, base_branch: str, title: str,
                                     body: str, changes: list[tuple[str, str]]) -> str:
        """Aplica alterações já aprovadas exclusivamente numa branch nova e abre um PR."""
        if not self.enabled:
            raise RuntimeError("A integração GitHub ainda não foi conectada.")
        if not changes or len(changes) > 8:
            raise ValueError("Um Pull Request precisa ter entre 1 e 8 alterações aprovadas.")
        for path, _content in changes:
            _validate_change_path(path)
        token = self._installation_token()
        branch = _safe_branch_name(title)
        base = self._request(f"/repos/{repository}/git/ref/heads/{quote(base_branch, safe='')}", token)
        base_sha = base["object"]["sha"]
        self._request(f"/repos/{repository}/git/refs", token, method="POST",
                      payload={"ref": f"refs/heads/{branch}", "sha": base_sha})
        for path, content in changes:
            payload: dict[str, Any] = {
                "message": f"{title}: {path}", "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": branch,
            }
            try:
                existing = self._request(f"/repos/{repository}/contents/{quote(path, safe='/')}?ref={quote(base_branch, safe='')}", token)
                if existing.get("sha"):
                    payload["sha"] = existing["sha"]
            except RuntimeError:
                # Arquivo novo é permitido; a API valida a existência da branch e permissões.
                pass
            self._request(f"/repos/{repository}/contents/{quote(path, safe='/')}", token, method="PUT", payload=payload)
        pull = self._request(f"/repos/{repository}/pulls", token, method="POST",
                             payload={"title": title[:120], "head": branch, "base": base_branch, "body": body[:6000]})
        return str(pull["html_url"])

    def _search_code(self, repo: dict[str, Any], token: str, prompt: str) -> list[RepositoryFile]:
        """Pesquisa conteúdo e caminho; a árvore abaixo continua como fallback."""
        terms = _content_terms(prompt)
        matches: list[RepositoryFile] = []
        for term in terms:
            query = quote(f"{term} repo:{repo['full_name']}", safe="")
            try:
                response = self._request(f"/search/code?q={query}&per_page=10", token)
            except RuntimeError as error:
                self.last_diagnostics.append(f"{repo['full_name']}: pesquisa de conteúdo indisponível ({error}).")
                continue
            for item in response.get("items", []):
                matches.append(RepositoryFile(repo["full_name"], item["path"], item["html_url"], repo["default_branch"]))
        return matches

    def _rank_translation_candidates(self, files: list[RepositoryFile], token: str, prompt: str) -> list[RepositoryFile]:
        if not _is_translation_request(prompt):
            return files
        ranked: list[RepositoryFile] = []
        for item in files[:12]:
            try:
                response = self._request(f"/repos/{item.repository}/contents/{quote(item.path, safe='/')}?ref={quote(item.branch, safe='')}", token)
                raw_content = base64.b64decode(response.get("content", "")).decode("utf-8", errors="ignore")
            except (RuntimeError, ValueError, TypeError):
                raw_content = ""
            confidence, reason = _translation_confidence(item.path, raw_content)
            ranked.append(RepositoryFile(item.repository, item.path, item.url, item.branch, confidence, reason))
        # Resultado sem evidência é ruído (por exemplo, imagens retornadas pela busca ampla).
        return sorted((item for item in ranked if item.confidence > 0), key=lambda item: item.confidence, reverse=True)

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

    def _request(self, path: str, token: str, method: str = "GET", payload: Any = None) -> Any:
        request = Request(f"{self.api}{path}", method=method, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "my-agent-web",
            **({"Content-Type": "application/json"} if payload is not None else {}),
        }, data=json.dumps(payload).encode("utf-8") if payload is not None else None)
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
    explicit = re.search(r"(?:projeto|reposit[óo]rio)\s+([a-zA-Z0-9_-]+)", prompt, flags=re.IGNORECASE)
    if explicit:
        return explicit.group(1).lower()
    words = ["".join(char for char in word.lower() if char.isalnum() or char in "-_") for word in prompt.split()]
    ignored = {"arquivo", "arquivos", "projeto", "localize", "localizar", "encontre", "encontrar", "quero", "preciso", "que", "tenha", "com", "para", "pro", "por", "traducao", "ingles", "somente", "mude", "nada"}
    candidates = [word for word in words if len(word) >= 4 and word not in ignored]
    return candidates[0] if candidates else ""


def _matches(path: str, prompt: str) -> bool:
    normalized = "".join(char for char in prompt.lower() if char.isalnum() or char in " /._-")
    path_lower = path.lower()
    translation_request = _is_translation_request(normalized)
    if translation_request:
        return any(marker in path_lower for marker in ("i18n", "locale", "lang", "translation", "traducao", "/en.", "/en/", "english"))
    terms = [word for word in normalized.split() if len(word) >= 4]
    return any(term in path_lower for term in terms)


def _content_terms(prompt: str) -> list[str]:
    normalized = prompt.lower()
    if any(word in normalized for word in ("trad", "ingl", "english", "idioma", "locale")):
        return ["translation", "traducao", "i18n", "locale", "english", "pt-br", "en-us"]
    return [word for word in normalized.split() if len(word) >= 4][:5]


def _is_translation_request(prompt: str) -> bool:
    normalized = prompt.lower()
    return any(word in normalized for word in ("trad", "ingl", "english", "idioma", "locale"))


def _needs_text_source_map(prompt: str) -> bool:
    normalized = prompt.lower()
    return _is_translation_request(prompt) or (
        any(term in normalized for term in ("texto", "textos", "mensagem", "mensagens", "conteúdo", "conteudo"))
        and any(term in normalized for term in ("carrega", "exib", "usuário", "usuario", "interface", "tela"))
    )


def _directory_hint(prompt: str) -> str:
    """Extrai um caminho de pasta explícito, como apps/web/app/."""
    match = re.search(r"(?<!\S)((?:[\w.-]+/){1,}[\w.-]+/?)(?!\S)", prompt)
    # O caminho costuma vir no fim de uma frase: "apps/web/app/.".
    # Pontos, crases e barras de pontuação não pertencem ao diretório.
    return match.group(1).strip('/`.,;:!?)]}') if match else ""


def _file_hint(prompt: str) -> str:
    match = re.search(r"(?<!\S)((?:[\w.-]+/)+[\w.-]+\.(?:tsx?|jsx?|vue|svelte|html|php|json|py|rb))(?=$|\s|[.,;:!?`])", prompt, flags=re.IGNORECASE)
    return match.group(1).strip('/`.,;:!?)]}') if match else ""


def _is_text_source_file(path: str) -> bool:
    lower = path.lower()
    extensions = (".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte", ".html", ".php", ".json", ".md", ".css", ".scss")
    return lower.endswith(extensions) and not any(part in lower for part in ("/public/", "/assets/", "/images/", "/node_modules/"))


def _file_role(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    lower = path.lower()
    if name.startswith("page."):
        return "página da interface; provável origem de textos visíveis"
    if name.startswith("layout."):
        return "layout compartilhado da interface"
    if name.startswith("loading.") or name.startswith("error."):
        return "estado de carregamento ou erro exibido ao usuário"
    if name.startswith("route.") or "/api/" in lower:
        return "rota/API; pode fornecer dados, não costuma renderizar textos diretamente"
    if any(marker in lower for marker in ("i18n", "locale", "translation", "traducao")):
        return "configuração ou catálogo de tradução"
    if "/components/" in lower:
        return "componente reutilizável da interface"
    return "arquivo de código/texto a analisar na interface"


def _source_summary(path: str, content: str) -> str:
    """Resumo heurístico: útil para triagem, sem fingir entendimento de especialista."""
    lines = len(content.splitlines())
    signals: list[str] = [_file_role(path), f"{lines} linhas"]
    lower = content.lower()
    if "use client" in lower:
        signals.append("componente executado no navegador")
    if re.search(r"\b(fetch|axios\.|trpc\.|usequery\b)", content, flags=re.IGNORECASE):
        signals.append("faz ou consome chamadas de dados/API")
    if re.search(r"\b(useState|useEffect|useMemo)\b", content):
        signals.append("tem estado ou comportamento interativo")
    texts = _visible_text_samples(content)
    if texts:
        signals.append("textos aparentes: " + ", ".join(f'“{text}”' for text in texts))
    return "; ".join(signals) + "."


def _visible_text_samples(content: str, limit: int = 4) -> list[str]:
    samples: list[str] = []
    patterns = (
        # Conteúdo diretamente renderizado por JSX: <h1>Meu painel</h1>.
        r">\s*([^<>{}\n]{3,80}?)\s*<",
        # Rótulos acessíveis e campos de formulário também são visíveis ao usuário.
        r"(?:aria-label|placeholder|title|label)\s*=\s*['\"]([^'\"\n]{3,80})['\"]",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, content, flags=re.IGNORECASE):
            text = re.sub(r"\s+", " ", match.group(1)).strip()
            lower = text.lower()
            if any(marker in lower for marker in ("/", "http", "class", "import", "use client", "@/")):
                continue
            if not re.search(r"[a-záàâãéêíóôõúç]", lower):
                continue
            if text not in samples:
                samples.append(text)
            if len(samples) >= limit:
                return samples
    return samples


def _safe_branch_name(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")[:48] or "alteracao"
    return f"my-agent/{slug}-{int(time.time())}"


def _validate_change_path(path: str) -> None:
    normalized = path.strip().lstrip("/")
    blocked = (".env", ".pem", ".key", "id_rsa", ".github/workflows/")
    if not normalized or ".." in normalized.split("/") or any(part in normalized.lower() for part in blocked):
        raise ValueError("A alteração inclui um arquivo sensível ou caminho inválido.")


def _text_source_directories(repo: dict[str, Any], tree: list[dict[str, Any]]) -> list[RepositoryFile]:
    """Agrupa qualquer diretório com código/texto, mesmo sem convenções de framework."""
    extensions = {
        ".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte", ".html", ".htm", ".php", ".py", ".rb",
        ".java", ".kt", ".go", ".cs", ".json", ".yaml", ".yml", ".xml", ".md", ".txt", ".css", ".scss",
        ".sql", ".twig", ".blade.php", ".cshtml", ".ejs", ".hbs",
    }
    ignored = ("node_modules/", "/public/", "/assets/", "/images/", "/pdf-", "/vendor/", "/dist/", "/build/", "/coverage/")
    directories: dict[str, int] = {}
    for item in tree:
        path = item.get("path", "")
        lower = path.lower()
        if item.get("type") != "blob" or any(part in lower for part in ignored):
            continue
        if not any(lower.endswith(extension) for extension in extensions):
            continue
        parts = path.split("/")
        # Agrupar no máximo três níveis deixa o resultado navegável, sem exigir
        # a convenção src/pages/components do framework usado pelo projeto.
        directory = "/".join(parts[:min(len(parts) - 1, 3)]) or "."
        directories[directory] = directories.get(directory, 0) + 1
    ordered = sorted(directories.items(), key=lambda item: (-item[1], item[0]))
    return [
        RepositoryFile(
            repo["full_name"], (directory + "/") if directory != "." else "./",
            f"{repo['html_url']}/tree/{repo['default_branch']}" + (f"/{directory}" if directory != "." else ""),
            repo["default_branch"], min(80, 30 + count * 3), f"{count} arquivos de código que podem carregar textos da interface", "directory",
        )
        for directory, count in ordered
    ]


def _translation_confidence(path: str, content: str) -> tuple[int, str]:
    path_lower, text = path.lower(), content.lower()
    score, signals = 0, []
    if any(marker in path_lower for marker in ("/en.", "/en/", "english", "locale", "i18n", "translation", "traducao")):
        score += 35
        signals.append("caminho de localização")
    if any(marker in text for marker in ('"en"', "'en'", "en-us", "en_gb", "english")):
        score += 40
        signals.append("referência ao inglês")
    if any(marker in text for marker in ("pt-br", "pt_br", "portuguese", "portugues")):
        score += 20
        signals.append("referência ao português")
    if any(marker in text for marker in ("translation", "translations", "i18n", "locale")):
        score += 15
        signals.append("estrutura de tradução")
    return min(score, 100), ", ".join(signals) or "nome/caminho correspondente"
