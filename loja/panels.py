"""Painel ativo de cada chat: uma mensagem que é editada em vez de enviar novas."""
import json

from . import storage, telegram_api


def send_new_panel(chat_id, text, reply_markup=None, replace_message_id=None):
    """Envia um novo painel de texto, torna-o o painel ativo e remove o anterior."""
    result = telegram_api.send_message(chat_id, text, reply_markup)
    if not isinstance(result, dict) or result.get("message_id") is None:
        raise RuntimeError("Telegram não retornou o ID do novo painel.")
    new_message_id = result["message_id"]
    remember_active_menu(chat_id, new_message_id, media="text")
    if replace_message_id is not None and replace_message_id != new_message_id:
        try:
            telegram_api.delete_message(chat_id, replace_message_id)
        except Exception as exc:
            print("Não foi possível remover o painel anterior:", exc)
    return new_message_id


def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode="HTML", keep_photo=False):
    """Edita o painel. Um painel com foto (QR Pix) é substituído por texto, salvo keep_photo."""
    is_photo = active_menu_is_photo(chat_id, message_id)
    if is_photo and not keep_photo:
        return send_new_panel(chat_id, text, reply_markup, replace_message_id=message_id)
    data = {"chat_id": chat_id, "message_id": message_id}
    data["caption" if is_photo else "text"] = text
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        telegram_api.call("editMessageCaption" if is_photo else "editMessageText", data)
    except RuntimeError as exc:
        if telegram_api.is_not_modified(exc):
            return message_id
        raise
    return message_id


def edit_active_panel(chat_id, text, reply_markup, keep_photo=False):
    """Atualiza o painel ativo do chat; se ele não existir mais, envia um novo."""
    message_id = active_menu_message_id(chat_id)
    if message_id is None:
        return send_new_panel(chat_id, text, reply_markup)
    try:
        return edit_message(chat_id, message_id, text, reply_markup, keep_photo=keep_photo)
    except RuntimeError as exc:
        if telegram_api.message_unavailable(exc):
            return send_new_panel(chat_id, text, reply_markup)
        raise


ACTIVE_MENUS = {}


def active_menu_is_photo(chat_id, message_id=None):
    if message_id is None:
        message_id = active_menu_message_id(chat_id)
    if message_id is None:
        return False
    data = storage._read()
    return (
        data["active_menus"].get(str(chat_id)) == message_id
        and data["active_menu_media"].get(str(chat_id)) == "photo"
    )


def active_menu_message_id(chat_id):
    message_id = ACTIVE_MENUS.get(chat_id)
    if message_id is None:
        message_id = storage._read()["active_menus"].get(str(chat_id))
        if type(message_id) is int and message_id > 0:
            ACTIVE_MENUS[chat_id] = message_id
        else:
            return None
    return message_id


def remember_active_menu(chat_id, message_id, media=None):
    ACTIVE_MENUS[chat_id] = message_id
    with storage._LOCK:
        data = storage._read()
        data["active_menus"][str(chat_id)] = int(message_id)
        if media is not None:
            data["active_menu_media"][str(chat_id)] = media
        storage._write(data)
