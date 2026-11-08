"""Servidor: recebe o webhook do WhatsApp, junta mensagens seguidas, chama o agente e responde.

Rodar:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .agenda import Agenda
from .agent import Agente, fmt_data_hora
from .config import load_settings
from .db import DB
from .lembretes import loop_lembretes
from .transcricao import Transcritor
from .whatsapp import Recebida, criar_whatsapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agente")

ESPERA_JUNTAR_S = 6  # espera alguns segundos para juntar mensagens enviadas em sequência
PAUSA_HUMANO_H = 12  # quando alguém do escritório responde um cliente, o agente fica quieto com ele
COMANDO_RETOMAR = "#bot"  # mande isto na conversa do cliente para o agente voltar a atender

settings = load_settings()
db = DB(settings.db_path)
wa = criar_whatsapp(settings)
agenda = Agenda(settings, db)
agente = Agente(settings, db, agenda, notificar=lambda tel, txt: enviar(tel, txt))
transcritor = Transcritor(settings.whisper_model)

_buffers: dict[str, list[str]] = {}
_nomes: dict[str, str | None] = {}
_timers: dict[str, asyncio.Task] = {}


async def enviar(telefone: str, texto: str) -> None:
    await wa.enviar_texto(telefone, texto)


async def _processar(telefone: str) -> None:
    await asyncio.sleep(ESPERA_JUNTAR_S)
    _timers.pop(telefone, None)
    textos = _buffers.pop(telefone, [])
    if not textos or db.pausado(telefone):
        return
    try:
        respostas = await agente.responder(telefone, "\n".join(textos), _nomes.get(telefone))
        for r in respostas:
            await enviar(telefone, r)
    except Exception:  # noqa: BLE001
        log.exception("Falha ao atender %s", telefone)


async def _receber(msg: Recebida) -> None:
    if not msg.telefone or (msg.id and db.ja_processada(msg.id)):
        return
    if msg.tipo == "humano":
        texto = (msg.texto or "").strip()
        if texto.lower() == COMANDO_RETOMAR:
            db.retomar(msg.telefone)
            log.info("Agente retomado para %s", msg.telefone)
        elif not wa.foi_o_bot(msg.telefone, texto):
            db.pausar(msg.telefone, datetime.now(settings.tz) + timedelta(hours=PAUSA_HUMANO_H))
            _buffers.pop(msg.telefone, None)
            log.info("Escritório assumiu a conversa com %s; agente pausado por %dh", msg.telefone, PAUSA_HUMANO_H)
        return
    if db.pausado(msg.telefone):
        return
    if msg.tipo == "audio":
        # transcreve em segundo plano para o webhook responder logo
        asyncio.create_task(_transcrever_e_guardar(msg))
        return
    if msg.tipo == "outro" and not msg.texto:
        msg.texto = "[A pessoa mandou uma imagem/arquivo/figurinha sem texto.]"
    if msg.texto:
        _guardar(msg)


async def _transcrever_e_guardar(msg: Recebida) -> None:
    try:
        audio, sufixo = await wa.baixar_audio(msg)
        texto = await transcritor.transcrever(audio, sufixo)
        msg.texto = f"[Áudio transcrito] {texto}" if texto else "[Áudio sem fala reconhecível]"
    except Exception:  # noqa: BLE001
        log.exception("Falha ao transcrever áudio")
        msg.texto = "[A pessoa mandou um áudio, mas não consegui ouvir. Peça para escrever.]"
    _guardar(msg)


def _guardar(msg: Recebida) -> None:
    _buffers.setdefault(msg.telefone, []).append(msg.texto)
    _nomes[msg.telefone] = msg.nome or _nomes.get(msg.telefone)
    if (t := _timers.get(msg.telefone)) and not t.done():
        t.cancel()
    _timers[msg.telefone] = asyncio.create_task(_processar(msg.telefone))


@asynccontextmanager
async def lifespan(_: FastAPI):
    tarefa = asyncio.create_task(loop_lembretes(settings, db, wa))
    log.info("Agente no ar — canal: %s | Google Agenda: %s", settings.whatsapp_provider,
             "sim" if agenda.google else "não (agenda local)")
    yield
    tarefa.cancel()


app = FastAPI(title="Agente de atendimento — advocacia", lifespan=lifespan)


def _checar_token(token: str | None) -> None:
    if settings.webhook_token and not secrets.compare_digest(token or "", settings.webhook_token):
        raise HTTPException(status_code=403)


@app.get("/")
async def saude():
    return {"ok": True, "canal": settings.whatsapp_provider}


# ---- Evolution API: configure o webhook para POST /webhook/evolution (evento MESSAGES_UPSERT)
@app.post("/webhook/evolution")
async def webhook_evolution(request: Request, token: str | None = Query(None)):
    _checar_token(token)
    for m in wa.interpretar(await request.json()):
        await _receber(m)
    return {"ok": True}


# ---- Meta Cloud API: verificação (GET) e mensagens (POST) em /webhook/meta
@app.get("/webhook/meta")
async def webhook_meta_verificar(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == settings.meta_verify_token:
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(status_code=403)


@app.post("/webhook/meta")
async def webhook_meta(request: Request):
    for m in wa.interpretar(await request.json()):
        await _receber(m)
    return {"ok": True}


# ---- Painel simples para o advogado: /painel?token=SEU_WEBHOOK_TOKEN
@app.get("/painel", response_class=HTMLResponse)
async def painel(token: str | None = Query(None)):
    if not settings.webhook_token:
        raise HTTPException(status_code=403, detail="Defina WEBHOOK_TOKEN no .env para abrir o painel.")
    _checar_token(token)
    esc = html.escape
    consultas = "".join(
        f"<tr><td>{esc(fmt_data_hora(datetime.fromisoformat(c['inicio']).astimezone(settings.tz)))}</td>"
        f"<td>{esc(c['nome'])}</td><td><a href='https://wa.me/{esc(c['telefone'])}'>+{esc(c['telefone'])}</a></td>"
        f"<td>{esc(c['area'] or '')}</td><td>{esc(c['modalidade'] or '')}</td><td>{esc(c['resumo'] or '')}</td></tr>"
        for c in db.consultas_futuras())
    triagens = "".join(
        f"<tr><td>{esc(t['criado_em'][:16].replace('T', ' '))}</td><td>{'🚨' if t['urgente'] else ''}</td>"
        f"<td>{esc(t['nome'] or '')}</td><td><a href='https://wa.me/{esc(t['telefone'])}'>+{esc(t['telefone'])}</a></td>"
        f"<td>{esc(t['area'] or '')}</td><td>{esc(t['resumo'] or '')}</td></tr>"
        for t in db.triagens(50))
    return f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Painel do agente</title>
<style>body{{font-family:system-ui;margin:16px;background:#111;color:#eee}}table{{border-collapse:collapse;width:100%;margin-bottom:32px;font-size:14px}}
td,th{{border-bottom:1px solid #333;padding:6px;text-align:left;vertical-align:top}}a{{color:#6cf}}</style></head><body>
<h2>Próximas consultas</h2><table><tr><th>Quando</th><th>Cliente</th><th>WhatsApp</th><th>Área</th><th>Modalidade</th><th>Resumo</th></tr>{consultas}</table>
<h2>Triagens recentes</h2><table><tr><th>Data</th><th></th><th>Nome</th><th>WhatsApp</th><th>Área</th><th>Resumo</th></tr>{triagens}</table>
</body></html>"""
