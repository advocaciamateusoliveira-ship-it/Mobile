"""Testes sem chamar a API: agenda, WhatsApp, lembretes e o laço do agente com um Claude falso."""
from __future__ import annotations

import asyncio
import copy
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from anthropic.types.beta import BetaMessage

from app.agenda import Agenda
from app.agent import Agente, fmt_data_hora
from app.config import ROOT, load_settings
from app.db import DB
from app.lembretes import enviar_lembretes_pendentes
from app.whatsapp import Console, EvolutionAPI, MetaCloudAPI


@pytest.fixture
def s():
    return load_settings(ROOT / "config" / "escritorio.yaml", db_path=Path(":memory:"))


@pytest.fixture
def db():
    return DB(":memory:")


def _dt(s, y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=s.tz)


# ------------------------------------------------------------------ agenda
def test_horarios_respeitam_expediente_e_antecedencia(s, db):
    ag = Agenda(s, db)
    agora = _dt(s, 2026, 11, 7, 23, 47)  # sábado à noite
    livres = ag.horarios_livres(a_partir_de=agora.date(), dias=4, agora=agora, limite=100)
    assert livres, "deveria haver horários"
    assert all(h.weekday() < 5 for h in livres)               # nada no fim de semana
    assert livres[0] == _dt(s, 2026, 11, 9, 9)                # segunda 9h
    assert all(h >= agora + timedelta(hours=12) for h in livres)
    assert _dt(s, 2026, 11, 9, 12) not in livres              # almoço
    assert _dt(s, 2026, 11, 9, 17) in livres
    assert _dt(s, 2026, 11, 9, 18) not in livres              # 18h + 1h passa do expediente


def test_periodo_tarde(s, db):
    ag = Agenda(s, db)
    agora = _dt(s, 2026, 11, 7, 10)
    livres = ag.horarios_livres(date(2026, 11, 9), 1, agora, "tarde", 100)
    assert livres and all(h.hour >= 12 for h in livres)


def test_marcar_ocupa_horario_e_bloqueia_conflito(s, db):
    ag = Agenda(s, db)
    agora = _dt(s, 2026, 11, 7, 10)
    alvo = _dt(s, 2026, 11, 10, 9)
    r = ag.marcar("5511999990000", "Maria Souza", alvo, "Trabalhista", "demissão", "presencial", agora=agora)
    assert r["ok"]
    assert alvo not in ag.horarios_livres(alvo.date(), 1, agora, None, 100)
    r2 = ag.marcar("5511999990001", "João", alvo, "Família", "x", "presencial", agora=agora)
    assert not r2["ok"]
    db.criar_bloqueio(_dt(s, 2026, 11, 10, 14), _dt(s, 2026, 11, 10, 18), "audiência")
    tarde = ag.horarios_livres(alvo.date(), 1, agora, "tarde", 100)
    assert tarde == []


def test_cancelar_libera(s, db):
    ag = Agenda(s, db)
    agora = _dt(s, 2026, 11, 7, 10)
    alvo = _dt(s, 2026, 11, 10, 10)
    cid = ag.marcar("55", "A", alvo, "Cível", "", "online", agora=agora)["consulta_id"]
    assert ag.cancelar(cid)
    assert alvo in ag.horarios_livres(alvo.date(), 1, agora, None, 100)


def test_fmt_data_hora(s):
    assert fmt_data_hora(_dt(s, 2026, 11, 11, 9, 30)) == "quarta, 11/11 às 9h30"
    assert fmt_data_hora(_dt(s, 2026, 11, 11, 14)) == "quarta, 11/11 às 14h"


# ---------------------------------------------------------------- whatsapp
def test_evolution_interpreta_texto_audio_e_ignora_grupo():
    wa = EvolutionAPI("http://x", "k", "inst")
    msgs = wa.interpretar({"event": "messages.upsert", "data": {
        "key": {"remoteJid": "5511999998888@s.whatsapp.net", "fromMe": False, "id": "A1"},
        "pushName": "Mariana", "message": {"conversation": "Oi, boa noite"}}})
    assert len(msgs) == 1 and msgs[0].telefone == "5511999998888" and msgs[0].texto == "Oi, boa noite"
    audio = wa.interpretar({"event": "MESSAGES_UPSERT", "data": {
        "key": {"remoteJid": "5511999998888@s.whatsapp.net", "fromMe": False, "id": "A2"},
        "message": {"audioMessage": {"seconds": 14}}}})
    assert audio[0].tipo == "audio"
    assert wa.interpretar({"event": "messages.upsert", "data": {
        "key": {"remoteJid": "123@g.us", "fromMe": False, "id": "G"}, "message": {"conversation": "x"}}}) == []
    eco = wa.interpretar({"event": "messages.upsert", "data": {
        "key": {"remoteJid": "55@s.whatsapp.net", "fromMe": True, "id": "M"}, "message": {"conversation": "x"}}})
    assert eco[0].tipo == "humano"


def test_meta_interpreta():
    wa = MetaCloudAPI("t", "123")
    msgs = wa.interpretar({"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "5511988887777", "profile": {"name": "Pedro"}}],
        "messages": [{"from": "5511988887777", "id": "w1", "type": "text", "text": {"body": "Olá"}},
                     {"from": "5511988887777", "id": "w2", "type": "audio", "audio": {"id": "MID"}}]}}]}]})
    assert [m.tipo for m in msgs] == ["texto", "audio"] and msgs[0].nome == "Pedro"


# --------------------------------------------------------------- lembretes
def test_lembretes_24h_e_2h_sem_duplicar(s, db):
    wa = Console()
    inicio = datetime.now(s.tz).replace(microsecond=0) + timedelta(hours=20)
    db.criar_consulta("5511", "Mariana Souza", inicio, inicio + timedelta(hours=1), "Trabalhista", "", "presencial")
    # marcada há menos de 24h da consulta: o lembrete de 24h não faz sentido e é pulado
    n = asyncio.run(enviar_lembretes_pendentes(s, db, wa))
    assert n == 0
    n = asyncio.run(enviar_lembretes_pendentes(s, db, wa, agora=inicio - timedelta(hours=1)))
    assert n == 1 and "Mariana" in wa.enviadas[0][1]
    assert asyncio.run(enviar_lembretes_pendentes(s, db, wa, agora=inicio - timedelta(minutes=30))) == 0


# ------------------------------------------------------- laço do agente
def _msg(content, stop):
    return BetaMessage.model_validate({
        "id": "msg_x", "type": "message", "role": "assistant", "model": "fake",
        "content": content, "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1}})


class FakeClient:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []
        outer = self

        class _M:
            async def create(self, **kw):
                outer.chamadas.append(copy.deepcopy(kw))
                return outer.respostas.pop(0)

        class _B:
            messages = _M()

        self.beta = _B()


def test_laco_agente_consulta_marca_e_avisa(s, db):
    agora = _dt(s, 2026, 11, 7, 23, 47)
    avisos = []

    async def notificar(tel, txt):
        avisos.append((tel, txt))

    fake = FakeClient([
        _msg([{"type": "thinking", "thinking": "", "signature": "sig"},
              {"type": "tool_use", "id": "t1", "name": "consultar_horarios",
               "input": {"a_partir_de": "2026-11-08", "dias": 3, "periodo": "manha"}}], "tool_use"),
        _msg([{"type": "text", "text": "Tenho segunda, 09/11 às 9h ou às 10h. Qual prefere?"}], "end_turn"),
        _msg([{"type": "tool_use", "id": "t2", "name": "registrar_triagem",
               "input": {"nome": "Mariana Souza", "area": "Trabalhista", "resumo": "Demitida sem receber",
                         "urgente": False, "motivo_urgencia": ""}},
              {"type": "tool_use", "id": "t3", "name": "marcar_consulta",
               "input": {"nome": "Mariana Souza", "data_hora": "2026-11-09T09:00", "area": "Trabalhista",
                         "resumo_caso": "Demitida sem receber", "modalidade": "presencial"}}], "tool_use"),
        _msg([{"type": "text", "text": "Prontinho, Mariana! Consulta confirmada."}], "end_turn"),
    ])
    ag = Agente(s, db, Agenda(s, db), notificar, client=fake)
    r1 = asyncio.run(ag.responder("5511999998888", "Oi, fui demitida e não recebi nada", "Mari", agora=agora))
    assert r1 == ["Tenho segunda, 09/11 às 9h ou às 10h. Qual prefere?"]
    # o resultado da ferramenta trouxe horários reais
    tool_result = fake.chamadas[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "t1" and "2026-11-09T09:00" in tool_result["content"]
    # o bloco de raciocínio foi devolvido intacto
    assert fake.chamadas[1]["messages"][1]["content"][0]["type"] == "thinking"
    assert fake.chamadas[0]["fallbacks"] == "default"

    r2 = asyncio.run(ag.responder("5511999998888", "9h, meu nome é Mariana Souza", None,
                                  agora=agora + timedelta(minutes=1)))
    assert r2 == ["Prontinho, Mariana! Consulta confirmada."]
    futuras = db.consultas_entre(_dt(s, 2026, 11, 9, 0), _dt(s, 2026, 11, 10, 0))
    assert len(futuras) == 1 and futuras[0]["nome"] == "Mariana Souza"
    assert any("Nova consulta" in t for _, t in avisos)
    # histórico salvo e sem turno pendurado
    hist, _ = db.conversa("5511999998888")
    assert hist[-1]["role"] == "assistant"
    # o prefixo enviado ao Claude só cresce (nada editado)
    assert fake.chamadas[2]["messages"][:3] == fake.chamadas[1]["messages"]


def test_urgencia_avisa_advogado(s, db):
    avisos = []

    async def notificar(tel, txt):
        avisos.append((tel, txt))

    fake = FakeClient([
        _msg([{"type": "tool_use", "id": "u1", "name": "registrar_triagem",
               "input": {"nome": "", "area": "Criminal", "resumo": "Filho preso agora",
                         "urgente": True, "motivo_urgencia": "prisão em flagrante"}}], "tool_use"),
        _msg([{"type": "text", "text": "Já avisei o advogado."}], "end_turn"),
    ])
    ag = Agente(s, db, Agenda(s, db), notificar, client=fake)
    asyncio.run(ag.responder("5511", "Meu filho foi preso agora", None, agora=_dt(s, 2026, 11, 8, 2)))
    assert avisos and "URGENTE" in avisos[0][1] and avisos[0][0] == s.whatsapp_advogado


def test_recusa_nao_quebra_historico(s, db):
    avisos = []

    async def notificar(tel, txt):
        avisos.append((tel, txt))

    fake = FakeClient([_msg([], "refusal")])
    ag = Agente(s, db, Agenda(s, db), notificar, client=fake)
    r = asyncio.run(ag.responder("5511", "oi", None, agora=_dt(s, 2026, 11, 8, 2)))
    assert "probleminha" in r[0]
    assert avisos and "+5511" in avisos[0][1]
    hist, _ = db.conversa("5511")
    assert hist[-1]["role"] == "user"


# ------------------------------------------------------------- webhook
def test_webhook_evolution_junta_mensagens_e_responde(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "w.db"))
    monkeypatch.setenv("WHATSAPP_PROVIDER", "evolution")
    monkeypatch.setenv("WEBHOOK_TOKEN", "segredo")
    import importlib

    import app.main as main
    main = importlib.reload(main)
    from fastapi.testclient import TestClient

    enviadas = []

    async def enviar(tel, txt):
        main.wa._registrar_envio(tel, txt)
        enviadas.append((tel, txt))

    monkeypatch.setattr(main.wa, "enviar_texto", enviar)
    monkeypatch.setattr(main, "ESPERA_JUNTAR_S", 0.3)
    fake = FakeClient([_msg([{"type": "text", "text": "Olá! Sou a assistente virtual."}], "end_turn")])
    main.agente.client = fake

    def payload(i, texto):
        return {"event": "messages.upsert", "data": {
            "key": {"remoteJid": "5511977776666@s.whatsapp.net", "fromMe": False, "id": f"id{i}"},
            "pushName": "Ana", "message": {"conversation": texto}}}

    with TestClient(main.app) as c:
        assert c.post("/webhook/evolution", json=payload(1, "oi")).status_code == 403
        assert c.post("/webhook/evolution?token=segredo", json=payload(1, "oi")).status_code == 200
        c.post("/webhook/evolution?token=segredo", json=payload(2, "preciso de um advogado"))
        c.post("/webhook/evolution?token=segredo", json=payload(2, "preciso de um advogado"))  # repetida
        import time
        time.sleep(1.0)
        assert c.get("/painel?token=segredo").status_code == 200
        assert enviadas == [("5511977776666", "Olá! Sou a assistente virtual.")]

        def eco(i, texto):
            p = payload(i, texto)
            p["data"]["key"]["fromMe"] = True
            return p

        # eco da própria resposta do bot: não pausa
        c.post("/webhook/evolution?token=segredo", json=eco(10, "Olá! Sou a assistente virtual."))
        assert not main.db.pausado("5511977776666")
        # o advogado respondeu pelo celular: pausa, e o agente não responde a próxima mensagem
        c.post("/webhook/evolution?token=segredo", json=eco(11, "Oi Ana, aqui é o Dr. Mateus"))
        assert main.db.pausado("5511977776666")
        c.post("/webhook/evolution?token=segredo", json=payload(12, "ah, obrigada doutor"))
        time.sleep(0.8)
        assert len(enviadas) == 1
        # #bot devolve o atendimento ao agente
        c.post("/webhook/evolution?token=segredo", json=eco(13, "#bot"))
        assert not main.db.pausado("5511977776666")
    assert len(fake.chamadas) == 1
    ultimo = fake.chamadas[0]["messages"][-1]["content"]
    assert "oi\npreciso de um advogado" in ultimo and ultimo.count("preciso") == 1
