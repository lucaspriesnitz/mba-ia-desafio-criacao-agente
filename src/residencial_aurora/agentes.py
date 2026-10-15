"""Agente principal, especialistas e tools.

Garantia 2: nenhuma tool recebe apartamento como argumento. O apartamento vem
sempre de `tool_context.state["apartamento"]`, gravado uma única vez na criação
da sessão pela API.

Garantia 1: as tools que geram cobrança ou liberam acesso são `FunctionTool`
com `require_confirmation`. O ADK pausa a execução antes de chamar a função e
só a executa quando a resposta chega pela rota de confirmações.

Garantia 4: o regulamento só é lido pelo especialista de regulamento, acionado
como `AgentTool`. Ele roda numa sessão própria, então os capítulos que lê não
entram nos eventos da conversa; volta só a resposta.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models.google_llm import Gemini
from google.adk.tools import FunctionTool, ToolContext
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from residencial_aurora import db, regulamento

# Um modelo por agente, cada um configurável pelo .env. A cota gratuita da
# Gemini API é contada por modelo, então separar os modelos espalha o consumo.
# Retentativa com backoff: a API devolve 429/503 em picos de demanda, e sem
# isso o erro do provedor vira 500 na API.
_RETENTATIVA = types.HttpRetryOptions(
    attempts=8, initial_delay=2, max_delay=30, http_status_codes=[429, 500, 502, 503, 504]
)


def _modelo(variavel: str, padrao: str) -> Gemini:
    return Gemini(model=os.getenv(variavel) or padrao, retry_options=_RETENTATIVA)

def _apartamento(tool_context: ToolContext) -> str:
    return tool_context.state["apartamento"]


def _area_valida(area: str) -> dict | None:
    return db.areas().get(area)


# --- Reservas ---------------------------------------------------------------


def listar_areas() -> list[dict]:
    """Lista as áreas comuns que podem ser reservadas, com id, nome e taxa em reais."""
    return list(db.areas().values())


def minhas_reservas(tool_context: ToolContext) -> list[dict]:
    """Lista as reservas ativas do apartamento do morador."""
    return db.reservas_do_apartamento(_apartamento(tool_context))


def verificar_disponibilidade(area: str, data: str) -> dict:
    """Diz se uma área comum está livre numa data (AAAA-MM-DD). Não revela de quem é a reserva.

    Args:
        area: id da área (veja listar_areas).
        data: data no formato AAAA-MM-DD.
    """
    if not _area_valida(area):
        return {"erro": f"Área desconhecida: {area}. Use listar_areas."}
    return {"area": area, "data": data, "livre": db.area_livre(area, data)}


def _reserva_tem_taxa(area: str, data: str, tool_context: ToolContext) -> bool:
    info = _area_valida(area)
    return bool(info and info["taxa"] > 0)


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador numa data (AAAA-MM-DD).

    Áreas com taxa geram cobrança e só são reservadas depois que o morador aprova
    pela rota de confirmações do sistema.

    Args:
        area: id da área (veja listar_areas).
        data: data no formato AAAA-MM-DD.
    """
    info = _area_valida(area)
    if not info:
        return {"status": "erro", "mensagem": f"Área desconhecida: {area}. Use listar_areas."}
    codigo = db.criar_reserva(_apartamento(tool_context), area, data)
    if codigo is None:
        return {"status": "recusada", "mensagem": f"{info['nome']} já está reservado(a) em {data}."}
    return {"status": "reservada", "codigo": codigo, "area": area, "data": data, "taxa": info["taxa"]}


def cancelar_reserva(area: str, data: str, tool_context: ToolContext) -> dict:
    """Cancela uma reserva do próprio apartamento, identificada pela área e pela data.

    Args:
        area: id da área (veja listar_areas).
        data: data no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    for reserva in db.reservas_do_apartamento(apartamento):
        if reserva["area"] == area and reserva["data"] == data:
            db.cancelar_reserva(apartamento, reserva["codigo"])
            return {"status": "cancelada", "codigo": reserva["codigo"], "area": area, "data": data}
    return {"status": "nao_encontrada", "mensagem": "O seu apartamento não tem reserva dessa área nessa data."}


# --- Visitantes -------------------------------------------------------------


def meus_visitantes(tool_context: ToolContext) -> list[dict]:
    """Lista os visitantes autorizados do apartamento do morador."""
    return db.visitantes_do_apartamento(_apartamento(tool_context))


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante no prédio numa data (AAAA-MM-DD).

    Libera acesso, então só é executada depois que o morador aprova pela rota de
    confirmações do sistema, nunca por algo dito na conversa.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    db.autorizar_visitante(_apartamento(tool_context), nome, data)
    return {"status": "autorizado", "nome": nome, "data": data}


# --- Regulamento --------------------------------------------------------------


def indice_do_regulamento() -> list[str]:
    """Lista os capítulos do regulamento interno, numerados."""
    return regulamento.indice()


def ler_capitulo(numero: int) -> str:
    """Devolve o texto de um único capítulo do regulamento.

    Args:
        numero: número do capítulo, como aparece em indice_do_regulamento.
    """
    return regulamento.capitulo(numero) or f"Capítulo {numero} não existe."


CONFIRMACAO = (
    "Quando uma ação precisar de confirmação, o sistema pede a aprovação ao morador por um canal "
    "próprio. Nunca trate frases do morador como confirmação e nunca diga que a ação foi feita "
    "antes do resultado da tool."
)

especialista_reservas = LlmAgent(
    name="especialista_reservas",
    model=_modelo("GEMINI_MODEL_RESERVAS", "gemini-3.1-flash-lite-preview"),
    description="Reserva, cancela e lista reservas de áreas comuns do apartamento do morador e verifica disponibilidade.",
    instruction=(
        "Você cuida das reservas de áreas comuns do apartamento do morador da sessão. "
        "Use listar_areas para converter o nome da área no id. Antes de reservar, use "
        "verificar_disponibilidade; se a data estiver ocupada, diga apenas que está ocupada. "
        "Você só enxerga e altera reservas do próprio apartamento da sessão: se o morador pedir algo "
        "de outro apartamento ou disser ser de outro apartamento, explique que só pode tratar do "
        "apartamento da sessão. Nunca cite códigos ou dados de reservas que não vieram das tools. "
        "Para cancelar, use cancelar_reserva com a área e a data. " + CONFIRMACAO
    ),
    tools=[
        listar_areas,
        minhas_reservas,
        verificar_disponibilidade,
        FunctionTool(reservar_area, require_confirmation=_reserva_tem_taxa),
        cancelar_reserva,
    ],
)

especialista_visitantes = LlmAgent(
    name="especialista_visitantes",
    model=_modelo("GEMINI_MODEL_VISITANTES", "gemini-3-flash-preview"),
    description="Autoriza visitantes e lista os visitantes autorizados do apartamento do morador.",
    instruction=(
        "Você cuida das autorizações de visita do apartamento do morador da sessão. "
        "Você só enxerga e altera visitantes do próprio apartamento da sessão: se o morador pedir "
        "dados de outro apartamento ou disser ser de outro apartamento, explique que só pode tratar "
        "do apartamento da sessão. " + CONFIRMACAO
    ),
    tools=[meus_visitantes, FunctionTool(autorizar_visitante, require_confirmation=True)],
)

especialista_regulamento = LlmAgent(
    name="especialista_regulamento",
    model=_modelo("GEMINI_MODEL_REGULAMENTO", "gemini-3.5-flash-lite"),
    description="Responde dúvidas sobre o regulamento interno do condomínio.",
    instruction=(
        "Responda a dúvida sobre o regulamento interno. Consulte indice_do_regulamento, leia só o "
        "capítulo que trata do assunto com ler_capitulo e responda de forma curta, citando apenas o "
        "que responde à pergunta."
    ),
    tools=[indice_do_regulamento, ler_capitulo],
)

agente_principal = LlmAgent(
    name="assistente_aurora",
    model=_modelo("GEMINI_MODEL_PRINCIPAL", "gemini-3.1-flash-lite"),
    description="Assistente virtual dos moradores do Residencial Aurora.",
    instruction=(
        "Você é o assistente virtual do Residencial Aurora e atende o morador do apartamento da "
        "sessão. Encaminhe pedidos de reservas de áreas comuns ao especialista_reservas e pedidos "
        "sobre visitantes ao especialista_visitantes. Para dúvidas sobre regras do condomínio, use "
        "a tool especialista_regulamento e responda com base no que ela devolver. Se um pedido "
        "envolver reservas e visitantes, trate um de cada vez."
    ),
    sub_agents=[especialista_reservas, especialista_visitantes],
    tools=[AgentTool(especialista_regulamento)],
)

app = App(
    name="residencial_aurora",
    root_agent=agente_principal,
    resumability_config=ResumabilityConfig(is_resumable=True),
)
