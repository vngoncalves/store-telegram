"""Canal obrigatório: só quem é membro do canal configurado pode usar a loja.

- check(): consulta o Telegram (getChatMember). Resultado positivo fica em cache por alguns
  minutos, então os cliques seguintes não fazem chamada nenhuma.
- watch(): quem ainda não entrou fica numa fila verificada por uma thread em segundo plano;
  quando a pessoa entra no canal, a loja é liberada sozinha, sem ela precisar tocar em nada.
- Se o bot não conseguir consultar o canal (ex.: não é administrador dele), a loja NÃO é
  bloqueada: o acesso é liberado e o motivo aparece no terminal ([CANAL]).

Configuração no .env: REQUIRED_CHANNEL_ID (ex.: -1001234567890) e REQUIRED_CHANNEL_LINK.
O bot precisa ser administrador do canal para conseguir ver quem é membro.
"""
import threading
import time
from datetime import datetime

from . import config, telegram_api

MEMBER_CACHE_SECONDS = 600     # membro confirmado: não consulta de novo por 10 min
WATCH_INTERVAL = 4             # a cada quantos segundos a fila de espera é verificada
WATCH_MAX_SECONDS = 30 * 60    # verifica sozinho por até 30 min; depois só pelo botão
WATCH_MAX_USERS = 2000

_member_until = {}
_pending = {}
_lock = threading.Lock()
_thread = None
_last_warning = 0.0


def out(message):
    print(f"[CANAL {datetime.now():%H:%M:%S}] {message}", flush=True)


def channel_id():
    return config.REQUIRED_CHANNEL_ID


def channel_link():
    return config.REQUIRED_CHANNEL_LINK


def enabled():
    return bool(channel_id())


def _warn(message):
    """Avisos de configuração: no máximo 1 a cada 5 minutos, para não poluir o terminal."""
    global _last_warning
    if time.monotonic() - _last_warning >= 300 or _last_warning == 0.0:
        _last_warning = time.monotonic()
        out(message)


def is_member(user_id, use_cache=True):
    """True se o usuário pode usar a loja (membro do canal, canal desligado ou falha na consulta)."""
    if not enabled():
        return True
    now = time.monotonic()
    if use_cache and _member_until.get(user_id, 0) > now:
        return True
    try:
        member = telegram_api.call("getChatMember", {"chat_id": channel_id(), "user_id": user_id}, timeout=15) or {}
    except Exception as exc:
        _warn(f"não foi possível verificar o canal {channel_id()} ({exc}). Acesso liberado por segurança. "
              "Confira se o bot é ADMINISTRADOR do canal.")
        return True
    status = member.get("status")
    joined = status in {"creator", "administrator", "member"} or (status == "restricted" and member.get("is_member"))
    if joined:
        _member_until[user_id] = now + MEMBER_CACHE_SECONDS
    else:
        _member_until.pop(user_id, None)
    return joined


def watch(user_id, chat_id, on_join):
    """Coloca o usuário na fila de verificação automática. on_join(user_id, chat_id) roda quando ele entrar."""
    global _thread
    with _lock:
        if len(_pending) >= WATCH_MAX_USERS and user_id not in _pending:
            return
        _pending[user_id] = {"chat_id": chat_id, "since": time.monotonic(), "on_join": on_join}
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_watch_loop, name="canal-obrigatorio", daemon=True)
            _thread.start()


def unwatch(user_id):
    with _lock:
        _pending.pop(user_id, None)


def pending_count():
    return len(_pending)


def check_pending_once():
    """Uma rodada da verificação automática (a thread chama isto a cada WATCH_INTERVAL segundos)."""
    with _lock:
        items = list(_pending.items())
    now = time.monotonic()
    for user_id, entry in items:
        if now - entry["since"] > WATCH_MAX_SECONDS:
            unwatch(user_id)
            continue
        if is_member(user_id, use_cache=False):
            unwatch(user_id)
            try:
                entry["on_join"](user_id, entry["chat_id"])
            except Exception as exc:
                out(f"falha ao liberar o usuário {user_id} automaticamente: {exc}")
        time.sleep(0.05)  # não dispara dezenas de consultas de uma vez


def _watch_loop():
    while True:
        time.sleep(WATCH_INTERVAL)
        try:
            check_pending_once()
        except Exception as exc:
            out(f"erro na verificação automática: {exc}")


def startup_report():
    if not enabled():
        out("Canal obrigatório: desligado.")
        return
    try:
        chat = telegram_api.call("getChat", {"chat_id": channel_id()}) or {}
        bot_id = telegram_api.bot_info().get("id")
        me = telegram_api.call("getChatMember", {"chat_id": channel_id(), "user_id": bot_id}) or {}
    except Exception as exc:
        out(f"Canal obrigatório {channel_id()} COM PROBLEMA: {exc}. Enquanto isso, a loja fica liberada para todos.")
        return
    if me.get("status") not in {"administrator", "creator"}:
        out(f"Canal obrigatório \"{chat.get('title')}\": o bot NÃO é administrador, então não consegue ver "
            "quem é membro. Adicione o bot como administrador. Enquanto isso, a loja fica liberada para todos.")
        return
    out(f"Canal obrigatório OK: \"{chat.get('title')}\" ({channel_id()}) · link {channel_link() or '—'}")
