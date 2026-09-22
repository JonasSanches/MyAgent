from __future__ import annotations

from .codex import CodexClient, CodexResult
from .config import Config
from .memory import Memory
from .permissions import ActionRisk, PermissionPolicy
from typing import List, Optional
from .skills import SkillRegistry


BASE_INSTRUCTIONS = """Você é um agente pessoal de desenvolvimento pragmático.
Responda em português, salvo pedido contrário. Entregue a menor solução correta,
aponte riscos reais e não invente contexto. Não execute ações: apenas oriente com
passos ou código conciso. Priorize conhecimento verificado que acompanha a tarefa.
Se não for suficiente, produza uma solução completa e inclua testes objetivos."""

SPECIALIST_INSTRUCTIONS = """Você foi chamado como especialista porque o agente não encontrou
conhecimento local validado suficiente. Investigue a lacuna com profundidade, proponha a menor
solução segura e inclua critérios claros para verificar a implementação. Não afirme que algo foi
testado sem evidência."""

ROUTINE_INSTRUCTIONS = """Você é a camada de rotina de um agente de desenvolvimento.
Resolva apenas tarefas textuais bem delimitadas e de baixo risco com rigor técnico. Se houver
incerteza relevante, impacto em segurança, arquitetura, banco de dados ou produção, declare a
lacuna para que a tarefa seja escalada ao especialista. Inclua critérios objetivos de teste."""

CRITICAL_TERMS = {
    "segurança", "security", "arquitetura", "architecture", "migração", "migration",
    "banco", "database", "produção", "production", "deploy", "destrutiv", "pagamento",
    "credencial", "autenticação", "authentication",
}


class SolveResult:
    def __init__(self, route: str, attempt_id: int | None = None, solution: str = "", message: str = ""):
        self.route = route
        self.attempt_id = attempt_id
        self.solution = solution
        self.message = message


class PersonalDevAgent:
    def __init__(self, config: Config):
        self.config = config
        self.memory = Memory(config.database)
        self.skills = SkillRegistry(config.skills_dir)
        self.codex = CodexClient(config.model, config.max_output_tokens, config.reasoning_effort, config.text_verbosity)
        self.routine = CodexClient(config.routine_model, config.max_output_tokens, "high", config.text_verbosity)
        self.permissions = PermissionPolicy()

    def build_context(self, prompt: str) -> str:
        memories = self.memory.search(prompt, limit=5)
        skills = self.skills.relevant(prompt)
        knowledge = self.memory.search_knowledge(prompt, limit=2)
        sections = [BASE_INSTRUCTIONS]
        if memories:
            sections.append("Memórias relevantes:\n" + "\n".join(f"- {m.content}" for m in memories))
        if skills:
            sections.append(
                "Habilidades aplicáveis:\n" + "\n\n".join(
                    f"## {skill.name}\n{skill.instructions}" for skill in skills
                )
            )
        if knowledge:
            sections.append(
                "Conhecimento validado relacionado (adapte, não copie cegamente):\n" + "\n".join(
                    f"- {item.title}: {item.solution[:500]}" for item in knowledge
                )
            )
        return "\n\n".join(sections)

    def can_spend(self) -> tuple[bool, str]:
        requests, tokens = self.memory.usage_today()
        if requests >= self.config.daily_request_limit:
            return False, f"Limite diário de {self.config.daily_request_limit} consultas atingido."
        if tokens >= self.config.daily_token_budget:
            return False, f"Orçamento diário de {self.config.daily_token_budget} tokens atingido."
        return True, "ok"

    def ask_codex(self, prompt: str, as_specialist: bool = False, image_data_urls: Optional[List[str]] = None,
                  use_web_search: bool = False) -> CodexResult:
        allowed, reason = self.can_spend()
        if not allowed:
            raise RuntimeError(reason)
        instructions = self.build_context(prompt)
        if as_specialist:
            instructions += "\n\n" + SPECIALIST_INSTRUCTIONS
        result = self.codex.ask(prompt, instructions, image_data_urls=image_data_urls, use_web_search=use_web_search)
        self.memory.record_usage(result.total_tokens)
        return result

    def ask_routine(self, prompt: str) -> CodexResult:
        allowed, reason = self.can_spend()
        if not allowed:
            raise RuntimeError(reason)
        result = self.routine.ask(prompt, self.build_context(prompt) + "\n\n" + ROUTINE_INSTRUCTIONS)
        self.memory.record_usage(result.total_tokens)
        return result

    def specialist_route(self, prompt: str, image_data_urls: Optional[List[str]] = None,
                         use_web_search: bool = False) -> str:
        words = prompt.lower()
        critical = any(term in words for term in CRITICAL_TERMS)
        if image_data_urls or use_web_search or critical or self.memory.has_failed_attempt(prompt):
            return "codex"
        return "routine" if self.config.routine_enabled else "codex"

    def solve(self, prompt: str, use_codex: bool = False, image_data_urls: Optional[List[str]] = None,
              use_web_search: bool = False) -> SolveResult:
        knowledge = self.memory.search_knowledge(prompt, limit=1)
        if knowledge and knowledge[0].relevance >= 0.34:
            item = knowledge[0]
            attempt = self.memory.create_attempt(prompt, item.solution, "knowledge", item.id)
            return SolveResult("knowledge", attempt.id, item.solution,
                               f"Conhecimento verificado: {item.title} ({item.relevance:.0%} de cobertura).")
        if not use_codex:
            gap = self.memory.create_attempt(prompt, "", "knowledge_gap", status="needs_specialist")
            return SolveResult("needs_codex", gap.id, message="Não há conhecimento verificado suficientemente parecido.")
        decision = self.permissions.decide(ActionRisk.CODEX)
        if not decision.requires_confirmation:
            raise RuntimeError("Política de permissão inválida para consulta ao Codex.")
        route = self.specialist_route(prompt, image_data_urls, use_web_search)
        if route == "routine":
            result = self.ask_routine(prompt)
            attempt = self.memory.create_attempt(prompt, result.text, "routine")
            return SolveResult("routine", attempt.id, result.text, f"Modelo de rotina consultado: {result.total_tokens} tokens.")
        result = self.ask_codex(prompt, as_specialist=True, image_data_urls=image_data_urls, use_web_search=use_web_search)
        attempt = self.memory.create_attempt(prompt, result.text, "codex")
        return SolveResult("codex", attempt.id, result.text, f"Codex especialista consultado: {result.total_tokens} tokens.")
