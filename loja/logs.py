"""Canal de logs: publica num canal do Telegram tudo o que acontece na loja.

Organização:
  1. Terminal ........ out()/warn(): toda linha começa com [LOGS] e diz o motivo.
  2. Canal ........... channel_id(), parse_channel_input(), explain_error()
  3. Diagnóstico ..... diagnose(): o canal existe? o bot é admin? pode publicar?
  4. Envio ........... fila em segundo plano, até 4 tentativas, espera em limite de taxa,
                       troca de ID quando o grupo vira supergrupo e reenvio sem HTML se a formatação falhar.
  5. Eventos ......... new_user(), purchase(), pix_confirmed()... (o que os handlers chamam)

Regra principal: uma falha de log NUNCA interrompe a operação do cliente; o motivo vai para o terminal.
Configuração: painel admin → ⚙️ Configurações → 📢 Definir canal de logs (ou LOG_CHANNEL_ID no .env).
"""
import queue
import re
import socket
import threading
import time
import urllib.error
from datetime import datetime
from decimal import Decimal
from html import escape, unescape

from . import config, storage, telegram_api, validators


LINE = "━━━━━━━━━━━━━━━━"
MAX_ATTEMPTS = 4
MAX_TEXT = 4096

# Em produção os logs saem por uma thread (não atrasam o cliente). Os testes desligam para checar na hora.
ASYNC = True


# ---------------------------------------------------------------------------
# 1. Terminal
# ---------------------------------------------------------------------------

def out(message):
    print(f"[LOGS {datetime.now():%H:%M:%S}] {message}", flush=True)


_last_not_configured = 0.0
_skipped_not_configured = 0


def _warn_not_configured(title):
    """Sem canal configurado: avisa no terminal no máximo a cada 5 minutos (com a contagem)."""
    global _last_not_configured, _skipped_not_configured
    _skipped_not_configured += 1
    now = time.monotonic()
    if now - _last_not_configured >= 300 or _last_not_configured == 0.0:
        extra = f" ({_skipped_not_configured} log(s) descartado(s) até agora)" if _skipped_not_configured > 1 else ""
        out(f"✗ '{title}' não enviado: nenhum canal de logs configurado{extra}. "
            "Configure em /admin → ⚙️ Configurações → 📢 Definir canal de logs, ou LOG_CHANNEL_ID no .env.")
        _last_not_configured = now


# ---------------------------------------------------------------------------
# 2. Canal
# ---------------------------------------------------------------------------

def channel_id():
    configured = str(storage.get_settings().get("log_channel_id") or "").strip()
    return configured or config.LOG_CHANNEL_ID


def channel_source():
    if str(storage.get_settings().get("log_channel_id") or "").strip():
        return "painel"
    return ".env" if config.LOG_CHANNEL_ID else None


def parse_channel_input(text, message=None):
    """Lista de IDs possíveis a partir de: post encaminhado, ID, @usuario ou link t.me."""
    message = message or {}
    origin = message.get("forward_origin") or {}
    forwarded = origin.get("chat") or message.get("forward_from_chat") or {}
    if forwarded.get("id"):
        return [str(forwarded["id"])]
    value = (text or "").strip().split("?")[0].rstrip("/")
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    if value.lower().startswith(("t.me/", "telegram.me/")):
        path = value.split("/", 1)[1]
        if path.startswith(("+", "joinchat")):
            raise ValueError(
                "Link de convite não identifica o canal. Encaminhe um post do canal para cá ou envie o ID (-100…)."
            )
        value = "-100" + path.split("/")[1] if path.startswith("c/") else "@" + path.split("/")[0]
    if value.startswith("@") and len(value) > 5:
        return [value]
    digits = value.lstrip("-")
    if not digits.isdigit():
        raise ValueError(
            "Não reconheci o canal. Encaminhe um post do canal para cá, "
            "ou envie o ID (ex.: -1001234567890) ou o @ dele."
        )
    if value.startswith("-100"):
        return [value]
    return [f"-100{digits}", f"-{digits}"]  # ID copiado sem o -100 (comum no Telegram Web)


def _error_code(exc):
    code = getattr(exc, "code", None)
    if code is None:
        match = re.search(r"HTTP (\d{3})", str(exc))
        code = int(match.group(1)) if match else None
    return code


def _error_text(exc):
    return str(getattr(exc, "description", None) or exc)


def explain_error(exc):
    """Explicação em português + o que fazer, a partir de um erro do Telegram."""
    text = _error_text(exc).lower()
    code = _error_code(exc)
    if isinstance(exc, (urllib.error.URLError,) + telegram_api.NETWORK_ERRORS):
        return "sem conexão com o Telegram (internet/rede). O bot tenta de novo sozinho."
    if "chat not found" in text:
        return ("canal não encontrado. O ID está errado ou o bot nunca foi adicionado ao canal. "
                "Encaminhe um post do canal para o bot para pegar o ID certo.")
    if "bot is not a member" in text or "bot was kicked" in text:
        return "o bot não está no canal. Adicione-o como administrador do canal."
    if "not enough rights" in text or "need administrator rights" in text or "have no rights" in text:
        return "o bot está no canal, mas sem permissão. Dê a ele a permissão 'Publicar mensagens'."
    if "can't parse entities" in text:
        return "formatação HTML inválida na mensagem (o bot reenvia em texto simples)."
    if "too many requests" in text or code == 429:
        return "limite de mensagens do Telegram. O bot espera e tenta de novo."
    if "upgraded to a supergroup" in text:
        return "o grupo virou supergrupo e mudou de ID (o bot atualiza sozinho)."
    if "unauthorized" in text or code == 401:
        return "BOT_TOKEN inválido."
    if code == 403 or "forbidden" in text:
        return "o Telegram bloqueou o envio. O bot precisa ser administrador do canal com 'Publicar mensagens'."
    if code == 400:
        return f"pedido recusado pelo Telegram ({_error_text(exc)})."
    return f"erro inesperado: {type(exc).__name__}: {_error_text(exc)}"


def _retryable(exc):
    if isinstance(exc, (urllib.error.URLError,) + telegram_api.NETWORK_ERRORS):
        return True
    code = _error_code(exc)
    return code == 429 or (code is not None and code >= 500)


# ---------------------------------------------------------------------------
# 3. Diagnóstico
# ---------------------------------------------------------------------------

def diagnose(chat=None):
    """Confere no Telegram se o bot consegue publicar no canal. Nunca levanta erro.

    Retorna {"ok", "chat_id", "title", "type", "problems": [...]}; chat_id já vem resolvido (numérico).
    """
    chat = chat or channel_id()
    report = {"ok": False, "chat_id": chat, "title": None, "type": None, "problems": []}
    if not chat:
        report["problems"].append("nenhum canal configurado")
        return report
    try:
        info = telegram_api.call("getChat", {"chat_id": chat}) or {}
    except Exception as exc:
        report["problems"].append(explain_error(exc))
        report["error"] = _error_text(exc)
        return report
    report.update(chat_id=str(info.get("id", chat)), title=info.get("title"), type=info.get("type"))
    try:
        bot_id = telegram_api.bot_info().get("id")
        member = telegram_api.call("getChatMember", {"chat_id": report["chat_id"], "user_id": bot_id}) or {}
    except Exception as exc:
        report["problems"].append("não consegui ver as permissões do bot: " + explain_error(exc))
        report["error"] = _error_text(exc)
        return report
    status = member.get("status")
    if status in {"left", "kicked"}:
        report["problems"].append("o bot não está no canal (foi removido ou nunca entrou). Adicione-o como administrador.")
    elif report["type"] == "channel":
        if status != "administrator" and status != "creator":
            report["problems"].append(
                f"o bot está no canal como '{status}', mas precisa ser ADMINISTRADOR para publicar."
            )
        elif status == "administrator" and member.get("can_post_messages") is False:
            report["problems"].append("o bot é administrador, mas a permissão 'Publicar mensagens' está desligada.")
    elif status == "restricted" and member.get("can_send_messages") is False:
        report["problems"].append("o bot está restrito no grupo e não pode enviar mensagens.")
    report["ok"] = not report["problems"]
    return report


def describe(report):
    name = f"\"{report['title']}\" " if report.get("title") else ""
    return f"{name}({report.get('chat_id')})"


# ---------------------------------------------------------------------------
# 4. Envio (fila + tentativas)
# ---------------------------------------------------------------------------

_queue = queue.Queue()
_worker = None
_worker_lock = threading.Lock()


def _strip_html(text):
    return unescape(re.sub(r"<[^>]+>", "", text))


def _post(chat, text, html=True):
    data = {"chat_id": chat, "text": text, "link_preview_options": '{"is_disabled": true}'}
    if html:
        data["parse_mode"] = "HTML"
    return telegram_api.call("sendMessage", data, timeout=20)


def deliver(text, title="log", chat=None, raise_errors=False):
    """Envia agora (na thread atual), com tentativas. Retorna True/False; raise_errors=True para o teste do admin."""
    chat = chat or channel_id()
    if not chat:
        _warn_not_configured(title)
        return False
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT - 20] + "\n… (cortado)"
    html = True
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            _post(chat, text, html)
            out(f"✓ '{title}' enviado ao canal {chat}"
                + ("" if attempt == 1 else f" (na tentativa {attempt})")
                + ("" if html else " em texto simples"))
            return True
        except Exception as exc:
            last_error = exc
            reason = explain_error(exc)
            description = _error_text(exc).lower()
            if html and "can't parse entities" in description:
                out(f"! '{title}': {reason} Detalhe: {_error_text(exc)}")
                html, text = False, _strip_html(text)
                continue
            new_id = getattr(exc, "parameters", {}).get("migrate_to_chat_id") if hasattr(exc, "parameters") else None
            if new_id:
                out(f"! '{title}': {reason} Novo ID: {new_id}. Salvando no painel.")
                chat = str(new_id)
                storage.update_settings(log_channel_id=chat)
                continue
            if _retryable(exc) and attempt < MAX_ATTEMPTS:
                wait = getattr(exc, "retry_after", None) or min(2 ** attempt, 30)
                out(f"! '{title}' falhou (tentativa {attempt}/{MAX_ATTEMPTS}): {reason} Tentando de novo em {wait}s.")
                time.sleep(wait)
                continue
            break
    out(f"✗ '{title}' NÃO enviado ao canal {chat}: {explain_error(last_error)} "
        f"| Erro do Telegram: {_error_text(last_error)}")
    if raise_errors:
        raise last_error
    return False


def _run_worker():
    while True:
        text, title = _queue.get()
        try:
            deliver(text, title)
        except Exception as exc:  # deliver já trata tudo; isto é só uma rede de segurança
            out(f"✗ '{title}' perdido por erro interno: {type(exc).__name__}: {exc}")
        finally:
            _queue.task_done()


def _enqueue(text, title):
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_worker, name="logs-canal", daemon=True)
            _worker.start()
    _queue.put((text, title))


def flush(timeout=10):
    """Espera a fila esvaziar (usado ao encerrar o bot)."""
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.1)
    pending = _queue.unfinished_tasks
    if pending:
        out(f"✗ {pending} log(s) ainda na fila ao encerrar; não foram enviados.")


def _now():
    return datetime.now().strftime("%d/%m/%Y às %H:%M:%S")


def send(icon, title, fields):
    """Monta o log e coloca na fila. fields: lista de (rótulo, valor); valores vazios são omitidos."""
    if not channel_id():
        _warn_not_configured(title)
        return False
    lines = [f"{icon} <b>{title}</b>", LINE]
    lines += [f"{label}: {value}" for label, value in fields if value not in (None, "")]
    lines += [LINE, f"🕒 {_now()}"]
    text = "\n".join(lines)
    if ASYNC:
        _enqueue(text, title)
        return True
    return deliver(text, title)


def _safe(func):
    """Erro ao montar um log nunca derruba o fluxo; o motivo vai para o terminal."""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            out(f"✗ log '{func.__name__}' não montado: {type(exc).__name__}: {exc}")
            return False
    wrapper.__name__ = func.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Inicialização e conexão do canal (painel admin)
# ---------------------------------------------------------------------------

def startup_report():
    """Diagnóstico impresso ao iniciar o bot."""
    chat = channel_id()
    if not chat:
        out("Canal de logs: não configurado (os eventos não serão publicados).")
        return None
    report = diagnose(chat)
    origin = channel_source()
    if report["ok"]:
        out(f"Canal de logs OK: {describe(report)} [definido no {origin}].")
    else:
        out(f"Canal de logs {describe(report)} [definido no {origin}] COM PROBLEMA:")
        for problem in report["problems"]:
            out(f"   - {problem}")
        if report.get("error"):
            out(f"   Erro do Telegram: {report['error']}")
    return report


def connect(candidates, admin_id):
    """Tenta cada ID possível: diagnostica, manda o teste e salva o primeiro que funcionar.

    Retorna (report_ok, None) ou (None, [(candidato, problemas), ...]).
    """
    failures = []
    for candidate in candidates:
        report = diagnose(candidate)
        out(f"Conectando canal {candidate}: " + ("diagnóstico OK" if report["ok"] else "; ".join(report["problems"])))
        if not report["ok"]:
            failures.append((candidate, report["problems"]))
            continue
        try:
            test_message(report["chat_id"], admin_id)
        except Exception as exc:
            failures.append((candidate, [explain_error(exc), f"Erro do Telegram: {_error_text(exc)}"]))
            continue
        storage.update_settings(log_channel_id=report["chat_id"])
        out(f"Canal de logs conectado: {describe(report)}.")
        return report, None
    return None, failures


def test_message(chat, admin_id):
    """Mensagem de teste, enviada na hora (fora da fila). Levanta o erro se não conseguir."""
    return deliver(
        "✅ <b>CANAL DE LOGS CONECTADO</b>\n" + LINE + "\n"
        f"🛠 Configurado por: {who(admin_id)} · <code>{admin_id}</code>\n"
        "A partir de agora, compras, usuários novos, Pix e ações do admin aparecem aqui.\n"
        + LINE + f"\n🕒 {_now()}",
        title="TESTE DO CANAL", chat=chat, raise_errors=True,
    )


# ---------------------------------------------------------------------------
# 5. Eventos
# ---------------------------------------------------------------------------

def money(value):
    return validators.money(Decimal(str(value)))


def who(user_id, user=None):
    """Nome clicável + @username de um usuário."""
    user = user or storage.get_user(user_id) or {}
    name = escape(user.get("first_name") or "Sem nome")
    username = f" (@{escape(user['username'])})" if user.get("username") else ""
    return f'<a href="tg://user?id={int(user_id)}">{name}</a>{username}'


def _person(user_id, user=None):
    return f"{who(user_id, user)} · <code>{user_id}</code>"


@_safe
def new_user(user_id):
    user = storage.get_user(user_id) or {}
    referrer_id = user.get("referred_by")
    return send("👋", "NOVO USUÁRIO", [
        ("👤 Usuário", who(user_id, user)),
        ("🆔 ID", f"<code>{user_id}</code>"),
        ("🎁 Bônus de boas-vindas", money(user["welcome_bonus"]) if user.get("welcome_bonus") else "nenhum"),
        ("💸 Saldo atual", f"<b>{money(user.get('balance', '0'))}</b>"),
        ("🤝 Indicado por", _person(referrer_id) if referrer_id else "ninguém (entrou direto)"),
        ("📈 Total de usuários", f"<b>{storage.count_users()}</b>"),
    ])


@_safe
def referral_bonus(referrer_id, referred_id, bonus, when):
    referrer = storage.get_user(referrer_id) or {}
    return send("🤝", "BÔNUS DE INDICAÇÃO PAGO", [
        ("👤 Quem indicou", _person(referrer_id, referrer)),
        ("🙋 Indicado", _person(referred_id)),
        ("🎁 Bônus", f"<b>{money(bonus)}</b> ({when})"),
        ("💸 Saldo de quem indicou", f"<b>{money(referrer.get('balance', '0'))}</b>"),
        ("👥 Indicados dele", str(storage.referral_stats(referrer_id)["count"])),
    ])


def _pix_amount(deposit):
    return Decimal(deposit["amount_cents"]) / 100


@_safe
def pix_created(deposit):
    user = storage.get_user(deposit["telegram_id"]) or {}
    return send("💠", "PIX GERADO", [
        ("👤 Usuário", _person(deposit["telegram_id"], user)),
        ("💳 Valor", f"<b>{money(_pix_amount(deposit))}</b>"),
        ("💸 Saldo atual", money(user.get("balance", "0"))),
        ("🧾 Transação", f"<code>{escape(str(deposit.get('transaction_id') or '—'))}</code>"),
        ("🔖 Referência", f"<code>{escape(deposit['external_ref'])}</code>"),
        ("⏳ Expira em", escape(str(deposit.get("expires_at") or "—")).replace("T", " ")),
        ("📌 Status", "aguardando pagamento"),
    ])


@_safe
def pix_creation_failed(deposit, reason):
    return send("⚠️", "FALHA AO GERAR PIX", [
        ("👤 Usuário", _person(deposit["telegram_id"])),
        ("💳 Valor", money(_pix_amount(deposit))),
        ("🔖 Referência", f"<code>{escape(deposit['external_ref'])}</code>"),
        ("❗ Motivo", escape(str(reason))[:300]),
    ])


@_safe
def pix_confirmed(deposit):
    user = storage.get_user(deposit["telegram_id"]) or {}
    amount = _pix_amount(deposit)
    after = Decimal(user.get("balance", "0"))
    reward = deposit.get("referral_reward") or {}
    return send("✅", "DEPÓSITO CONFIRMADO", [
        ("👤 Usuário", _person(deposit["telegram_id"], user)),
        ("💰 Valor recebido", f"<b>{money(amount)}</b>"),
        ("💸 Saldo", f"{money(after - amount)} → <b>{money(after)}</b>"),
        ("🧾 Transação", f"<code>{escape(str(deposit.get('transaction_id') or '—'))}</code>"),
        ("🔢 Depósitos do usuário", str(storage.deposit_count(deposit["telegram_id"]))),
        ("🤝 Bônus de indicação", f"{money(reward['bonus'])} para <code>{reward['referrer_id']}</code>"
         if reward else None),
    ])


@_safe
def pix_failed(deposit):
    return send("❌", "PIX NÃO PAGO / EXPIRADO", [
        ("👤 Usuário", _person(deposit["telegram_id"])),
        ("💳 Valor", money(_pix_amount(deposit))),
        ("🧾 Transação", f"<code>{escape(str(deposit.get('transaction_id') or '—'))}</code>"),
    ])


@_safe
def purchase(user_id, result):
    user = storage.get_user(user_id) or {}
    product = result["product"]
    price = Decimal(product["price"])
    after = Decimal(user.get("balance", "0"))
    stock = (storage.get_product(product["id"], include_inactive=True) or {}).get("stock", 0)
    info = result.get("access_info") or {}
    login_info = None
    if info:
        total = money(info["valor_total"]) if info.get("valor_total") is not None else "—"
        login_info = f"#{result.get('access_id')} · {info.get('pedidos', '—')} pedidos · {total}"
    return send("🛒", "NOVA COMPRA", [
        ("👤 Cliente", _person(user_id, user)),
        ("📦 Produto", f"<b>{escape(product['name'])}</b> (#{product['id']})"),
        ("🔐 Login vendido", login_info),
        ("💵 Preço", f"<b>{money(price)}</b>"),
        ("💸 Saldo", f"{money(after + price)} → <b>{money(after)}</b>"),
        ("🧾 Pedido", f"#{result['order_id']}"),
        ("📊 Estoque restante", f"<b>{stock}</b>" + (" ⚠️ ESGOTOU" if not stock else "")),
        ("🛍 Compras do cliente", str(storage.purchase_stats(user_id)["count"])),
    ])


@_safe
def delivery_failed(user_id, order_id, product_name, reason):
    return send("🚨", "FALHA NA ENTREGA", [
        ("👤 Cliente", _person(user_id)),
        ("📦 Produto", escape(product_name)),
        ("🧾 Pedido", f"#{order_id} (pago, entrega pendente)"),
        ("❗ Motivo", escape(str(reason))[:300]),
        ("🛠 Como resolver", "Painel admin → 📨 Reenviar entrega"),
    ])


@_safe
def delivery_resent(admin_id, order):
    return send("📨", "ENTREGA REENVIADA", [
        ("🛠 Admin", _person(admin_id)),
        ("👤 Cliente", _person(order["telegram_id"])),
        ("📦 Produto", escape(order["name"])),
        ("🧾 Pedido", f"#{order['id']}"),
    ])


@_safe
def admin_balance(admin_id, target_id, amount):
    user = storage.get_user(target_id) or {}
    after = Decimal(user.get("balance", "0"))
    return send("💰", "SALDO ADICIONADO PELO ADMIN", [
        ("🛠 Admin", _person(admin_id)),
        ("👤 Usuário", _person(target_id, user)),
        ("➕ Valor", f"<b>{money(amount)}</b>"),
        ("💸 Saldo", f"{money(after - Decimal(amount))} → <b>{money(after)}</b>"),
    ])


@_safe
def stock_changed(admin_id, product_id, change, detail=None):
    """change > 0 = acessos adicionados; change < 0 = removidos."""
    product = storage.get_product(product_id, include_inactive=True) or {"name": "Produto removido", "stock": 0}
    added = change > 0
    return send("📥" if added else "📤", "ESTOQUE ADICIONADO" if added else "ESTOQUE REMOVIDO", [
        ("🛠 Admin", _person(admin_id)),
        ("📦 Produto", f"<b>{escape(product['name'])}</b> (#{product_id})"),
        ("🔢 Quantidade", f"<b>{'+' if added else '−'}{abs(change)}</b>"),
        ("📊 Estoque agora", f"<b>{product['stock']}</b>"),
        ("📝 Detalhe", detail),
    ])


@_safe
def product_event(admin_id, title, product_id, detail=None, name=None):
    product = storage.get_product(product_id, include_inactive=True)
    label = escape(product["name"] if product else (name or "Produto"))
    fields = [("🛠 Admin", _person(admin_id)), ("📦 Produto", f"<b>{label}</b> (#{product_id})")]
    if product:
        fields += [
            ("💵 Preço", money(product["price"])),
            ("📊 Estoque", str(product["stock"])),
            ("📌 Status", "à venda" if product["active"] else "pausado"),
        ]
    fields.append(("📝 Detalhe", detail))
    return send("🗂", title, fields)


@_safe
def broadcast_finished(admin_id, job, kind=None):
    return send("📢", "TRANSMISSÃO INTERROMPIDA" if job.get("stop") else "TRANSMISSÃO CONCLUÍDA", [
        ("🛠 Admin", _person(admin_id)),
        ("📨 Mensagem", escape(kind or "—")),
        ("👥 Destino", str(job["total"])),
        ("✅ Enviadas", f"<b>{job['sent']}</b>"),
        ("🚫 Bloquearam o bot", str(job["blocked"])),
        ("⚠️ Falhas", str(job["failed"])),
        ("⏱ Tempo", f"{int(job.get('elapsed', 0))}s"),
    ])


@_safe
def settings_changed(admin_id, detail):
    settings = storage.get_settings()
    rule = " (no 1º depósito)" if settings["referral_requires_deposit"] else " (ao entrar)"
    return send("⚙️", "CONFIGURAÇÃO ALTERADA", [
        ("🛠 Admin", _person(admin_id)),
        ("📝 Mudança", escape(_strip_html(detail))),
        ("🎁 Boas-vindas", money(settings["welcome_bonus"])),
        ("🤝 Indicação", money(settings["referral_bonus"]) + rule),
    ])
