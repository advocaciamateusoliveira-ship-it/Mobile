"""Transcrição de áudios do WhatsApp com faster-whisper (roda no próprio servidor, sem chave)."""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


class Transcritor:
    def __init__(self, modelo: str = "small"):
        self.modelo_nome = modelo
        self._modelo = None

    def _carregar(self):
        if self._modelo is None:
            from faster_whisper import WhisperModel

            log.info("Carregando modelo de transcrição '%s' (a primeira vez baixa o modelo)…", self.modelo_nome)
            self._modelo = WhisperModel(self.modelo_nome, device="cpu", compute_type="int8")
        return self._modelo

    def _transcrever(self, audio: bytes, sufixo: str) -> str:
        with tempfile.NamedTemporaryFile(suffix=sufixo, delete=False) as f:
            f.write(audio)
            caminho = Path(f.name)
        try:
            segmentos, _ = self._carregar().transcribe(str(caminho), language="pt", vad_filter=True)
            return " ".join(s.text.strip() for s in segmentos).strip()
        finally:
            caminho.unlink(missing_ok=True)

    async def transcrever(self, audio: bytes, sufixo: str = ".ogg") -> str:
        return await asyncio.to_thread(self._transcrever, audio, sufixo)
