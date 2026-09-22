import tempfile
import unittest
import sys
import subprocess
import shlex
import urllib.error
import io
import os
from pathlib import Path

from dev_agent.codex import CodexResult
from dev_agent.auth import SessionAuth
from dev_agent.config import Config
from dev_agent.core import PersonalDevAgent
from dev_agent.server import AgentWebService
from dev_agent.github import RepositoryFile
from dev_agent.github import _content_terms, _project_hint
from dev_agent.security import redact_secrets


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "skills" / "testar").mkdir(parents=True)
        (root / "skills" / "testar" / "SKILL.md").write_text(
            "---\nname: testar\ndescription: testar código e testes\n---\nRode o menor teste.\n",
            encoding="utf-8",
        )
        self.config = Config(root, "fake-codex", 100, 2, 500, routine_enabled=False)
        self.agent = PersonalDevAgent(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def test_memory_round_trip(self):
        self.agent.memory.remember("Usar Python", "preferencia")
        self.assertEqual(self.agent.memory.search("Python")[0].content, "Usar Python")

    def test_skill_is_selected(self):
        self.assertIn("Rode o menor teste", self.agent.build_context("quero testar código"))

    def test_usage_limit_blocks_extra_request(self):
        self.agent.codex.ask = lambda *_, **__: CodexResult("ok", 250)
        self.agent.ask_codex("um")
        self.agent.ask_codex("dois")
        with self.assertRaisesRegex(RuntimeError, "Limite diário"):
            self.agent.ask_codex("três")

    def test_known_solution_is_reused_without_codex(self):
        self.agent.memory.add_knowledge("Somar números", "somar dois números inteiros", "Use a + b.", "python -m unittest", "python matematica")
        self.agent.codex.ask = lambda *_, **__: self.fail("Codex não deveria ser chamado")
        result = self.agent.solve("como somar dois números inteiros")
        self.assertEqual(result.route, "knowledge")
        self.assertIn("a + b", result.solution)

    def test_passing_test_waits_for_human_approval_before_promoting(self):
        self.agent.codex.ask = lambda *_, **__: CodexResult("Solução proposta", 100)
        result = self.agent.solve("criar parser json", use_codex=True)
        test_result = self.agent.memory.record_test_result(result.attempt_id, True, "ok", f"{sys.executable} -m unittest")
        self.assertEqual(test_result.attempt.status, "awaiting_approval")
        self.assertEqual(self.agent.memory.search_knowledge("parser json"), [])
        knowledge_id = self.agent.memory.approve_attempt(result.attempt_id)
        learned = self.agent.memory.search_knowledge("parser json")
        self.assertEqual(knowledge_id, learned[0].id)
        self.assertEqual(learned[0].solution, "Solução proposta")

    def test_failed_test_does_not_promote(self):
        attempt = self.agent.memory.create_attempt("tarefa nova", "solução", "codex")
        result = self.agent.memory.record_test_result(attempt.id, False, "erro", "false")
        self.assertEqual(result.attempt.status, "failed")
        self.assertEqual(self.agent.memory.search_knowledge("tarefa nova"), [])

    def test_default_remote_quality_is_high(self):
        self.assertEqual(self.agent.codex.reasoning_effort, "high")
        self.assertEqual(self.agent.codex.text_verbosity, "medium")

    def test_small_real_task_reuses_knowledge_and_runs_a_real_test(self):
        knowledge_id = self.agent.memory.add_knowledge(
            "Somar inteiros", "somar dois números inteiros", "Use: resultado = a + b", "python -c 'assert 19 + 23 == 42'", "python soma"
        )
        self.agent.codex.ask = lambda *_, **__: self.fail("A tarefa conhecida não deve consultar o Codex")
        result = self.agent.solve("preciso somar dois números inteiros")
        self.assertEqual(result.route, "knowledge")
        completed = subprocess.run([sys.executable, "-c", "assert 19 + 23 == 42"], capture_output=True, text=True)
        test_result = self.agent.memory.record_test_result(result.attempt_id, completed.returncode == 0, completed.stdout + completed.stderr, "python -c 'assert 19 + 23 == 42'")
        self.assertTrue(test_result.passed)
        self.assertEqual(test_result.attempt.status, "verified")
        self.assertEqual(self.agent.memory.search_knowledge("somar inteiros")[0].id, knowledge_id)

    def test_web_flow_keeps_new_solution_pending_until_approved(self):
        service = AgentWebService(self.agent)
        self.agent.codex.ask = lambda *_, **__: CodexResult("Implementar a função de soma.", 120)
        gap = service.chat("criar uma função de soma")
        self.assertEqual(gap["kind"], "confirmation")
        solution = service.chat("criar uma função de soma", authorize_codex=True)
        self.assertEqual(solution["kind"], "solution")
        checked = service.complete(solution["attempt_id"], f"{shlex.quote(sys.executable)} -c 'assert 2 + 2 == 4'", ".")
        self.assertTrue(checked["passed"])
        self.assertTrue(checked["requires_approval"])
        stored = service.approve(solution["attempt_id"])
        self.assertIn("knowledge_id", stored)

    def test_api_key_is_redacted_before_history_storage(self):
        secret = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
        attempt = self.agent.memory.create_attempt(f"usar {secret}", secret, "manual")
        self.assertNotIn(secret, attempt.prompt)
        self.assertIn("[CHAVE_OPENAI_OCULTA]", attempt.solution)
        self.assertEqual(redact_secrets(secret), "[CHAVE_OPENAI_OCULTA]")

    def test_credit_error_is_explained_without_raw_api_json(self):
        client = self.agent.codex
        original = urllib.request.urlopen
        previous_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "sk-proj-test-key-not-real-123456789"
        urllib.request.urlopen = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.HTTPError("https://api.openai.com", 429, "quota", {}, io.BytesIO(b'{"error":{"code":"credit_balance_exhausted"}}'))
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "sem créditos"):
                client.ask("teste", "instruções")
        finally:
            urllib.request.urlopen = original
            if previous_key is None:
                del os.environ["OPENAI_API_KEY"]
            else:
                os.environ["OPENAI_API_KEY"] = previous_key

    def test_print_and_link_wait_for_confirmation_then_reach_specialist(self):
        service = AgentWebService(self.agent)
        image = "data:image/png;base64,QUJDRA=="
        seen = {}
        def specialist(*_, **kwargs):
            seen.update(kwargs)
            return CodexResult("Análise pronta", 80)
        self.agent.codex.ask = specialist
        prompt = "Interprete https://example.com e este print"
        self.assertEqual(service.chat(prompt, images=[image])["kind"], "confirmation")
        answer = service.chat(prompt, authorize_codex=True, images=[image])
        self.assertEqual(answer["kind"], "solution")
        self.assertEqual(seen["image_data_urls"], [image])
        self.assertTrue(seen["use_web_search"])

    def test_routine_model_handles_simple_unknown_task_when_enabled(self):
        config = Config(self.config.root, "fake-codex", 100, 2, 500, routine_enabled=True)
        agent = PersonalDevAgent(config)
        agent.routine.ask = lambda *_, **__: CodexResult("Solução de rotina", 45)
        agent.codex.ask = lambda *_, **__: self.fail("Não deveria escalar uma tarefa simples")
        result = agent.solve("criar função para formatar um nome", use_codex=True)
        self.assertEqual(result.route, "routine")
        self.assertEqual(result.solution, "Solução de rotina")

    def test_critical_task_bypasses_routine_and_uses_codex(self):
        config = Config(self.config.root, "fake-codex", 100, 2, 500, routine_enabled=True)
        agent = PersonalDevAgent(config)
        agent.routine.ask = lambda *_, **__: self.fail("Tarefa crítica não pode usar a camada de rotina")
        agent.codex.ask = lambda *_, **__: CodexResult("Análise especializada", 90)
        result = agent.solve("planejar migração de banco de dados", use_codex=True)
        self.assertEqual(result.route, "codex")
        self.assertEqual(result.solution, "Análise especializada")

    def test_private_session_is_signed_and_expires_only_after_valid_signature(self):
        auth = SessionAuth("senha-segura", "s" * 40, cookie_secure=True)
        token = auth.login("senha-segura")
        self.assertIsNotNone(token)
        self.assertTrue(auth.authenticated(f"{auth.cookie_name}={token}"))
        self.assertFalse(auth.authenticated(f"{auth.cookie_name}={token}alterado"))
        self.assertIn("HttpOnly", auth.cookie(token))
        self.assertIn("Secure", auth.cookie(token))

    def test_public_host_requires_password_secret_and_https_cookie(self):
        with self.assertRaisesRegex(ValueError, "APP_PASSWORD"):
            self.config.validate_for_host("0.0.0.0")
        configured = Config(
            self.config.root, "fake-codex", 100, 2, 500,
            app_password="senha", session_secret="s" * 40, cookie_secure=True,
        )
        configured.validate_for_host("0.0.0.0")

    def test_test_directory_cannot_escape_configured_workspace(self):
        service = AgentWebService(self.agent)
        with self.assertRaisesRegex(ValueError, "DEV_AGENT_WORKSPACE"):
            service._safe_workdir("../fora")

    def test_maturity_tracks_local_reuse_separately_from_external_models(self):
        knowledge_id = self.agent.memory.add_knowledge("Formato", "formatar nome", "Use title.", "true")
        reused = self.agent.memory.create_attempt("formatar nome", "Use title.", "knowledge", knowledge_id)
        self.agent.memory.record_test_result(reused.id, True, "ok", "true")
        self.agent.memory.create_attempt("tarefa nova", "resposta", "routine")
        stats = self.agent.memory.maturity_stats()
        self.assertEqual(stats["local_solutions"], 1)
        self.assertEqual(stats["external_consultations"], 1)
        self.assertEqual(stats["local_rate"], 50)

    def test_repository_lookup_uses_github_read_only_without_model_call(self):
        service = AgentWebService(self.agent)
        class FakeGitHub:
            enabled = True
            last_diagnostics = []
            def find_files(self, _prompt):
                return [RepositoryFile("ariane/vendamais", "locales/en.json", "https://github.com/ariane/vendamais/blob/main/locales/en.json")]
        service.github = FakeGitHub()
        self.agent.codex.ask = lambda *_, **__: self.fail("Busca no GitHub não consulta modelo")
        result = service.chat("Encontre o arquivo do projeto vendamais com tradução para inglês")
        self.assertEqual(result["source"], "github")
        self.assertIn("locales/en.json", result["solution"])

    def test_repository_lookup_extracts_project_name_from_portuguese_request(self):
        self.assertEqual(_project_hint("Encontre o arquivo do projeto Venda-SaaS com tradução"), "venda-saas")

    def test_translation_request_uses_content_search_terms(self):
        self.assertIn("i18n", _content_terms("Localize tradução para inglês"))


if __name__ == "__main__":
    unittest.main()
