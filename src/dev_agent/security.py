from __future__ import annotations

import re


_OPENAI_KEY = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b")


def redact_secrets(text: str) -> str:
    """Remove chaves da OpenAI antes de qualquer dado chegar ao armazenamento ou à interface."""
    return _OPENAI_KEY.sub("[CHAVE_OPENAI_OCULTA]", text)
