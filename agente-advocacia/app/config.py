"""Carrega o .env e o config/escritorio.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DIAS = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]  # índice = date.weekday()


@dataclass
class Settings:
    raw: dict
    db_path: Path
    whatsapp_provider: str
    evolution_url: str
    evolution_api_key: str
    evolution_instance: str
    meta_token: str
    meta_phone_number_id: str
    meta_verify_token: str
    google_sa_file: str
    google_calendar_id: str
    whisper_model: str
    webhook_token: str
    model: str = "claude-opus-5-5"
    expediente: dict[int, list[tuple[str, str]]] = field(default_factory=dict)

    # atalhos para o YAML
    @property
    def escritorio(self) -> dict:
        return self.raw["escritorio"]

    @property
    def consulta(self) -> dict:
        return self.raw["consulta"]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.raw.get("fuso", "America/Sao_Paulo"))

    @property
    def whatsapp_advogado(self) -> str:
        return str(self.raw.get("whatsapp_advogado", "")).strip()

    @property
    def lembretes_horas(self) -> list[int]:
        return [int(x["horas_antes"]) for x in self.raw.get("lembretes", [])]

    @property
    def nova_sessao_apos_horas(self) -> int:
        return int(self.raw.get("nova_sessao_apos_horas", 12))


def _parse_expediente(exp: dict) -> dict[int, list[tuple[str, str]]]:
    out: dict[int, list[tuple[str, str]]] = {}
    for i, dia in enumerate(DIAS):
        faixas = []
        for faixa in exp.get(dia) or []:
            ini, fim = [p.strip() for p in faixa.split("-")]
            faixas.append((ini, fim))
        out[i] = faixas
    return out


def load_settings(config_path: Path | None = None, db_path: Path | None = None) -> Settings:
    config_path = config_path or Path(os.getenv("ESCRITORIO_CONFIG", ROOT / "config" / "escritorio.yaml"))
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Settings(
        raw=raw,
        db_path=db_path or Path(os.getenv("DB_PATH", ROOT / "data" / "agente.db")),
        whatsapp_provider=os.getenv("WHATSAPP_PROVIDER", "evolution").lower(),
        evolution_url=os.getenv("EVOLUTION_URL", "http://localhost:8080").rstrip("/"),
        evolution_api_key=os.getenv("EVOLUTION_API_KEY", ""),
        evolution_instance=os.getenv("EVOLUTION_INSTANCE", "advocacia"),
        meta_token=os.getenv("META_TOKEN", ""),
        meta_phone_number_id=os.getenv("META_PHONE_NUMBER_ID", ""),
        meta_verify_token=os.getenv("META_VERIFY_TOKEN", ""),
        google_sa_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", ""),
        google_calendar_id=os.getenv("GOOGLE_CALENDAR_ID", ""),
        whisper_model=os.getenv("WHISPER_MODEL", "small"),
        webhook_token=os.getenv("WEBHOOK_TOKEN", ""),
        model=os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
        expediente=_parse_expediente(raw.get("expediente", {})),
    )
