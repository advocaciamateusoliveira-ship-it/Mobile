# Agente de atendimento no WhatsApp — Advocacia

Assistente virtual que atende o WhatsApp do escritório 24 horas: entende texto **e áudio**, faz a
triagem do caso, identifica urgências, consulta a agenda real, marca a consulta, avisa o advogado e
manda lembretes antes do horário. É o mesmo fluxo do reel da clínica odontológica, adaptado para advocacia.

```
WhatsApp ──► transcreve áudio ──► agente (Claude) ──► agenda (local ou Google Agenda)
                                     │
                                     ├─► ficha de triagem (banco de dados + painel)
                                     ├─► aviso no WhatsApp do advogado (nova consulta / urgência)
                                     └─► lembretes 24h e 2h antes da consulta
```

## O que ele faz

| Etapa | Como funciona |
|---|---|
| Recebe texto e áudio | Áudios são transcritos no próprio servidor (faster-whisper, sem custo por minuto). Mensagens seguidas são juntadas antes de responder. |
| Triagem | Identifica a área (trabalhista, família, previdenciário…), resume o caso e grava a ficha. |
| Urgência | Preso agora, violência doméstica, prazo/audiência em 3 dias etc.: **avisa você na hora** no seu WhatsApp, com link para responder o cliente. |
| Agenda | Só oferece horários **livres de verdade**, dentro do expediente e com antecedência mínima. Se o Google Agenda estiver conectado, tudo o que estiver nele conta como ocupado. |
| Marca, remarca e cancela | Confirma com o cliente antes, cria o evento e manda o resumo para você. |
| Informações | Endereço, mapa, valor da consulta, modalidade (presencial/online). |
| Lembretes | 24h e 2h antes (configurável). |
| Painel | `/painel?token=...` lista as próximas consultas e as triagens. |

**O que ele NÃO faz (de propósito):** não dá parecer jurídico, não promete resultado, não fala de
chances de êxito e não pede documentos ou CPF pelo WhatsApp (regras da OAB e LGPD). Ele se apresenta
como assistente virtual.

---

## Passo 1 — Configurar o escritório

Edite **`config/escritorio.yaml`**: nome, endereço, link do mapa, seu WhatsApp pessoal (para receber
os avisos), horários de atendimento, duração e valor da consulta, áreas e o que é urgente.
O agente só fala o que estiver nesse arquivo.

## Passo 2 — Testar no computador (sem WhatsApp)

Precisa de Python 3.11 ou mais novo.

```bash
cd agente-advocacia
python -m venv .venv && source .venv/bin/activate     # no Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Crie uma chave em **console.anthropic.com → API Keys**, cole no `.env` em `ANTHROPIC_API_KEY` e rode:

```bash
python simulador.py
```

Você conversa no terminal como se fosse um cliente. Teste, por exemplo:
- "Oi, fui demitida semana passada e não me pagaram nada" → triagem e oferta de horários
- "Meu filho foi preso agora" → aviso de urgência para você
- "Quero remarcar minha consulta" → lista e remarca
- `/consultas` mostra a agenda, `/novo` zera a conversa.

Os testes automáticos (não gastam API) rodam com `pytest`.

## Passo 3 — Conectar o WhatsApp

Escolha **um** dos dois caminhos e ajuste `WHATSAPP_PROVIDER` no `.env`.

### Opção A — Evolution API (mais rápido para testar)
Conecta um número de WhatsApp comum ou Business lendo um QR Code, como o WhatsApp Web.

1. Suba a Evolution API seguindo a documentação oficial (Docker): https://doc.evolution-api.com
2. Crie uma instância com o nome do `.env` (`EVOLUTION_INSTANCE`) e leia o QR Code com o celular do número do escritório.
3. Configure o webhook da instância:
   - URL: `https://SEU-SERVIDOR/webhook/evolution?token=SEU_WEBHOOK_TOKEN`
   - Evento: `MESSAGES_UPSERT`
4. Preencha `EVOLUTION_URL`, `EVOLUTION_API_KEY` e `EVOLUTION_INSTANCE` no `.env`.

> ⚠️ A Evolution API **não é oficial** do WhatsApp. Funciona bem, mas existe risco de o número ser
> bloqueado. Para testar, use de preferência um **chip separado**, não o número principal do escritório.

### Opção B — API oficial da Meta (recomendado para produção)
1. Crie um app em https://developers.facebook.com → produto **WhatsApp** e cadastre o número.
2. Webhook: URL `https://SEU-SERVIDOR/webhook/meta`, token de verificação = `META_VERIFY_TOKEN`, assine o campo `messages`.
3. Preencha `META_TOKEN` (token permanente de usuário do sistema) e `META_PHONE_NUMBER_ID`.
4. Na API oficial, mensagens enviadas **mais de 24h depois** da última mensagem do cliente precisam de
   um *template* aprovado. Crie um template de lembrete com duas variáveis (`{{1}}` = nome,
   `{{2}}` = data e hora) e informe o nome em `META_TEMPLATE_LEMBRETE`. O mesmo vale para os avisos
   ao seu WhatsApp: mande uma mensagem para o número do bot uma vez por dia, ou use a Opção A para os avisos.

## Passo 4 — Colocar no ar

O servidor precisa de um endereço **HTTPS público** para o WhatsApp mandar as mensagens.

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- **Para testar hoje:** rode no seu computador e exponha com um túnel (`cloudflared tunnel --url http://localhost:8000` ou `ngrok http 8000`). Use a URL gerada no webhook.
- **Para deixar 24h:** uma VPS simples (2 GB de RAM já basta para o modelo de transcrição `small`) com o mesmo comando, atrás de um proxy HTTPS (Caddy ou Nginx).

Defina `WEBHOOK_TOKEN` no `.env`: ele protege o webhook da Evolution e o painel (`/painel?token=...`).

## Passo 5 (opcional) — Google Agenda

Sem isso, o agente usa a agenda própria (arquivo `data/agente.db`). Com o Google Agenda, ele
respeita seus compromissos (audiências, reuniões) e cria os eventos das consultas lá.

1. Em https://console.cloud.google.com crie um projeto, ative a **Google Calendar API** e crie uma **conta de serviço**. Gere uma chave JSON e salve em `credenciais/google-service-account.json`.
2. No Google Agenda, em *Configurações da agenda → Compartilhar com pessoas específicas*, adicione o e-mail da conta de serviço com permissão **"Fazer alterações nos eventos"**.
3. No `.env`: `GOOGLE_CALENDAR_ID` = o ID da agenda (fica em *Integrar agenda*; para a principal costuma ser o seu e-mail).

---

## Custos

- **Claude:** cobrado por uso na sua conta da Anthropic. Um atendimento completo (triagem + marcação) costuma
  custar poucos centavos de dólar. O histórico usa cache, o que barateia as conversas longas. Acompanhe
  o gasto em console.anthropic.com e defina um limite mensal lá.
- **Transcrição:** roda no seu servidor, sem custo por áudio.
- **WhatsApp:** a Evolution API é gratuita. A API oficial da Meta cobra por conversa iniciada pela empresa (lembretes).

## Estrutura

```
config/escritorio.yaml   ← dados do escritório (edite aqui)
app/prompts.py           ← instruções e regras do agente
app/agent.py             ← conversa com o Claude e ferramentas (horários, triagem, marcar, cancelar)
app/agenda.py            ← horários livres, agenda local e Google Agenda
app/whatsapp.py          ← Evolution API e Meta Cloud API
app/transcricao.py       ← áudio → texto
app/lembretes.py         ← lembretes antes da consulta
app/main.py              ← servidor (webhooks + painel)
simulador.py             ← teste pelo terminal
tests/                   ← testes automáticos (pytest)
```

## Cuidados

- O agente é de **atendimento e agendamento**. Revise as respostas nas primeiras semanas e ajuste `app/prompts.py` e `config/escritorio.yaml` conforme o seu jeito de atender.
- Conversas ficam gravadas em `data/agente.db`. Faça backup desse arquivo e trate-o como dado sigiloso de clientes (LGPD).
- **Assumir uma conversa:** (Opção A, Evolution API) basta responder o cliente pelo celular ou WhatsApp Web do escritório. O agente percebe e fica **12 horas sem responder aquele cliente**. Para devolver antes, mande `#bot` na conversa com o cliente.
