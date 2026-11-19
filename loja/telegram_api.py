"""Chamadas HTTP à Bot API do Telegram.

As requisições reaproveitam uma conexão HTTPS aberta por thread (keep-alive), em vez de abrir
uma nova (DNS + TCP + TLS) a cada chamada. Isso deixa cada clique bem mais rápido.
"""
import http.client
import json
import socket
import ssl
import threading
import time
import urllib.parse
import uuid

from . import config


_HOST = "api.telegram.org"
_BASE_PATH = f"/bot{config.BOT_TOKEN}/"
_SSL = ssl.create_default_context()
_local = threading.local()

# Erros de rede (não são respostas da API): quem chama pode tentar de novo.
NETWORK_ERRORS = (TimeoutError, socket.timeout, ConnectionError, OSError, http.client.HTTPException)


def _connection(timeout):
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = http.client.HTTPSConnection(_HOST, timeout=timeout, context=_SSL)
        _local.conn = conn
    conn.timeout = timeout
    if conn.sock is not None:
        conn.sock.settimeout(timeout)
    return conn


def _drop_connection():
    conn = getattr(_local, "conn", None)
    _local.conn = None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _post(method, body, content_type, timeout):
    """POST na Bot API mantendo a conexão aberta. Retorna (status_http, corpo_em_bytes).

    Se o Telegram tiver fechado uma conexão ociosa, reabre e tenta mais uma vez.
    """
    for reuse_attempt in range(2):
        conn = _connection(timeout)
        reused = conn.sock is not None
        try:
            conn.request("POST", _BASE_PATH + method, body=body, headers={"Content-Type": content_type})
            response = conn.getresponse()
            raw = response.read()
        except (http.client.RemoteDisconnected, http.client.CannotSendRequest, http.client.BadStatusLine,
                BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            _drop_connection()
            if reused and reuse_attempt == 0:
                continue
            raise ConnectionError(f"Conexão com o Telegram caiu: {type(exc).__name__}") from exc
        except BaseException:
            _drop_connection()
            raise
        if response.will_close:
            _drop_connection()
        return response.status, raw
    raise ConnectionError("Conexão com o Telegram caiu.")


def _parse(status, raw):
    text = raw.decode("utf-8", errors="replace")
    if status != 200:
        raise _api_error(status, text)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise RuntimeError(f"Resposta inválida do Telegram: {text[:200]}") from exc


class TelegramAPIError(RuntimeError):
    """Erro devolvido pela Bot API, com código, descrição e parâmetros (retry_after, migrate_to_chat_id).

    str(erro) continua no formato antigo ("Telegram HTTP 403: {...}") para quem compara texto.
    """

    def __init__(self, message, code=None, description="", parameters=None):
        super().__init__(message)
        self.code = code
        self.description = description or message
        self.parameters = parameters or {}

    @property
    def retry_after(self):
        return self.parameters.get("retry_after")


def _api_error(http_code, body):
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {}
    return TelegramAPIError(
        f"Telegram HTTP {http_code}: {body}",
        code=payload.get("error_code", http_code),
        description=payload.get("description", body),
        parameters=payload.get("parameters"),
    )


def call(method, data=None, timeout=45):
    payload = urllib.parse.urlencode(data or {}).encode("utf-8")
    retryable = method in {"getUpdates", "editMessageText"}
    attempts = 3 if retryable else 1
    for attempt in range(attempts):
        try:
            status, raw = _post(method, payload, "application/x-www-form-urlencoded", timeout)
            break
        except NETWORK_ERRORS:
            if attempt + 1 == attempts:
                raise
            time.sleep(attempt + 1)
    result = _parse(status, raw)
    if not result.get("ok"):
        description = result.get("description", "Erro na API do Telegram")
        raise TelegramAPIError(description, result.get("error_code"), description, result.get("parameters"))
    return result.get("result")


_BOT_INFO = None


def bot_info():
    """Resultado do getMe (id, username...), lido uma vez e guardado."""
    global _BOT_INFO
    if not _BOT_INFO:
        _BOT_INFO = call("getMe") or {}
    return _BOT_INFO


def bot_username():
    """@ do bot (sem o @). Usado no link de indicação."""
    return bot_info().get("username")


def get_updates(offset=None, timeout=30):
    args = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query", "inline_query"])}
    if offset is not None:
        args["offset"] = offset
    return call("getUpdates", args, timeout=timeout + 10)


def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    data = {"chat_id": chat_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return call("sendMessage", data)


def send_photo_url(chat_id, photo_url, caption, reply_markup=None, parse_mode="HTML"):
    """Foto por link: o próprio Telegram baixa a imagem do endereço informado."""
    data = {"chat_id": chat_id, "photo": photo_url, "caption": caption, "parse_mode": parse_mode}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return call("sendPhoto", data, timeout=30)


def send_photo(chat_id, photo_data, caption, reply_markup=None, parse_mode="HTML"):
    boundary = "----TelegramBot" + uuid.uuid4().hex
    fields = {
        "chat_id": str(chat_id),
        "caption": caption,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    parts = []
    for name, value in fields.items():
        parts.extend([
            f"--{boundary}\r\n".encode("ascii"),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    parts.extend([
        f"--{boundary}\r\n".encode("ascii"),
        b'Content-Disposition: form-data; name="photo"; filename="pix-qr.png"\r\n',
        b"Content-Type: image/png\r\n\r\n",
        photo_data,
        b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ])
    status, raw = _post("sendPhoto", b"".join(parts), f"multipart/form-data; boundary={boundary}", 45)
    result = _parse(status, raw)
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Erro ao enviar o QR Pix pelo Telegram."))
    return result.get("result")


def answer_inline_query(inline_query_id, results, next_offset=""):
    return call("answerInlineQuery", {
        "inline_query_id": inline_query_id,
        "results": json.dumps(results, ensure_ascii=False),
        "cache_time": 0,  # estoque e preço mudam em tempo real
        "is_personal": "true",
        "next_offset": next_offset,
    })


def delete_message(chat_id, message_id):
    return call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def is_not_modified(exc):
    return "message is not modified" in str(exc).lower()


def message_unavailable(exc):
    message = str(exc).lower()
    return any(
        fragment in message
        for fragment in ("message to edit not found", "message can't be edited", "message_id_invalid")
    )


def answer_callback(callback_id, text=None, show_alert=False):
    data = {"callback_query_id": callback_id, "show_alert": str(show_alert).lower()}
    if text:
        data["text"] = text
    try:
        return call("answerCallbackQuery", data)
    except RuntimeError as exc:
        message = str(exc).lower()
        if "query is too old" in message or "query id is invalid" in message:
            return False
        raise
