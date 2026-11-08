"""Agenda: calcula horários livres e cria/cancela compromissos.

Duas fontes de "ocupado":
  * a agenda local (consultas marcadas pelo agente + bloqueios manuais, no SQLite);
  * o Google Agenda do advogado, se configurado (tudo o que estiver lá conta como ocupado).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)


class GoogleAgenda:
    """Acesso ao Google Agenda por conta de serviço (service account)."""

    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self, sa_file: str, calendar_id: str, tz_name: str):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(sa_file, scopes=self.SCOPES)
        self.svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.calendar_id = calendar_id
        self.tz_name = tz_name

    def ocupados(self, inicio: datetime, fim: datetime) -> list[tuple[datetime, datetime]]:
        body = {"timeMin": inicio.isoformat(), "timeMax": fim.isoformat(), "items": [{"id": self.calendar_id}]}
        resp = self.svc.freebusy().query(body=body).execute()
        busy = resp["calendars"][self.calendar_id].get("busy", [])
        return [(datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                 datetime.fromisoformat(b["end"].replace("Z", "+00:00"))) for b in busy]

    def criar_evento(self, inicio: datetime, fim: datetime, titulo: str, descricao: str) -> str:
        ev = {
            "summary": titulo,
            "description": descricao,
            "start": {"dateTime": inicio.isoformat(), "timeZone": self.tz_name},
            "end": {"dateTime": fim.isoformat(), "timeZone": self.tz_name},
        }
        return self.svc.events().insert(calendarId=self.calendar_id, body=ev).execute()["id"]

    def cancelar_evento(self, event_id: str) -> None:
        self.svc.events().delete(calendarId=self.calendar_id, eventId=event_id).execute()


class Agenda:
    def __init__(self, settings: Settings, db: DB, google: GoogleAgenda | None = None):
        self.s = settings
        self.db = db
        self.google = google
        if google is None and settings.google_sa_file and settings.google_calendar_id:
            try:
                self.google = GoogleAgenda(settings.google_sa_file, settings.google_calendar_id, str(settings.tz))
                log.info("Google Agenda conectado: %s", settings.google_calendar_id)
            except Exception:  # noqa: BLE001 — segue com a agenda local
                log.exception("Não consegui conectar ao Google Agenda; usando só a agenda local.")

    # ------------------------------------------------------------ ocupação
    def _ocupados(self, inicio: datetime, fim: datetime) -> list[tuple[datetime, datetime]]:
        out = [(datetime.fromisoformat(c["inicio"]), datetime.fromisoformat(c["fim"]))
               for c in self.db.consultas_entre(inicio, fim)]
        out += [(datetime.fromisoformat(b["inicio"]), datetime.fromisoformat(b["fim"]))
                for b in self.db.bloqueios_entre(inicio, fim)]
        if self.google:
            out += self.google.ocupados(inicio, fim)
        return out

    # ------------------------------------------------------ horários livres
    def horarios_livres(self, a_partir_de: date | None = None, dias: int = 7, agora: datetime | None = None,
                        periodo: str | None = None, limite: int = 12) -> list[datetime]:
        """Lista inícios de horários livres.

        periodo: None | "manha" | "tarde" (filtra pelo horário de início).
        """
        tz = self.s.tz
        agora = (agora or datetime.now(tz)).astimezone(tz)
        dur = timedelta(minutes=int(self.s.consulta.get("duracao_minutos", 60)))
        passo = dur + timedelta(minutes=int(self.s.consulta.get("intervalo_entre_consultas_minutos", 0)))
        minimo = agora + timedelta(hours=float(self.s.consulta.get("antecedencia_minima_horas", 0)))
        max_dia = agora.date() + timedelta(days=int(self.s.consulta.get("dias_a_frente", 21)))

        inicio_dia = max(a_partir_de or agora.date(), agora.date())
        dias = max(1, min(dias, (max_dia - inicio_dia).days + 1))
        if dias <= 0 or inicio_dia > max_dia:
            return []

        janela_ini = datetime.combine(inicio_dia, time(0, 0), tz)
        janela_fim = janela_ini + timedelta(days=dias)
        ocupados = self._ocupados(janela_ini, janela_fim)

        livres: list[datetime] = []
        for d in range(dias):
            dia = inicio_dia + timedelta(days=d)
            for ini_s, fim_s in self.s.expediente.get(dia.weekday(), []):
                slot = datetime.combine(dia, time.fromisoformat(ini_s), tz)
                fim_faixa = datetime.combine(dia, time.fromisoformat(fim_s), tz)
                while slot + dur <= fim_faixa:
                    if slot >= minimo and not any(o_i < slot + dur and o_f > slot for o_i, o_f in ocupados):
                        if periodo is None or (periodo == "manha" and slot.hour < 12) or (
                                periodo == "tarde" and slot.hour >= 12):
                            livres.append(slot)
                    slot += passo
                    if len(livres) >= limite:
                        return livres
        return livres

    def horario_disponivel(self, inicio: datetime, agora: datetime | None = None) -> bool:
        inicio = inicio.astimezone(self.s.tz)
        livres = self.horarios_livres(a_partir_de=inicio.date(), dias=1, agora=agora, limite=100)
        return any(s == inicio for s in livres)

    # ------------------------------------------------------- criar/cancelar
    def marcar(self, telefone: str, nome: str, inicio: datetime, area: str, resumo: str, modalidade: str,
               agora: datetime | None = None) -> dict:
        inicio = inicio.astimezone(self.s.tz)
        if not self.horario_disponivel(inicio, agora=agora):
            return {"ok": False, "erro": "Esse horário não está mais livre. Consulte os horários de novo."}
        fim = inicio + timedelta(minutes=int(self.s.consulta.get("duracao_minutos", 60)))
        event_id = None
        if self.google:
            try:
                event_id = self.google.criar_evento(
                    inicio, fim,
                    titulo=f"Consulta — {nome} ({area})",
                    descricao=f"WhatsApp: +{telefone}\nModalidade: {modalidade}\n\nResumo da triagem:\n{resumo}\n\n"
                              f"(Marcado pelo assistente virtual)",
                )
            except Exception:  # noqa: BLE001
                log.exception("Falha ao criar evento no Google Agenda; consulta fica só na agenda local.")
        cid = self.db.criar_consulta(telefone, nome, inicio, fim, area, resumo, modalidade, event_id)
        return {"ok": True, "consulta_id": cid, "inicio": inicio, "fim": fim}

    def cancelar(self, consulta_id: int) -> bool:
        c = self.db.consulta(consulta_id)
        if not c or c["status"] != "confirmada":
            return False
        if self.google and c.get("google_event_id"):
            try:
                self.google.cancelar_evento(c["google_event_id"])
            except Exception:  # noqa: BLE001
                log.exception("Falha ao apagar evento do Google Agenda.")
        self.db.cancelar_consulta(consulta_id)
        return True
