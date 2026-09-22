from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .security import redact_secrets


@dataclass(frozen=True)
class MemoryItem:
    id: int
    content: str
    tags: str
    created_at: str


@dataclass(frozen=True)
class KnowledgeItem:
    id: int
    title: str
    problem: str
    solution: str
    tags: str
    test_command: str
    verified_at: str
    success_count: int
    relevance: float = 0.0


@dataclass(frozen=True)
class Attempt:
    id: int
    prompt: str
    solution: str
    source: str
    knowledge_id: Optional[int]
    status: str
    approval_status: str = "not_required"


@dataclass(frozen=True)
class TestResult:
    attempt: Attempt
    passed: bool
    learning_summary: str


class Memory:
    def __init__(self, database: Path):
        database.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage (
                    day TEXT PRIMARY KEY,
                    requests INTEGER NOT NULL DEFAULT 0,
                    tokens INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS knowledge (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    problem TEXT NOT NULL,
                    solution TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    test_command TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt TEXT NOT NULL,
                    solution TEXT NOT NULL,
                    source TEXT NOT NULL,
                    knowledge_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'proposed',
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    test_output TEXT NOT NULL DEFAULT '',
                    test_command TEXT NOT NULL DEFAULT '',
                    approval_status TEXT NOT NULL DEFAULT 'not_required',
                    FOREIGN KEY(knowledge_id) REFERENCES knowledge(id)
                );
                """
            )
            self._ensure_column(connection, "attempts", "test_command", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "attempts", "approval_status", "TEXT NOT NULL DEFAULT 'not_required'")
            self._sanitize_existing_data(connection)

    @staticmethod
    def _sanitize_existing_data(connection: sqlite3.Connection) -> None:
        fields = {
            "memories": ("content", "tags"),
            "knowledge": ("title", "problem", "solution", "tags", "test_command"),
            "attempts": ("prompt", "solution", "test_output", "test_command"),
        }
        for table, columns in fields.items():
            rows = connection.execute(f"SELECT id, {', '.join(columns)} FROM {table}").fetchall()
            for row in rows:
                replacement = {column: redact_secrets(row[column]) for column in columns}
                if any(replacement[column] != row[column] for column in columns):
                    assignments = ", ".join(f"{column} = ?" for column in columns)
                    connection.execute(
                        f"UPDATE {table} SET {assignments} WHERE id = ?",
                        (*[replacement[column] for column in columns], row["id"]),
                    )

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def remember(self, content: str, tags: str = "") -> int:
        text = redact_secrets(content.strip())
        if not text:
            raise ValueError("A memória não pode estar vazia.")
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO memories(content, tags, created_at) VALUES (?, ?, ?)",
                (text, tags.strip(), datetime.now(timezone.utc).isoformat()),
            )
            return int(cursor.lastrowid)

    def search(self, query: str = "", limit: int = 5) -> list[MemoryItem]:
        pattern = f"%{query.strip()}%"
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, content, tags, created_at FROM memories
                   WHERE content LIKE ? OR tags LIKE ?
                   ORDER BY id DESC LIMIT ?""",
                (pattern, pattern, limit),
            ).fetchall()
        return [MemoryItem(**dict(row)) for row in rows]

    def usage_today(self) -> tuple[int, int]:
        day = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT requests, tokens FROM usage WHERE day = ?", (day,)
            ).fetchone()
        return (int(row["requests"]), int(row["tokens"])) if row else (0, 0)

    def record_usage(self, tokens: int) -> None:
        day = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO usage(day, requests, tokens) VALUES (?, 1, ?)
                   ON CONFLICT(day) DO UPDATE SET
                   requests = requests + 1, tokens = tokens + excluded.tokens""",
                (day, max(0, tokens)),
            )

    def add_knowledge(self, title: str, problem: str, solution: str, test_command: str, tags: str = "") -> int:
        values = tuple(redact_secrets(value.strip()) for value in (title, problem, solution, tags, test_command))
        if not all((values[0], values[1], values[2], values[4])):
            raise ValueError("Conhecimento exige título, problema, solução e teste aprovado.")
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO knowledge(title, problem, solution, tags, test_command, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (*values, datetime.now(timezone.utc).isoformat()),
            )
            return int(cursor.lastrowid)

    def search_knowledge(self, query: str, limit: int = 3) -> list[KnowledgeItem]:
        query_words = _terms(query)
        if not query_words:
            return []
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM knowledge ORDER BY id DESC").fetchall()
        matches = []
        for row in rows:
            item = KnowledgeItem(**dict(row))
            document_words = _terms(f"{item.title} {item.problem} {item.tags}")
            overlap = len(query_words & document_words)
            relevance = overlap / len(query_words)
            if overlap:
                matches.append(KnowledgeItem(**{**item.__dict__, "relevance": relevance}))
        return sorted(matches, key=lambda item: (item.relevance, item.success_count), reverse=True)[:limit]

    def create_attempt(self, prompt: str, solution: str, source: str, knowledge_id: Optional[int] = None,
                       status: str = "proposed") -> Attempt:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO attempts(prompt, solution, source, knowledge_id, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (redact_secrets(prompt.strip()), redact_secrets(solution.strip()), source, knowledge_id, status, datetime.now(timezone.utc).isoformat()),
            )
            identifier = int(cursor.lastrowid)
        return Attempt(identifier, redact_secrets(prompt.strip()), redact_secrets(solution.strip()), source, knowledge_id, status)

    def get_attempt(self, identifier: int) -> Attempt:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM attempts WHERE id = ?", (identifier,)).fetchone()
        if not row:
            raise ValueError(f"Tentativa #{identifier} não encontrada.")
        return Attempt(**{key: row[key] for key in Attempt.__dataclass_fields__})

    def record_test_result(self, identifier: int, passed: bool, output: str, test_command: str) -> TestResult:
        attempt = self.get_attempt(identifier)
        status = "verified" if passed else "failed"
        approval_status = "not_required"
        if passed and attempt.knowledge_id is None:
            status = "awaiting_approval"
            approval_status = "pending"
        with self._connect() as connection:
            connection.execute(
                """UPDATE attempts SET status = ?, approval_status = ?, completed_at = ?, test_output = ?, test_command = ?
                   WHERE id = ?""",
                (status, approval_status, datetime.now(timezone.utc).isoformat(), output[-6000:], test_command, identifier),
            )
            if passed and attempt.knowledge_id is not None:
                connection.execute("UPDATE knowledge SET success_count = success_count + 1 WHERE id = ?", (attempt.knowledge_id,))
        refreshed = self.get_attempt(identifier)
        return TestResult(refreshed, passed, self.learning_summary(identifier))

    def approve_attempt(self, identifier: int) -> int:
        attempt = self.get_attempt(identifier)
        if attempt.status != "awaiting_approval" or attempt.approval_status != "pending":
            raise ValueError(f"Tentativa #{identifier} não está aguardando aprovação.")
        with self._connect() as connection:
            row = connection.execute("SELECT test_command FROM attempts WHERE id = ?", (identifier,)).fetchone()
        knowledge_id = self.add_knowledge(
            title=_title_for(attempt.prompt), problem=attempt.prompt, solution=attempt.solution,
            test_command=row["test_command"], tags="aprendido",
        )
        with self._connect() as connection:
            connection.execute(
                "UPDATE attempts SET status = 'learned', approval_status = 'approved', knowledge_id = ? WHERE id = ?",
                (knowledge_id, identifier),
            )
        return knowledge_id

    def approve_github_discovery(self, identifier: int) -> int:
        """Promove uma descoberta de leitura do GitHub somente após revisão humana."""
        attempt = self.get_attempt(identifier)
        if attempt.source != "github" or attempt.status != "completed":
            raise ValueError(f"Descoberta #{identifier} não está disponível para aprendizado.")
        evidence = "revisão humana: descoberta GitHub somente-leitura"
        knowledge_id = self.add_knowledge(
            title=_title_for(attempt.prompt), problem=attempt.prompt, solution=attempt.solution,
            test_command=evidence, tags="github, leitura-validada",
        )
        with self._connect() as connection:
            connection.execute(
                """UPDATE attempts SET status = 'learned', approval_status = 'approved', knowledge_id = ?,
                   completed_at = ?, test_output = ?, test_command = ? WHERE id = ?""",
                (knowledge_id, datetime.now(timezone.utc).isoformat(), evidence, evidence, identifier),
            )
        return knowledge_id

    def reject_attempt(self, identifier: int) -> None:
        attempt = self.get_attempt(identifier)
        if attempt.status != "awaiting_approval":
            raise ValueError(f"Tentativa #{identifier} não está aguardando aprovação.")
        with self._connect() as connection:
            connection.execute(
                "UPDATE attempts SET status = 'verified_not_learned', approval_status = 'rejected' WHERE id = ?", (identifier,)
            )

    def learning_summary(self, identifier: int) -> str:
        attempt = self.get_attempt(identifier)
        return (
            f"Tarefa: {attempt.prompt}\n"
            f"Origem: {attempt.source}\n"
            f"Solução validada: {attempt.solution[:800]}\n"
            f"Estado: {attempt.status}"
        )

    def history(self, limit: int = 20) -> list[Attempt]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM attempts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Attempt(**{key: row[key] for key in Attempt.__dataclass_fields__}) for row in rows]

    def has_failed_attempt(self, prompt: str) -> bool:
        """Uma repetição de falha deve subir de camada, não gastar na mesma rota."""
        normalized = " ".join(prompt.lower().split())
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT prompt FROM attempts WHERE status = 'failed' ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return any(" ".join(row["prompt"].lower().split()) == normalized for row in rows)

    def all_knowledge(self, limit: int = 20) -> list[KnowledgeItem]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM knowledge ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [KnowledgeItem(**dict(row)) for row in rows]

    def maturity_stats(self) -> dict[str, int | bool]:
        """Indicadores simples, explicáveis e baseados somente em tentativas reais."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT source, status FROM attempts WHERE source IN ('knowledge', 'routine', 'codex', 'github')"
            ).fetchall()
            knowledge_count = int(connection.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0])
        local = sum(row["source"] == "knowledge" and row["status"] == "verified" for row in rows)
        remote = sum(row["source"] in {"routine", "codex"} for row in rows)
        verified = sum(row["status"] in {"verified", "learned", "verified_not_learned"} for row in rows)
        total = local + remote
        local_rate = round((local / total) * 100) if total else 0
        return {
            "knowledge_count": knowledge_count,
            "local_solutions": local,
            "external_consultations": remote,
            "verified_attempts": verified,
            "local_rate": local_rate,
            "ready_to_review_routine": knowledge_count >= 10 and total >= 20 and local_rate >= 70,
        }


def _terms(text: str) -> set[str]:
    return {"".join(character for character in word.lower() if character.isalnum()) for word in text.split()
            if len("".join(character for character in word if character.isalnum())) >= 3}


def _title_for(prompt: str) -> str:
    clean = " ".join(prompt.split())
    return clean[:80] + ("…" if len(clean) > 80 else "")
