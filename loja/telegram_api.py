"""Chamadas HTTP à Bot API do Telegram."""
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import config


API = f"https://api.telegram.org/bot{config.BOT_TOKEN}/"


def call(method, data=None, timeout=45):
    payload = urllib.parse.urlencode(data or {}).encode("utf-8")
    request = urllib.request.Request(API + method, data=payload if data is not None else None)
    retryable = method in {"getUpdates", "editMessageText"}
    attempts = 3 if retryable else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
            if attempt + 1 == attempts:
                raise
            time.sleep(attempt + 1)
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Erro na API do Telegram"))
    return result.get("result")


def get_updates(offset=None, timeout=30):
    args = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
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
    request = urllib.request.Request(
        API + "sendPhoto",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {body}") from exc
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Erro ao enviar o QR Pix pelo Telegram."))
    return result.get("result")


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
