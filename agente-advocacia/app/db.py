"""Banco SQLite: clientes, conversas, triagens, consultas e lembretes."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clientes (
    telefone TEXT PRIMARY KEY,
    nome TEXT,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversas (
    telefone TEXT PRIMARY KEY,
    mensagens TEXT NOT NULL,          -- histórico no formato da API do Claude (JSON)
    atualizado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS triagens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telefone TEXT NOT NULL,
    nome TEXT,
    area TEXT,
    resumo TEXT,
    urgente INTEGER NOT NULL DEFAULT 0,
    motivo_urgencia TEXT,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consultas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telefone TEXT NOT NULL,
    nome TEXT NOT NULL,
    inicio TEXT NOT NULL,             -- ISO 8601 com fuso
    fim TEXT NOT NULL,
    area TEXT,
    resumo TEXT,
    modalidade TEXT,
    status TEXT NOT NULL DEFAULT 'confirmada',   -- confirmada | cancelada
    google_event_id TEXT,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bloqueios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inicio TEXT NOT NULL,
    fim TEXT NOT NULL,
    motivo TEXT
);
CREATE TABLE IF NOT EXISTS lembretes_enviados (
    consulta_id INTEGER NOT NULL,
    horas_antes INTEGER NOT NULL,
    enviado_em TEXT NOT NULL,
    PRIMARY KEY (consulta_id, horas_antes)
);
CREATE TABLE IF NOT EXISTS pausas (
    telefone TEXT PRIMARY KEY,
    ate TEXT NOT NULL                 -- agente não responde este cliente até esse momento
);
CREATE TABLE IF NOT EXISTS mensagens_processadas (
    id TEXT PRIMARY KEY,
    recebido_em TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ----------------------------------------------------------------- util
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def agora() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    # ------------------------------------------------------------ dedupe
    def ja_processada(self, msg_id: str) -> bool:
        """Marca a mensagem como processada; devolve True se já tinha sido."""
        try:
            self._exec("INSERT INTO mensagens_processadas (id, recebido_em) VALUES (?, ?)", (msg_id, self.agora()))
            return False
        except sqlite3.IntegrityError:
            return True

    # ----------------------------------------------------------- clientes
    def cliente(self, telefone: str) -> dict | None:
        return self._one("SELECT * FROM clientes WHERE telefone = ?", (telefone,))

    def salvar_cliente(self, telefone: str, nome: str | None) -> None:
        atual = self.cliente(telefone)
        if atual is None:
            self._exec("INSERT INTO clientes (telefone, nome, criado_em) VALUES (?, ?, ?)", (telefone, nome, self.agora()))
        elif nome and nome != atual.get("nome"):
            self._exec("UPDATE clientes SET nome = ? WHERE telefone = ?", (nome, telefone))

    # ---------------------------------------------------------- conversas
    def conversa(self, telefone: str) -> tuple[list, str | None]:
        row = self._one("SELECT mensagens, atualizado_em FROM conversas WHERE telefone = ?", (telefone,))
        if not row:
            return [], None
        return json.loads(row["mensagens"]), row["atualizado_em"]

    def salvar_conversa(self, telefone: str, mensagens: list) -> None:
        self._exec(
            "INSERT INTO conversas (telefone, mensagens, atualizado_em) VALUES (?, ?, ?) "
            "ON CONFLICT(telefone) DO UPDATE SET mensagens = excluded.mensagens, atualizado_em = excluded.atualizado_em",
            (telefone, json.dumps(mensagens, ensure_ascii=False), self.agora()),
        )

    def apagar_conversa(self, telefone: str) -> None:
        self._exec("DELETE FROM conversas WHERE telefone = ?", (telefone,))

    # ----------------------------------------------------------- triagens
    def salvar_triagem(self, telefone: str, nome: str | None, area: str, resumo: str,
                       urgente: bool, motivo_urgencia: str | None) -> int:
        cur = self._exec(
            "INSERT INTO triagens (telefone, nome, area, resumo, urgente, motivo_urgencia, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (telefone, nome, area, resumo, int(urgente), motivo_urgencia, self.agora()),
        )
        return int(cur.lastrowid)

    def triagens(self, limite: int = 100) -> list[dict]:
        return self._all("SELECT * FROM triagens ORDER BY id DESC LIMIT ?", (limite,))

    # ---------------------------------------------------------- consultas
    def criar_consulta(self, telefone: str, nome: str, inicio: datetime, fim: datetime, area: str,
                       resumo: str, modalidade: str, google_event_id: str | None = None) -> int:
        cur = self._exec(
            "INSERT INTO consultas (telefone, nome, inicio, fim, area, resumo, modalidade, google_event_id, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (telefone, nome, inicio.isoformat(), fim.isoformat(), area, resumo, modalidade, google_event_id, self.agora()),
        )
        return int(cur.lastrowid)

    def consulta(self, consulta_id: int) -> dict | None:
        return self._one("SELECT * FROM consultas WHERE id = ?", (consulta_id,))

    def cancelar_consulta(self, consulta_id: int) -> None:
        self._exec("UPDATE consultas SET status = 'cancelada' WHERE id = ?", (consulta_id,))

    def consultas_futuras(self, telefone: str | None = None) -> list[dict]:
        agora = datetime.now().astimezone().isoformat()
        if telefone:
            return self._all(
                "SELECT * FROM consultas WHERE status = 'confirmada' AND fim > ? AND telefone = ? ORDER BY inicio",
                (agora, telefone),
            )
        return self._all("SELECT * FROM consultas WHERE status = 'confirmada' AND fim > ? ORDER BY inicio", (agora,))

    def consultas_entre(self, inicio: datetime, fim: datetime) -> list[dict]:
        # Compara como datetime em Python (as strings podem ter fusos diferentes).
        rows = self._all("SELECT * FROM consultas WHERE status = 'confirmada'")
        out = []
        for r in rows:
            ri, rf = datetime.fromisoformat(r["inicio"]), datetime.fromisoformat(r["fim"])
            if ri < fim and rf > inicio:
                out.append(r)
        return out

    def todas_consultas(self, limite: int = 200) -> list[dict]:
        return self._all("SELECT * FROM consultas ORDER BY inicio DESC LIMIT ?", (limite,))

    # ---------------------------------------------------------- bloqueios
    def bloqueios_entre(self, inicio: datetime, fim: datetime) -> list[dict]:
        rows = self._all("SELECT * FROM bloqueios")
        return [r for r in rows
                if datetime.fromisoformat(r["inicio"]) < fim and datetime.fromisoformat(r["fim"]) > inicio]

    def criar_bloqueio(self, inicio: datetime, fim: datetime, motivo: str = "") -> int:
        cur = self._exec("INSERT INTO bloqueios (inicio, fim, motivo) VALUES (?, ?, ?)",
                         (inicio.isoformat(), fim.isoformat(), motivo))
        return int(cur.lastrowid)

    # ------------------------------------------------------------- pausas
    def pausar(self, telefone: str, ate: datetime) -> None:
        self._exec("INSERT INTO pausas (telefone, ate) VALUES (?, ?) "
                   "ON CONFLICT(telefone) DO UPDATE SET ate = excluded.ate", (telefone, ate.isoformat()))

    def retomar(self, telefone: str) -> None:
        self._exec("DELETE FROM pausas WHERE telefone = ?", (telefone,))

    def pausado(self, telefone: str, agora: datetime | None = None) -> bool:
        row = self._one("SELECT ate FROM pausas WHERE telefone = ?", (telefone,))
        return bool(row) and datetime.fromisoformat(row["ate"]) > (agora or datetime.now().astimezone())

    # ---------------------------------------------------------- lembretes
    def lembrete_enviado(self, consulta_id: int, horas_antes: int) -> bool:
        return self._one("SELECT 1 AS x FROM lembretes_enviados WHERE consulta_id = ? AND horas_antes = ?",
                         (consulta_id, horas_antes)) is not None

    def marcar_lembrete(self, consulta_id: int, horas_antes: int) -> None:
        self._exec("INSERT OR IGNORE INTO lembretes_enviados (consulta_id, horas_antes, enviado_em) VALUES (?, ?, ?)",
                   (consulta_id, horas_antes, self.agora()))
