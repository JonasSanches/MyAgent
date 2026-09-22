from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ActionRisk(str, Enum):
    READ = "leitura local"
    ROUTINE = "rotina local"
    CODEX = "consulta ao Codex"
    PERMANENT_KNOWLEDGE = "conhecimento permanente"
    MODIFY = "alteração de arquivos"
    DESTRUCTIVE = "ação destrutiva"
    EXTERNAL = "ação externa"


@dataclass(frozen=True)
class PermissionDecision:
    action: ActionRisk
    requires_confirmation: bool
    reason: str


class PermissionPolicy:
    """Política pequena e explícita; novas ferramentas devem consultá-la antes de agir."""

    def decide(self, action: ActionRisk) -> PermissionDecision:
        if action in {ActionRisk.READ, ActionRisk.ROUTINE}:
            return PermissionDecision(action, False, "Autonomia para leitura, busca local, histórico e testes solicitados.")
        if action == ActionRisk.CODEX:
            return PermissionDecision(action, True, "Há custo e envio de contexto para a API.")
        if action == ActionRisk.PERMANENT_KNOWLEDGE:
            return PermissionDecision(action, True, "A solução passará a influenciar tarefas futuras.")
        if action == ActionRisk.MODIFY:
            return PermissionDecision(action, True, "A alteração pode modificar o projeto do usuário.")
        if action == ActionRisk.DESTRUCTIVE:
            return PermissionDecision(action, True, "A ação pode remover ou sobrescrever dados.")
        return PermissionDecision(action, True, "A ação alcança um sistema externo.")

    def describe(self) -> list[PermissionDecision]:
        return [self.decide(action) for action in ActionRisk]
