"""Lembretes automáticos antes das consultas (ex.: 24h e 2h antes)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from .agent import fmt_data_hora
from .config import Settings
from .db import DB
from .whatsapp import WhatsApp

log = logging.getLogger(__name__)


def texto_lembrete(s: Settings, c: dict, quando: str, horas: int) -> str:
    e = s.escritorio
    primeiro_nome = (c["nome"] or "").split(" ")[0]
    linhas = [f"Olá, {primeiro_nome}! Passando para lembrar da sua consulta com o {e['advogado']}:",
              f"📅 *{quando}*"]
    if (c.get("modalidade") or "").lower().startswith("presencial"):
        linhas.append(f"📍 {e['endereco']}\n{e.get('link_mapa', '')}".rstrip())
    else:
        linhas.append("💻 Consulta online: o link será enviado pelo escritório antes do horário.")
    linhas.append("Se precisar remarcar ou cancelar, é só responder esta mensagem.")
    return "\n".join(linhas)


async def enviar_lembretes_pendentes(s: Settings, db: DB, wa: WhatsApp, agora: datetime | None = None) -> int:
    agora = (agora or datetime.now(s.tz)).astimezone(s.tz)
    enviados = 0
    for c in db.consultas_futuras():
        inicio = datetime.fromisoformat(c["inicio"]).astimezone(s.tz)
        criada = datetime.fromisoformat(c["criado_em"]).astimezone(s.tz)
        for horas in s.lembretes_horas:
            momento = inicio - timedelta(hours=horas)
            # Só envia dentro da janela, e não manda lembrete de algo marcado depois desse momento.
            if not (momento <= agora < inicio) or criada > momento or db.lembrete_enviado(c["id"], horas):
                continue
            quando = fmt_data_hora(inicio)
            try:
                await wa.enviar_lembrete(c["telefone"], texto_lembrete(s, c, quando, horas),
                                         (c["nome"] or "").split(" ")[0], quando)
                db.marcar_lembrete(c["id"], horas)
                enviados += 1
            except Exception:  # noqa: BLE001
                log.exception("Falha ao enviar lembrete da consulta %s", c["id"])
    return enviados


async def loop_lembretes(s: Settings, db: DB, wa: WhatsApp, intervalo_s: int = 300) -> None:
    while True:
        try:
            n = await enviar_lembretes_pendentes(s, db, wa)
            if n:
                log.info("%d lembrete(s) enviado(s)", n)
        except Exception:  # noqa: BLE001
            log.exception("Erro no loop de lembretes")
        await asyncio.sleep(intervalo_s)
