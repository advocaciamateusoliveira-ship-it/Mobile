"""O agente: conversa com o Claude e executa as ferramentas (triagem, agenda, consultas)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from typing import Awaitable, Callable

import anthropic

from .agenda import Agenda
from .config import Settings
from .db import DB
from .prompts import system_prompt

log = logging.getLogger(__name__)

DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
MAX_PASSOS = 10  # limite de idas e vindas de ferramenta por mensagem recebida

MSG_ERRO = ("Desculpe, tive um probleminha técnico agora. Já avisei o escritório e "
            "o {advogado} vai te responder assim que possível.")


def fmt_data_hora(dt: datetime) -> str:
    """Ex.: 'terça, 11/11 às 9h30'."""
    hora = f"{dt.hour}h" + (f"{dt.minute:02d}" if dt.minute else "")
    return f"{DIAS_SEMANA[dt.weekday()]}, {dt:%d/%m} às {hora}"


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


TOOLS = [
    {
        "name": "consultar_horarios",
        "description": "Lista horários LIVRES reais para consulta com o advogado. Use sempre antes de oferecer horários.",
        "strict": True,
        "input_schema": _schema({
            "a_partir_de": {"type": "string", "description": "Data inicial no formato AAAA-MM-DD."},
            "dias": {"type": "integer", "description": "Quantos dias olhar a partir da data inicial (1 a 14)."},
            "periodo": {"type": "string", "enum": ["manha", "tarde", "qualquer"]},
        }),
    },
    {
        "name": "registrar_triagem",
        "description": "Registra a ficha de triagem do atendimento. Se urgente=true, o advogado é avisado na hora.",
        "strict": True,
        "input_schema": _schema({
            "nome": {"type": "string", "description": "Nome da pessoa (vazio se ainda não souber)."},
            "area": {"type": "string", "description": "Área do direito (uma das áreas atendidas, ou 'Outro')."},
            "resumo": {"type": "string", "description": "Resumo do caso em 1 a 3 frases."},
            "urgente": {"type": "boolean"},
            "motivo_urgencia": {"type": "string", "description": "Por que é urgente (vazio se não for)."},
        }),
    },
    {
        "name": "marcar_consulta",
        "description": "Marca a consulta. Só use depois que a pessoa confirmar o horário e informar o nome completo.",
        "strict": True,
        "input_schema": _schema({
            "nome": {"type": "string", "description": "Nome completo da pessoa."},
            "data_hora": {"type": "string", "description": "Início no formato AAAA-MM-DDTHH:MM (horário do escritório)."},
            "area": {"type": "string"},
            "resumo_caso": {"type": "string"},
            "modalidade": {"type": "string", "description": "Uma das modalidades oferecidas pelo escritório."},
        }),
    },
    {
        "name": "minhas_consultas",
        "description": "Lista as consultas futuras já marcadas por esta pessoa (para confirmar, remarcar ou cancelar).",
        "strict": True,
        "input_schema": _schema({}),
    },
    {
        "name": "cancelar_consulta",
        "description": "Cancela uma consulta desta pessoa. Para remarcar: marque a nova e depois cancele a antiga.",
        "strict": True,
        "input_schema": _schema({"consulta_id": {"type": "integer"}}),
    },
]

Notificar = Callable[[str, str], Awaitable[None]]  # (telefone, texto)


class Agente:
    def __init__(self, settings: Settings, db: DB, agenda: Agenda, notificar: Notificar,
                 client: anthropic.AsyncAnthropic | None = None):
        self.s = settings
        self.db = db
        self.agenda = agenda
        self.notificar = notificar
        self.client = client or anthropic.AsyncAnthropic()
        self.system = system_prompt(settings)
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------- público
    async def responder(self, telefone: str, texto: str, nome_contato: str | None = None,
                        agora: datetime | None = None) -> list[str]:
        """Processa uma mensagem recebida e devolve as mensagens a enviar ao cliente."""
        lock = self._locks.setdefault(telefone, asyncio.Lock())
        async with lock:
            return await self._responder(telefone, texto, nome_contato, agora)

    # ------------------------------------------------------------- interno
    def _cabecalho(self, telefone: str, nome_contato: str | None, nova_sessao: bool, agora: datetime) -> str:
        linhas = [f"[Mensagem recebida em {DIAS_SEMANA[agora.weekday()]}, {agora:%d/%m/%Y %H:%M} "
                  f"(horário do escritório)]"]
        if nova_sessao:
            cli = self.db.cliente(telefone)
            nome = (cli or {}).get("nome") or nome_contato
            if cli and cli.get("nome"):
                linhas.append(f"[Contexto: cliente já atendido antes, nome {cli['nome']}.]")
            elif nome:
                linhas.append(f"[Contexto: nome no perfil do WhatsApp: {nome} (confirme o nome completo).]")
            futuras = self.db.consultas_futuras(telefone)
            if futuras:
                lst = "; ".join(f"#{c['id']} {fmt_data_hora(datetime.fromisoformat(c['inicio']).astimezone(self.s.tz))}"
                                for c in futuras)
                linhas.append(f"[Contexto: consultas já marcadas: {lst}.]")
        return "\n".join(linhas)

    async def _responder(self, telefone: str, texto: str, nome_contato: str | None,
                         agora: datetime | None) -> list[str]:
        agora = (agora or datetime.now(self.s.tz)).astimezone(self.s.tz)
        mensagens, atualizado_em = self.db.conversa(telefone)
        nova_sessao = not mensagens
        if atualizado_em:
            ultima = datetime.fromisoformat(atualizado_em)
            if agora - ultima > timedelta(hours=self.s.nova_sessao_apos_horas):
                mensagens, nova_sessao = [], True  # recomeça do zero (sem editar histórico antigo)

        if nova_sessao:
            self.db.salvar_cliente(telefone, None)

        conteudo = self._cabecalho(telefone, nome_contato, nova_sessao, agora) + "\n" + texto
        mensagens.append({"role": "user", "content": conteudo})

        respostas: list[str] = []
        try:
            for _ in range(MAX_PASSOS):
                resp = await self.client.beta.messages.create(
                    model=self.s.model,
                    max_tokens=16000,
                    system=self.system,
                    tools=TOOLS,
                    messages=mensagens,
                    thinking={"type": "adaptive"},
                    output_config={"effort": "medium"},
                    cache_control={"type": "ephemeral"},
                    # Se o modelo principal recusar, a própria API tenta de novo em outro modelo.
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
                if resp.stop_reason == "refusal" or not resp.content:
                    log.warning("Resposta recusada/vazia (%s) para %s", resp.stop_reason, telefone)
                    await self._avisar_advogado(
                        f"⚠️ O assistente não conseguiu responder +{telefone}. Mensagem do cliente:\n\n{texto}")
                    respostas.append(MSG_ERRO.format(advogado=self.s.escritorio["advogado"]))
                    break

                # Guarda o turno exatamente como veio (inclui blocos de raciocínio).
                mensagens.append({"role": "assistant",
                                  "content": [b.to_dict(mode="json", exclude_none=True) for b in resp.content]})
                respostas += [b.text.strip() for b in resp.content if b.type == "text" and b.text.strip()]

                usos = [b for b in resp.content if b.type == "tool_use"]
                if resp.stop_reason != "tool_use" or not usos:
                    break
                resultados = []
                for u in usos:
                    saida, erro = await self._executar(u.name, u.input, telefone, agora)
                    item = {"type": "tool_result", "tool_use_id": u.id, "content": saida}
                    if erro:
                        item["is_error"] = True
                    resultados.append(item)
                mensagens.append({"role": "user", "content": resultados})
            else:
                log.warning("Limite de passos atingido para %s", telefone)
        except anthropic.APIError:
            log.exception("Erro na API do Claude")
            await self._avisar_advogado(f"⚠️ Erro técnico ao responder +{telefone}. Mensagem do cliente:\n\n{texto}")
            return [MSG_ERRO.format(advogado=self.s.escritorio["advogado"])]

        # Se a conversa terminou num tool_result (limite de passos), não salva um turno pendurado.
        if mensagens and mensagens[-1]["role"] == "user" and isinstance(mensagens[-1]["content"], list):
            mensagens.pop()
            mensagens.pop()
        self.db.salvar_conversa(telefone, mensagens)
        return respostas

    async def _avisar_advogado(self, texto: str) -> None:
        if self.s.whatsapp_advogado:
            try:
                await self.notificar(self.s.whatsapp_advogado, texto)
            except Exception:  # noqa: BLE001
                log.exception("Falha ao avisar o advogado")

    # ----------------------------------------------------------- ferramentas
    async def _executar(self, nome: str, args: dict, telefone: str, agora: datetime) -> tuple[str, bool]:
        try:
            if isinstance(args, str):
                args = json.loads(args)
            fn = getattr(self, f"_tool_{nome}", None)
            if fn is None:
                return f"Ferramenta desconhecida: {nome}", True
            return await fn(args, telefone, agora), False
        except Exception as exc:  # noqa: BLE001 — o modelo recebe o erro e se corrige
            log.exception("Erro na ferramenta %s", nome)
            return f"Erro ao executar {nome}: {exc}", True

    async def _tool_consultar_horarios(self, a: dict, telefone: str, agora: datetime) -> str:
        try:
            ini = date.fromisoformat(a["a_partir_de"])
        except (KeyError, ValueError):
            ini = agora.date()
        dias = max(1, min(int(a.get("dias", 7)), 14))
        periodo = None if a.get("periodo") in (None, "qualquer") else a["periodo"]
        livres = await asyncio.to_thread(self.agenda.horarios_livres, ini, dias, agora, periodo, 12)
        if not livres:
            return "Nenhum horário livre nesse período. Tente outras datas."
        return "Horários livres (use exatamente o valor entre parênteses ao marcar):\n" + "\n".join(
            f"- {fmt_data_hora(h)} ({h:%Y-%m-%dT%H:%M})" for h in livres)

    async def _tool_registrar_triagem(self, a: dict, telefone: str, agora: datetime) -> str:
        nome = (a.get("nome") or "").strip() or None
        if nome:
            self.db.salvar_cliente(telefone, nome)
        urgente = bool(a.get("urgente"))
        self.db.salvar_triagem(telefone, nome, a.get("area", ""), a.get("resumo", ""), urgente,
                               a.get("motivo_urgencia") or None)
        if urgente:
            await self._avisar_advogado(
                f"🚨 *URGENTE* — atendimento pelo WhatsApp\n"
                f"Cliente: {nome or '(sem nome)'} — +{telefone}\n"
                f"Área: {a.get('area', '')}\n"
                f"Motivo: {a.get('motivo_urgencia', '')}\n"
                f"Resumo: {a.get('resumo', '')}\n\n"
                f"Responder: https://wa.me/{telefone}")
            return "Triagem registrada e advogado avisado agora pelo WhatsApp."
        return "Triagem registrada."

    async def _tool_marcar_consulta(self, a: dict, telefone: str, agora: datetime) -> str:
        inicio = datetime.fromisoformat(a["data_hora"])
        if inicio.tzinfo is None:
            inicio = inicio.replace(tzinfo=self.s.tz)
        nome = a["nome"].strip()
        r = await asyncio.to_thread(self.agenda.marcar, telefone, nome, inicio, a.get("area", ""),
                                    a.get("resumo_caso", ""), a.get("modalidade", ""), agora)
        if not r["ok"]:
            return r["erro"]
        self.db.salvar_cliente(telefone, nome)
        quando = fmt_data_hora(r["inicio"])
        await self._avisar_advogado(
            f"📅 *Nova consulta marcada*\n{quando} — {a.get('modalidade', '')}\n"
            f"Cliente: {nome} — +{telefone}\nÁrea: {a.get('area', '')}\nResumo: {a.get('resumo_caso', '')}")
        return f"Consulta #{r['consulta_id']} marcada para {quando} ({a.get('modalidade', '')})."

    async def _tool_minhas_consultas(self, a: dict, telefone: str, agora: datetime) -> str:
        futuras = self.db.consultas_futuras(telefone)
        if not futuras:
            return "Esta pessoa não tem consultas futuras marcadas."
        return "\n".join(
            f"- #{c['id']}: {fmt_data_hora(datetime.fromisoformat(c['inicio']).astimezone(self.s.tz))}, "
            f"{c['modalidade']}, área {c['area']}" for c in futuras)

    async def _tool_cancelar_consulta(self, a: dict, telefone: str, agora: datetime) -> str:
        cid = int(a["consulta_id"])
        c = self.db.consulta(cid)
        if not c or c["telefone"] != telefone:
            return "Consulta não encontrada para esta pessoa."
        if not await asyncio.to_thread(self.agenda.cancelar, cid):
            return "Essa consulta já estava cancelada."
        quando = fmt_data_hora(datetime.fromisoformat(c["inicio"]).astimezone(self.s.tz))
        await self._avisar_advogado(f"❌ Consulta cancelada pelo cliente\n{quando}\nCliente: {c['nome']} — +{telefone}")
        return f"Consulta #{cid} ({quando}) cancelada."
