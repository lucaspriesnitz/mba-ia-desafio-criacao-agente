"""Dados do condomínio em SQLite.

`dados/` é só o estado inicial: nunca é escrito. Reservas canceladas ficam na
tabela com `ativa = 0`, então o código delas continua ocupando a chave primária
e nunca é reutilizado. A exclusividade de área por data é um índice único
parcial sobre as reservas ativas: vale no instante da gravação, não numa
conferência feita antes.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
DADOS = RAIZ / "dados"
DB_PATH = Path(os.getenv("AURORA_DB", RAIZ / "aurora.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservas (
    codigo      TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    area        TEXT NOT NULL,
    data        TEXT NOT NULL,
    ativa       INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS reserva_unica_por_area_e_data
    ON reservas (area, data) WHERE ativa = 1;
CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL
);
"""


@contextmanager
def conexao():
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        yield con
    finally:
        con.close()


def _ler_json(nome: str) -> list[dict]:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


def areas() -> dict[str, dict]:
    return {a["id"]: a for a in _ler_json("areas.json")}


def apartamentos() -> set[str]:
    return {a["numero"] for a in _ler_json("apartamentos.json")}


def criar_schema() -> None:
    with conexao() as con:
        con.executescript(SCHEMA)


def restaurar() -> None:
    """Volta reservas e visitantes ao estado de `dados/`. Não toca nas sessões."""
    criar_schema()
    with conexao() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM reservas")
        con.execute("DELETE FROM visitantes")
        con.executemany(
            "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
            [(r["codigo"], r["apartamento"], r["area"], r["data"]) for r in _ler_json("reservas.json")],
        )
        con.executemany(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            [(v["apartamento"], v["nome"], v["data"]) for v in _ler_json("visitantes.json")],
        )
        con.execute("COMMIT")


def reservas_do_apartamento(apartamento: str) -> list[dict]:
    with conexao() as con:
        linhas = con.execute(
            "SELECT codigo, area, data FROM reservas WHERE apartamento = ? AND ativa = 1 ORDER BY data, codigo",
            (apartamento,),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def visitantes_do_apartamento(apartamento: str) -> list[dict]:
    with conexao() as con:
        linhas = con.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id",
            (apartamento,),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def area_livre(area: str, data: str) -> bool:
    """Só diz se a data está livre; nunca de quem é a reserva."""
    with conexao() as con:
        linha = con.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND ativa = 1", (area, data)
        ).fetchone()
    return linha is None


def criar_reserva(apartamento: str, area: str, data: str) -> str | None:
    """Grava a reserva e devolve o código, ou None se a área já está ocupada na data."""
    while True:
        codigo = f"RSV-{secrets.token_hex(3).upper()}"
        try:
            with conexao() as con:
                con.execute(
                    "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
                    (codigo, apartamento, area, data),
                )
            return codigo
        except sqlite3.IntegrityError as erro:
            if "reservas.codigo" in str(erro):
                continue  # código já usado, inclusive por reserva cancelada: sorteia outro
            return None


def cancelar_reserva(apartamento: str, codigo: str) -> bool:
    """Cancela só se a reserva for do apartamento informado."""
    with conexao() as con:
        cursor = con.execute(
            "UPDATE reservas SET ativa = 0 WHERE codigo = ? AND apartamento = ? AND ativa = 1",
            (codigo, apartamento),
        )
    return cursor.rowcount == 1


def autorizar_visitante(apartamento: str, nome: str, data: str) -> None:
    with conexao() as con:
        con.execute(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            (apartamento, nome, data),
        )
