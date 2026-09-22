from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

from .security import redact_secrets


@dataclass(frozen=True)
class CodexResult:
    text: str
    total_tokens: int


class CodexClient:
    endpoint = "https://api.openai.com/v1/responses"

    def __init__(self, model: str, max_output_tokens: int, reasoning_effort: str = "high", text_verbosity: str = "medium"):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.text_verbosity = text_verbosity

    def ask(self, prompt: str, instructions: str, image_data_urls: Optional[List[str]] = None,
            use_web_search: bool = False) -> CodexResult:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("Defina OPENAI_API_KEY antes de consultar o Codex.")
        images = image_data_urls or []
        input_data = prompt
        if images:
            input_data = [{
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}] + [
                    {"type": "input_image", "image_url": image_url, "detail": "auto"}
                    for image_url in images
                ],
            }]
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": input_data,
            "reasoning": {"effort": self.reasoning_effort},
            "text": {"verbosity": self.text_verbosity},
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }
        if use_web_search:
            payload["tools"] = [{"type": "web_search"}]
            payload["include"] = ["web_search_call.action.sources"]
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if error.code == 401:
                raise RuntimeError("A OpenAI recusou a chave. Revogue a chave exposta, crie outra e reinicie o servidor.") from error
            if error.code == 429 and "credit_balance_exhausted" in detail:
                raise RuntimeError("A chave está válida, mas o projeto da OpenAI está sem créditos. Adicione saldo em Billing antes de consultar o Codex.") from error
            raise RuntimeError(f"Erro da OpenAI ({error.code}): {redact_secrets(detail)}") from error
        text_parts = []
        for item in data.get("output", []):
            if item.get("type") == "message":
                text_parts.extend(
                    part.get("text", "") for part in item.get("content", [])
                    if part.get("type") == "output_text"
                )
        usage = data.get("usage", {})
        return CodexResult("\n".join(text_parts).strip(), int(usage.get("total_tokens", 0)))
