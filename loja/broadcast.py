"""Transmissão: copia uma mensagem do admin para todos os usuários do bot.

Usa copyMessage/copyMessages, então qualquer tipo de mensagem funciona exatamente como o admin
mandou: texto (com formatação), foto, foto com legenda, vídeo, GIF, arquivo, áudio, voz,
figurinha, enquete, localização, contato e álbuns.

O envio roda numa thread em segundo plano, respeitando o limite do Telegram (~20 mensagens/s),
com progresso ao vivo no painel do admin, botão de parar e relatório no final.
Quem bloqueou o bot é marcado e deixa de receber as próximas transmissões até voltar a usá-lo.
"""
import json
import threading
import time
from datetime import datetime

from . import storage, telegram_api

SEND_INTERVAL = 0.05      # ~20 mensagens por segundo (o limite do Telegram é ~30/s)
PROGRESS_EVERY = 3.0      # segundos entre atualizações do painel de progresso
MAX_ATTEMPTS = 3

# Erros que significam "esse usuário não recebe mais mensagens do bot"
_GONE = ("bot was blocked by the user", "user is deactivated", "chat not found",
         "bot can't initiate conversation", "peer_id_invalid", "bot was kicked")

_lock = threading.Lock()
CURRENT = None            # transmissão em andamento (dict) ou None


def out(message):
    print(f"[TRANSMISSÃO {datetime.now():%H:%M:%S}] {message}", flush=True)


def is_running():
    return CURRENT is not None and not CURRENT.get("finished")


def _copy(chat_id, source):
    """Copia a mensagem (ou o álbum) do admin para chat_id."""
    ids = source["message_ids"]
    if len(ids) == 1:
        return telegram_api.call("copyMessage", {
            "chat_id": chat_id, "from_chat_id": source["chat_id"], "message_id": ids[0],
        }, timeout=20)
    return telegram_api.call("copyMessages", {
        "chat_id": chat_id, "from_chat_id": source["chat_id"], "message_ids": json.dumps(sorted(ids)),
    }, timeout=30)


def _send_one(chat_id, source, job):
    """Retorna 'sent', 'blocked' ou 'failed'."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            _copy(chat_id, source)
            return "sent"
        except Exception as exc:
            text = str(getattr(exc, "description", "") or exc).lower()
            retry_after = getattr(exc, "retry_after", None)
            if retry_after or "too many requests" in text:
                wait = int(retry_after or 5) + 1
                out(f"limite do Telegram atingido; esperando {wait}s")
                time.sleep(wait)
                continue
            if any(fragment in text for fragment in _GONE) or getattr(exc, "code", None) == 403:
                return "blocked"
            if isinstance(exc, telegram_api.NETWORK_ERRORS) and attempt < MAX_ATTEMPTS:
                time.sleep(attempt * 2)
                continue
            job["errors"][str(exc)[:120]] = job["errors"].get(str(exc)[:120], 0) + 1
            return "failed"
    return "failed"


def start(source, targets, on_progress, on_finish):
    """Inicia a transmissão em segundo plano. source = {"chat_id", "message_ids": [...]}."""
    global CURRENT
    with _lock:
        if is_running():
            raise RuntimeError("Já existe uma transmissão em andamento.")
        job = {
            "source": source, "total": len(targets), "sent": 0, "blocked": 0, "failed": 0,
            "stop": False, "finished": False, "started": time.monotonic(), "errors": {},
            "blocked_ids": [],
        }
        CURRENT = job
    thread = threading.Thread(
        target=_run, args=(job, list(targets), on_progress, on_finish), name="transmissao", daemon=True
    )
    job["thread"] = thread
    thread.start()
    return job


def stop():
    if is_running():
        CURRENT["stop"] = True
        return True
    return False


def _run(job, targets, on_progress, on_finish):
    out(f"iniciando para {len(targets)} usuário(s)")
    last_progress = time.monotonic()
    try:
        for chat_id in targets:
            if job["stop"]:
                out("interrompida pelo admin")
                break
            result = _send_one(chat_id, job["source"], job)
            job[result] += 1
            if result == "blocked":
                job["blocked_ids"].append(chat_id)
            if time.monotonic() - last_progress >= PROGRESS_EVERY:
                last_progress = time.monotonic()
                _safe(on_progress, job)
            time.sleep(SEND_INTERVAL)
    finally:
        try:
            storage.mark_blocked(job["blocked_ids"])
        except Exception as exc:
            out(f"não foi possível marcar usuários bloqueados: {exc}")
        job["finished"] = True
        job["elapsed"] = time.monotonic() - job["started"]
        out(f"fim: {job['sent']} enviada(s), {job['blocked']} bloquearam, {job['failed']} falha(s), "
            f"{job['elapsed']:.0f}s")
        for error, count in job["errors"].items():
            out(f"   falha x{count}: {error}")
        _safe(on_finish, job)


def _safe(callback, job):
    try:
        callback(job)
    except Exception as exc:
        out(f"não foi possível atualizar o painel: {exc}")
