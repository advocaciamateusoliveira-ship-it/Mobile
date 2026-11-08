"""Canais de WhatsApp: Evolution API (número comum/Business via QR Code) e Meta Cloud API (oficial)."""
from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class Recebida:
    id: str
    telefone: str              # só dígitos, ex.: 5511999998888
    nome: str | None
    texto: str | None          # None quando for áudio
    audio_ref: Any = None      # referência para baixar o áudio
    tipo: str = "texto"        # texto | audio | outro | humano (enviada pelo próprio escritório)


class WhatsApp:
    """Base. Guarda o que o próprio agente enviou para não confundir o eco com uma resposta humana."""

    _ECO_S = 300

    def _registrar_envio(self, telefone: str, texto: str) -> None:
        reg = self.__dict__.setdefault("_enviadas", {})
        reg.setdefault(telefone, []).append((time.monotonic(), texto.strip()))

    def foi_o_bot(self, telefone: str, texto: str) -> bool:
        reg = self.__dict__.setdefault("_enviadas", {})
        agora = time.monotonic()
        reg[telefone] = [(t, x) for t, x in reg.get(telefone, []) if agora - t < self._ECO_S]
        return any(x == texto.strip() for _, x in reg[telefone])

    async def enviar_texto(self, telefone: str, texto: str) -> None: ...
    async def enviar_lembrete(self, telefone: str, texto: str, nome: str, quando: str) -> None:
        await self.enviar_texto(telefone, texto)
    def interpretar(self, payload: dict) -> list[Recebida]: ...
    async def baixar_audio(self, msg: Recebida) -> tuple[bytes, str]: ...


def _digitos(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


# ============================================================ Evolution API
class EvolutionAPI(WhatsApp):
    """https://doc.evolution-api.com — conecta um número de WhatsApp lendo um QR Code."""

    def __init__(self, url: str, api_key: str, instancia: str):
        self.url, self.instancia = url.rstrip("/"), instancia
        self.http = httpx.AsyncClient(headers={"apikey": api_key}, timeout=60)

    async def enviar_texto(self, telefone: str, texto: str) -> None:
        self._registrar_envio(telefone, texto)
        r = await self.http.post(f"{self.url}/message/sendText/{self.instancia}",
                                 json={"number": telefone, "text": texto})
        r.raise_for_status()

    def interpretar(self, payload: dict) -> list[Recebida]:
        evento = (payload.get("event") or "").lower().replace("_", ".")
        if evento != "messages.upsert":
            return []
        dados = payload.get("data") or {}
        itens = dados if isinstance(dados, list) else [dados]
        out = []
        for d in itens:
            key = d.get("key") or {}
            jid = key.get("remoteJid") or ""
            if jid.endswith("@g.us") or jid.startswith("status@") or jid.endswith("@newsletter"):
                continue
            if jid.endswith("@lid"):  # contas com identificador oculto: o número vem em outro campo
                jid = key.get("senderPn") or key.get("remoteJidAlt") or jid
            telefone = _digitos(jid.split("@")[0])
            msg = d.get("message") or {}
            texto = msg.get("conversation") or (msg.get("extendedTextMessage") or {}).get("text")
            if key.get("fromMe"):  # alguém do escritório respondeu pelo celular/WhatsApp Web
                out.append(Recebida(id=key.get("id", ""), telefone=telefone, nome=None, texto=texto or "",
                                    tipo="humano"))
                continue
            base = dict(id=key.get("id", ""), telefone=telefone, nome=d.get("pushName"))
            if texto:
                out.append(Recebida(texto=texto, **base))
            elif "audioMessage" in msg:
                out.append(Recebida(texto=None, tipo="audio", audio_ref=d, **base))
            else:
                legenda = ((msg.get("imageMessage") or {}).get("caption")
                           or (msg.get("documentMessage") or {}).get("caption"))
                out.append(Recebida(texto=legenda, tipo="outro", **base))
        return out

    async def baixar_audio(self, msg: Recebida) -> tuple[bytes, str]:
        d = msg.audio_ref or {}
        b64 = (d.get("message") or {}).get("base64")  # vem pronto se "Webhook Base64" estiver ligado
        if not b64:
            r = await self.http.post(f"{self.url}/chat/getBase64FromMediaMessage/{self.instancia}",
                                     json={"message": {"key": d.get("key", {})}, "convertToMp4": False})
            r.raise_for_status()
            b64 = r.json().get("base64", "")
        return base64.b64decode(b64), ".ogg"


# ======================================================= Meta Cloud API
class MetaCloudAPI(WhatsApp):
    """API oficial do WhatsApp (developers.facebook.com). Lembretes fora da janela de 24h exigem template."""

    GRAPH = "https://graph.facebook.com/v21.0"

    def __init__(self, token: str, phone_number_id: str):
        self.phone_number_id = phone_number_id
        self.http = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60)
        self.template_lembrete = os.getenv("META_TEMPLATE_LEMBRETE", "")
        self.template_idioma = os.getenv("META_TEMPLATE_IDIOMA", "pt_BR")

    async def _enviar(self, corpo: dict) -> None:
        r = await self.http.post(f"{self.GRAPH}/{self.phone_number_id}/messages",
                                 json={"messaging_product": "whatsapp", **corpo})
        if r.status_code >= 400:
            log.error("Meta API %s: %s", r.status_code, r.text)
        r.raise_for_status()

    async def enviar_texto(self, telefone: str, texto: str) -> None:
        await self._enviar({"to": telefone, "type": "text", "text": {"body": texto}})

    async def enviar_lembrete(self, telefone: str, texto: str, nome: str, quando: str) -> None:
        if not self.template_lembrete:
            return await self.enviar_texto(telefone, texto)
        await self._enviar({"to": telefone, "type": "template", "template": {
            "name": self.template_lembrete, "language": {"code": self.template_idioma},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": nome},
                                                           {"type": "text", "text": quando}]}]}})

    def interpretar(self, payload: dict) -> list[Recebida]:
        out = []
        for entry in payload.get("entry", []):
            for ch in entry.get("changes", []):
                v = ch.get("value") or {}
                nomes = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in v.get("contacts", [])}
                for m in v.get("messages", []):
                    base = dict(id=m.get("id", ""), telefone=_digitos(m.get("from", "")), nome=nomes.get(m.get("from")))
                    t = m.get("type")
                    if t == "text":
                        out.append(Recebida(texto=m["text"]["body"], **base))
                    elif t == "audio":
                        out.append(Recebida(texto=None, tipo="audio", audio_ref=m["audio"]["id"], **base))
                    elif t == "button":
                        out.append(Recebida(texto=(m.get("button") or {}).get("text"), **base))
                    else:
                        cap = (m.get(t) or {}).get("caption") if isinstance(m.get(t), dict) else None
                        out.append(Recebida(texto=cap, tipo="outro", **base))
        return out

    async def baixar_audio(self, msg: Recebida) -> tuple[bytes, str]:
        r = await self.http.get(f"{self.GRAPH}/{msg.audio_ref}")
        r.raise_for_status()
        r2 = await self.http.get(r.json()["url"])
        r2.raise_for_status()
        return r2.content, ".ogg"


# ============================================================ Console (testes)
class Console(WhatsApp):
    """Não envia nada: imprime no terminal. Usado pelo simulador e pelos testes."""

    def __init__(self):
        self.enviadas: list[tuple[str, str]] = []

    async def enviar_texto(self, telefone: str, texto: str) -> None:
        self.enviadas.append((telefone, texto))
        print(f"\n📤 [para +{telefone}]\n{texto}\n")

    def interpretar(self, payload: dict) -> list[Recebida]:
        return []


def criar_whatsapp(s: Settings) -> WhatsApp:
    if s.whatsapp_provider == "meta":
        return MetaCloudAPI(s.meta_token, s.meta_phone_number_id)
    if s.whatsapp_provider == "console":
        return Console()
    return EvolutionAPI(s.evolution_url, s.evolution_api_key, s.evolution_instance)
