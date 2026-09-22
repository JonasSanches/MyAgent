from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from typing import List, Optional

from .config import Config
from .core import PersonalDevAgent
from .permissions import ActionRisk


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="dev-agent")
    sub = command.add_subparsers(dest="command", required=True)
    remember = sub.add_parser("remember", help="Salva uma memória local; custo zero")
    remember.add_argument("text")
    remember.add_argument("--tags", default="")
    recall = sub.add_parser("recall", help="Busca memórias locais; custo zero")
    recall.add_argument("query", nargs="?", default="")
    sub.add_parser("skills", help="Lista habilidades; custo zero")
    sub.add_parser("status", help="Mostra orçamento diário; custo zero")
    ask = sub.add_parser("ask", help="Consulta explicitamente o Codex; pode gerar custo")
    ask.add_argument("prompt")
    ask.add_argument("--yes", action="store_true", help="Pula a confirmação")
    solve = sub.add_parser("solve", help="Busca conhecimento; consulta Codex somente se necessário")
    solve.add_argument("prompt")
    solve.add_argument("--yes", action="store_true", help="Autoriza a consulta ao Codex se não houver conhecimento")
    complete = sub.add_parser("complete", help="Roda teste e promove tentativa aprovada a conhecimento")
    complete.add_argument("attempt", type=int)
    complete.add_argument("--test", required=True, help="Comando de teste, sem shell; ex.: 'python3 -m unittest'")
    complete.add_argument("--workdir", default=".", help="Diretório do projeto a testar")
    complete.add_argument("--yes", action="store_true", help="Aprova conhecimento permanente depois do teste")
    complete.add_argument("--no-approve", action="store_true", help="Mantém a solução pendente de aprovação")
    approve = sub.add_parser("approve", help="Aprova uma solução testada como conhecimento permanente")
    approve.add_argument("attempt", type=int)
    reject = sub.add_parser("reject", help="Mantém uma solução testada fora do conhecimento permanente")
    reject.add_argument("attempt", type=int)
    sub.add_parser("history", help="Lista o histórico de tentativas e resultados")
    sub.add_parser("knowledge", help="Lista somente o conhecimento permanente validado")
    sub.add_parser("permissions", help="Mostra quais ações pedem confirmação")
    return command


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    agent = PersonalDevAgent(Config.load())
    if args.command == "remember":
        identifier = agent.memory.remember(args.text, args.tags)
        print(f"Memória #{identifier} salva localmente.")
    elif args.command == "recall":
        items = agent.memory.search(args.query, limit=10)
        print("\n".join(f"#{item.id} [{item.tags or '-'}] {item.content}" for item in items) or "Nada encontrado.")
    elif args.command == "skills":
        print("\n".join(f"- {skill.name}: {skill.description}" for skill in agent.skills.all()))
    elif args.command == "status":
        requests, tokens = agent.memory.usage_today()
        print(f"Hoje: {requests}/{agent.config.daily_request_limit} consultas; {tokens}/{agent.config.daily_token_budget} tokens.")
    elif args.command == "ask":
        allowed, reason = agent.can_spend()
        if not allowed:
            print(reason, file=sys.stderr)
            return 2
        if not args.yes:
            estimate = max(1, len(args.prompt) // 4)
            answer = input(f"Consulta paga (~{estimate} tokens de entrada + até {agent.config.max_output_tokens} de saída). Continuar? [s/N] ")
            if answer.lower() not in {"s", "sim", "y", "yes"}:
                print("Cancelado sem usar créditos.")
                return 0
        try:
            result = agent.ask_codex(args.prompt)
        except RuntimeError as error:
            print(str(error), file=sys.stderr)
            return 2
        print(result.text)
        print(f"\n[uso: {result.total_tokens} tokens]")
    elif args.command == "solve":
        result = agent.solve(args.prompt)
        if result.route == "needs_codex":
            print(result.message)
            if not args.yes:
                answer = input("Consultar o Codex com raciocínio de alta qualidade? [s/N] ")
                if answer.lower() not in {"s", "sim", "y", "yes"}:
                    print("Encerrado sem usar créditos.")
                    return 0
            try:
                result = agent.solve(args.prompt, use_codex=True)
            except RuntimeError as error:
                print(str(error), file=sys.stderr)
                return 2
        print(result.message)
        print(result.solution)
        print(f"\n[tentativa #{result.attempt_id}; aplique a solução e rode: dev-agent complete {result.attempt_id} --test '…']")
    elif args.command == "complete":
        try:
            completed = subprocess.run(
                shlex.split(args.test), cwd=args.workdir, text=True, capture_output=True, timeout=300, check=False
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            print(f"Não foi possível executar o teste: {error}", file=sys.stderr)
            return 2
        output = (completed.stdout + completed.stderr).strip()
        result = agent.memory.record_test_result(args.attempt, completed.returncode == 0, output, args.test)
        if completed.returncode:
            print(f"Teste falhou; tentativa #{args.attempt} não foi aprendida.\n{output}", file=sys.stderr)
            return completed.returncode or 1
        if result.attempt.status == "verified":
            print("Teste passou; o conhecimento já era permanente e sua confiança foi atualizada.")
            return 0
        print("Teste passou. Resumo do possível aprendizado:\n\n" + result.learning_summary)
        approve_now = args.yes
        if not args.yes and not args.no_approve:
            try:
                answer = input("Aprovar como conhecimento permanente? [s/N] ")
                approve_now = answer.lower() in {"s", "sim", "y", "yes"}
            except EOFError:
                approve_now = False
        if approve_now:
            knowledge_id = agent.memory.approve_attempt(args.attempt)
            print(f"Conhecimento #{knowledge_id} aprovado e salvo permanentemente.")
        else:
            print(f"Tentativa #{args.attempt} permanece validada, aguardando aprovação. Use: dev-agent approve {args.attempt}")
    elif args.command == "approve":
        decision = agent.permissions.decide(ActionRisk.PERMANENT_KNOWLEDGE)
        if not decision.requires_confirmation:
            print("Política de permissão inválida.", file=sys.stderr)
            return 2
        try:
            knowledge_id = agent.memory.approve_attempt(args.attempt)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        print(f"Conhecimento #{knowledge_id} aprovado e salvo permanentemente.")
    elif args.command == "reject":
        try:
            agent.memory.reject_attempt(args.attempt)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        print(f"Tentativa #{args.attempt} mantida somente no histórico.")
    elif args.command == "history":
        entries = agent.memory.history()
        print("\n".join(f"#{item.id} [{item.status}/{item.approval_status}] {item.source}: {item.prompt}" for item in entries) or "Histórico vazio.")
    elif args.command == "knowledge":
        entries = agent.memory.all_knowledge()
        print("\n".join(f"#{item.id} [sucessos: {item.success_count}] {item.title}" for item in entries) or "Conhecimento permanente vazio.")
    elif args.command == "permissions":
        for decision in agent.permissions.describe():
            mode = "CONFIRMAR" if decision.requires_confirmation else "AUTÔNOMO"
            print(f"- {decision.action.value}: {mode} — {decision.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
