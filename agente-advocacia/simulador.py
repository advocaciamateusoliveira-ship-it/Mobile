"""Converse com o agente pelo terminal, sem WhatsApp — o jeito mais rápido de testar.

    python simulador.py                 # você é um cliente novo
    python simulador.py --telefone 5511988887777 --nome "Maria"

Comandos: /novo (zera a conversa)  /consultas (lista a agenda)  /sair
Só precisa do ANTHROPIC_API_KEY no .env. Usa um banco separado: data/simulador.db
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from app.agenda import Agenda
from app.agent import Agente, fmt_data_hora
from app.config import ROOT, load_settings
from app.db import DB


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--telefone", default="5500911112222")
    ap.add_argument("--nome", default=None, help="nome que aparece no perfil do WhatsApp")
    ap.add_argument("--db", default=str(ROOT / "data" / "simulador.db"))
    args = ap.parse_args()

    s = load_settings(db_path=Path(args.db))
    db = DB(s.db_path)
    agente = Agente(s, db, Agenda(s, db), notificar=lambda tel, txt: _aviso(tel, txt))

    print(f"Simulador — {s.escritorio['nome']}. Você é o cliente +{args.telefone}. /sair para terminar.\n")
    while True:
        try:
            texto = await asyncio.to_thread(input, "🧑 Você: ")
        except (EOFError, KeyboardInterrupt):
            break
        texto = texto.strip()
        if not texto:
            continue
        if texto == "/sair":
            break
        if texto == "/novo":
            db.apagar_conversa(args.telefone)
            print("(conversa zerada)\n")
            continue
        if texto == "/consultas":
            for c in db.consultas_futuras():
                print(f"  #{c['id']} {fmt_data_hora(datetime.fromisoformat(c['inicio']).astimezone(s.tz))} — "
                      f"{c['nome']} ({c['area']})")
            continue
        for r in await agente.responder(args.telefone, texto, args.nome):
            print(f"\n🤖 Agente: {r}\n")


async def _aviso(tel: str, txt: str) -> None:
    print(f"\n   📲 [aviso para o advogado +{tel}]\n   " + txt.replace("\n", "\n   ") + "\n")


if __name__ == "__main__":
    asyncio.run(main())
