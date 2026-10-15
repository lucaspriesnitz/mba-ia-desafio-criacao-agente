# Regra é regra: o assistente virtual do Residencial Aurora

Assistente dos moradores do Residencial Aurora, construído com Google ADK e exposto por uma API
FastAPI em `http://localhost:8000`. Ele reserva e cancela áreas comuns, autoriza visitantes e
responde dúvidas do regulamento. O modelo conduz a conversa; as regras críticas estão no código.

## Arquitetura

```
POST /sessoes/{id}/mensagens ──► Runner (App residencial_aurora, sessão em SQLite)
                                   │
                                   ▼
                          assistente_aurora (principal)
                 ┌─────────────────┼──────────────────────┐
         sub_agent (transferência) │ sub_agent             │ AgentTool
                 ▼                 ▼                       ▼
      especialista_reservas  especialista_visitantes  especialista_regulamento
                 │                 │                       │
                 └──── tools ──────┴──► aurora.db           └──► dados/regulamento.md (por capítulo)
```

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `assistente_aurora` | Recebe o morador e encaminha o pedido | Raiz do `App` | Não tem tools de dados nem o regulamento nas instruções: só roteia. |
| `especialista_reservas` | Lista áreas, verifica disponibilidade, reserva, cancela e lista reservas do apartamento | `sub_agent` (transferência) | Reservar pode exigir confirmação; a pausa e a retomada acontecem dentro dele, com as tools dele. |
| `especialista_visitantes` | Autoriza e lista visitantes do apartamento | `sub_agent` (transferência) | Mesmo motivo: autorizar visitante sempre pede confirmação. |
| `especialista_regulamento` | Responde dúvidas lendo um capítulo por vez | `AgentTool` do principal | Roda numa sessão própria: o capítulo lido fica lá, e só a resposta volta para a conversa principal. |

Código: [`src/residencial_aurora/agentes.py`](src/residencial_aurora/agentes.py) (agentes e tools),
[`api.py`](src/residencial_aurora/api.py) (rotas, Runner, confirmações),
[`db.py`](src/residencial_aurora/db.py) (dados do condomínio),
[`regulamento.py`](src/residencial_aurora/regulamento.py) (acesso por capítulo).

Armazenamento: dois arquivos SQLite locais, sem serviço externo. `aurora.db` guarda reservas e
visitantes, carregados de `dados/` (que nunca é escrito); `sessoes.db` guarda sessões e eventos
pelo `SqliteSessionService` do ADK.

## Garantias

### 1. Cobrança ou acesso só com confirmação

- [`agentes.py`](src/residencial_aurora/agentes.py): as tools que cobram ou liberam acesso são
  `FunctionTool` com confirmação exigida pelo ADK.
  ```python
  FunctionTool(reservar_area, require_confirmation=_reserva_tem_taxa),
  FunctionTool(autorizar_visitante, require_confirmation=True)
  ```
  `_reserva_tem_taxa` lê a taxa em `dados/areas.json`: área com taxa zero (a quadra) reserva direto.
- [`api.py`](src/residencial_aurora/api.py), `_pendentes`: as pendências são lidas dos eventos
  persistidos (`adk_request_confirmation` sem resposta). `responder_confirmacao` só aceita um `id`
  dessa lista; qualquer outro, inclusive um já respondido, recebe `409` sem executar nada. A
  resposta volta ao Runner como `FunctionResponse` com o `invocation_id` da pendência, e o
  `App` usa `ResumabilityConfig(is_resumable=True)`, então a execução retoma no agente que pediu.

Não depende do modelo: o ADK pausa antes de chamar a função, e só a rota de confirmações produz a
resposta que a destrava. Frase do morador ("já estou confirmando") não é `FunctionResponse`.

### 2. Cada sessão pertence a um apartamento

- [`api.py`](src/residencial_aurora/api.py), `criar_sessao`: o apartamento entra no state uma única
  vez, `state={"apartamento": corpo.apartamento}`.
- [`agentes.py`](src/residencial_aurora/agentes.py), `_apartamento`: todas as tools leem
  `tool_context.state["apartamento"]`. Nenhuma tool tem parâmetro de apartamento.
- [`db.py`](src/residencial_aurora/db.py): `area_livre` devolve só se a data está livre, nunca de
  quem é a reserva; `cancelar_reserva` filtra `WHERE codigo = ? AND apartamento = ?`.

Não depende do modelo: o modelo não tem como passar outro apartamento, porque nenhuma tool aceita
um, e as consultas só devolvem dados do apartamento da sessão.

### 3. Nada se perde no reinício

- [`api.py`](src/residencial_aurora/api.py): `SqliteSessionService(SESSOES_DB)` grava sessões e
  eventos em `sessoes.db`.
- [`db.py`](src/residencial_aurora/db.py): reservas e visitantes ficam em `aurora.db`. Reserva
  cancelada não é apagada (`UPDATE reservas SET ativa = 0`), então o código continua ocupando a
  chave primária; `criar_reserva` sorteia outro código se o sorteado já existir.

Não depende do modelo: tudo está em disco, e a unicidade do código é a `PRIMARY KEY` da tabela.

### 4. O regulamento é consultado, não carregado

- [`regulamento.py`](src/residencial_aurora/regulamento.py): as tools devolvem só o índice ou um
  capítulo (`indice`, `capitulo(numero)`).
- [`agentes.py`](src/residencial_aurora/agentes.py): `tools=[AgentTool(especialista_regulamento)]`
  no principal, cujas instruções não contêm o regulamento. O especialista roda em sessão própria;
  o capítulo lido não entra nos eventos da conversa principal.

Não depende do modelo: o texto do regulamento só existe dentro das tools do especialista, e o
`AgentTool` devolve à sessão principal só a resposta final.

### 5. Dois moradores, uma reserva

- [`db.py`](src/residencial_aurora/db.py), `SCHEMA`:
  ```sql
  CREATE UNIQUE INDEX IF NOT EXISTS reserva_unica_por_area_e_data
      ON reservas (area, data) WHERE ativa = 1;
  ```
  `criar_reserva` grava direto; se o índice recusar, devolve `None` e a tool responde
  "já está reservado(a)", sem erro de servidor.

Não depende do modelo nem de conferência prévia: o SQLite garante a exclusividade no instante do
`INSERT`. Com duas aprovações simultâneas, as duas respondem `200` e só uma reserva existe.

## Como rodar

Pré-requisitos: Python 3.12+, [uv](https://docs.astral.sh/uv/) e uma chave do
[Google AI Studio](https://aistudio.google.com/apikey).

```sh
cp .env.example .env    # preencha GOOGLE_API_KEY
uv sync
uv run restaurar        # volta reservas e visitantes ao estado de dados/ (não apaga sessões)
uv run api              # sobe a API em http://localhost:8000
```

Variáveis do `.env`:

| Variável | Uso |
|---|---|
| `GOOGLE_API_KEY` | Chave do Google AI Studio (obrigatória). |
| `GOOGLE_GENAI_USE_VERTEXAI` | Vazio ou `FALSE`: usa a Gemini API do AI Studio. |
| `GEMINI_MODEL_PRINCIPAL` | Modelo do agente principal. Padrão `gemini-3.1-flash-lite`. |
| `GEMINI_MODEL_RESERVAS` | Padrão `gemini-3.1-flash-lite-preview`. |
| `GEMINI_MODEL_VISITANTES` | Padrão `gemini-3-flash-preview`. |
| `GEMINI_MODEL_REGULAMENTO` | Padrão `gemini-3.5-flash-lite`. |

Cada agente usa um modelo diferente porque a cota gratuita da Gemini API é contada por modelo:
assim o fluxo completo cabe nela. Se algum modelo estiver indisponível ou sem cota no seu projeto,
troque-o no `.env`.

Para apagar também as sessões, pare a API e remova `sessoes.db`.
