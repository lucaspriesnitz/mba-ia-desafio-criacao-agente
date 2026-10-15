"""API HTTP do assistente, no contrato do enunciado.

Garantia 1: as confirmações pendentes são lidas dos eventos persistidos da
sessão (chamadas `adk_request_confirmation` ainda sem resposta). A rota de
confirmações só aceita um `id` dessa lista; qualquer outro, inclusive um já
respondido, recebe 409 sem executar nada.

Garantia 3: sessões e eventos ficam no `SqliteSessionService`, num arquivo em
disco; reiniciar a API não perde nada.
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions.sqlite_session_service import SqliteSessionService  # noqa: E402
from google.genai import types  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from residencial_aurora import db  # noqa: E402
from residencial_aurora.agentes import app as adk_app  # noqa: E402

CONFIRMACAO = "adk_request_confirmation"
USUARIO = "morador"
SESSOES_DB = os.getenv("AURORA_SESSOES_DB", str(db.RAIZ / "sessoes.db"))

sessoes = SqliteSessionService(SESSOES_DB)
runner = Runner(app=adk_app, session_service=sessoes)
travas: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@asynccontextmanager
async def ciclo_de_vida(_: FastAPI):
    db.criar_schema()
    yield


api = FastAPI(title="Residencial Aurora", lifespan=ciclo_de_vida)


class NovaSessao(BaseModel):
    apartamento: str


class Mensagem(BaseModel):
    texto: str


class Resposta(BaseModel):
    id: str
    confirmado: bool


async def _sessao(session_id: str):
    sessao = await sessoes.get_session(app_name=adk_app.name, user_id=USUARIO, session_id=session_id)
    if sessao is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")
    return sessao


def _pendentes(sessao) -> list[dict]:
    """Confirmações pedidas pelo ADK e ainda não respondidas, na ordem em que surgiram."""
    respondidas = {
        resposta.id
        for evento in sessao.events
        for resposta in evento.get_function_responses()
        if resposta.name == CONFIRMACAO
    }
    pendentes = []
    for evento in sessao.events:
        for chamada in evento.get_function_calls():
            if chamada.name != CONFIRMACAO or chamada.id in respondidas:
                continue
            original = (chamada.args or {}).get("originalFunctionCall", {})
            pendentes.append(
                {
                    "id": chamada.id,
                    "acao": original.get("name", ""),
                    "detalhes": original.get("args", {}),
                    "invocation_id": evento.invocation_id,
                }
            )
    return pendentes


async def _executar(session_id: str, mensagem: types.Content, invocation_id: str | None = None) -> dict:
    textos = []
    async for evento in runner.run_async(
        user_id=USUARIO, session_id=session_id, new_message=mensagem, invocation_id=invocation_id
    ):
        if evento.partial or evento.author == "user" or not evento.content:
            continue
        for parte in evento.content.parts or []:
            if parte.text and not parte.thought:
                textos.append(parte.text.strip())
    sessao = await _sessao(session_id)
    return {
        "resposta": "\n\n".join(t for t in textos if t),
        "confirmacoes_pendentes": [
            {"id": p["id"], "acao": p["acao"], "detalhes": p["detalhes"]} for p in _pendentes(sessao)
        ],
    }


@api.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao) -> dict:
    # O apartamento entra no state uma única vez, aqui; nenhuma tool o escreve.
    sessao = await sessoes.create_session(
        app_name=adk_app.name, user_id=USUARIO, state={"apartamento": corpo.apartamento}
    )
    return {"session_id": sessao.id}


@api.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: Mensagem) -> dict:
    await _sessao(session_id)
    async with travas[session_id]:
        conteudo = types.Content(role="user", parts=[types.Part(text=corpo.texto)])
        return await _executar(session_id, conteudo)


@api.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, corpo: Resposta) -> dict:
    await _sessao(session_id)
    async with travas[session_id]:
        sessao = await _sessao(session_id)
        pendente = next((p for p in _pendentes(sessao) if p["id"] == corpo.id), None)
        if pendente is None:
            raise HTTPException(status_code=409, detail="Não existe confirmação pendente com esse id nesta sessão")
        resposta = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=corpo.id, name=CONFIRMACAO, response={"confirmed": corpo.confirmado}
                    )
                )
            ],
        )
        return await _executar(session_id, resposta, invocation_id=pendente["invocation_id"])


@api.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str) -> list[dict]:
    sessao = await _sessao(session_id)
    return [evento.model_dump(mode="json", by_alias=True, exclude_none=True) for evento in sessao.events]


@api.get("/apartamentos/{apartamento}/reservas")
def reservas(apartamento: str) -> list[dict]:
    return db.reservas_do_apartamento(apartamento)


@api.get("/apartamentos/{apartamento}/visitantes")
def visitantes(apartamento: str) -> list[dict]:
    return db.visitantes_do_apartamento(apartamento)


def main() -> None:
    import uvicorn

    uvicorn.run(api, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
