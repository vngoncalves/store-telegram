"""Painel ativo de cada chat: uma mensagem que é editada em vez de enviar novas."""
import json

from . import storage, telegram_api


def send_new_panel(chat_id, text, reply_markup=None, replace_message_id=None, preview_url=None):
    """Envia um novo painel de texto, torna-o o painel ativo e remove o anterior.

    preview_url: imagem mostrada grande abaixo do texto (prévia de link), como no menu inicial.
    """
    result = telegram_api.send_message(chat_id, text, reply_markup, preview_url=preview_url)
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


def send_photo_panel(chat_id, photo_url, caption, reply_markup=None, replace_message_id=None):
    """Envia uma foto (por link) com legenda e botões, torna-a o painel ativo e remove o painel anterior.

    Levanta o erro do Telegram se a imagem não puder ser usada (link quebrado, não é imagem...).
    """
    result = _with_cached_photo(
        photo_url, lambda ref: telegram_api.send_photo_url(chat_id, ref, caption, reply_markup)
    )
    if not isinstance(result, dict) or result.get("message_id") is None:
        raise RuntimeError("Telegram não retornou o ID da foto enviada.")
    new_message_id = result["message_id"]
    remember_active_menu(chat_id, new_message_id, media="photo")
    if replace_message_id is not None and replace_message_id != new_message_id:
        try:
            telegram_api.delete_message(chat_id, replace_message_id)
        except Exception as exc:
            print("Não foi possível remover o painel anterior:", exc)
    return new_message_id


# Link da imagem → file_id do Telegram. Depois do primeiro envio, o Telegram não precisa
# baixar a imagem do link de novo: o menu e os produtos com foto abrem mais rápido.
PHOTO_IDS = {}


def _remember_photo(photo_url, result):
    photos = result.get("photo") if isinstance(result, dict) else None
    if isinstance(photos, list) and photos and photos[-1].get("file_id"):
        PHOTO_IDS[photo_url] = photos[-1]["file_id"]


def _with_cached_photo(photo_url, send):
    """Envia usando o file_id guardado; se ele falhar, tenta pelo link e guarda o novo file_id."""
    cached = PHOTO_IDS.get(photo_url)
    if cached:
        try:
            result = send(cached)
            return result
        except RuntimeError as exc:
            if telegram_api.is_not_modified(exc):
                raise
            PHOTO_IDS.pop(photo_url, None)
    result = send(photo_url)
    _remember_photo(photo_url, result)
    return result


def edit_photo(chat_id, message_id, photo_url, caption, reply_markup=None):
    """Troca a foto e a legenda de um painel que já é foto (sem enviar mensagem nova)."""
    def send(ref):
        data = {
            "chat_id": chat_id, "message_id": message_id,
            "media": json.dumps({"type": "photo", "media": ref, "caption": caption, "parse_mode": "HTML"},
                                ensure_ascii=False),
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        return telegram_api.call("editMessageMedia", data, timeout=30)

    try:
        _with_cached_photo(photo_url, send)
    except RuntimeError as exc:
        if not telegram_api.is_not_modified(exc):
            raise
    remember_active_menu(chat_id, message_id, media="photo")
    return message_id


def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode="HTML", keep_photo=False,
                 preview_url=None):
    """Edita o painel. Um painel com foto (QR Pix) é substituído por texto, salvo keep_photo."""
    is_photo = active_menu_is_photo(chat_id, message_id)
    if is_photo and not keep_photo:
        return send_new_panel(chat_id, text, reply_markup, replace_message_id=message_id, preview_url=preview_url)
    data = {"chat_id": chat_id, "message_id": message_id}
    data["caption" if is_photo else "text"] = text
    if not is_photo:
        data["link_preview_options"] = telegram_api.link_preview(preview_url)
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


def edit_active_panel(chat_id, text, reply_markup, keep_photo=False, preview_url=None):
    """Atualiza o painel ativo do chat; se ele não existir mais, envia um novo."""
    message_id = active_menu_message_id(chat_id)
    if message_id is None:
        return send_new_panel(chat_id, text, reply_markup, preview_url=preview_url)
    try:
        return edit_message(chat_id, message_id, text, reply_markup, keep_photo=keep_photo, preview_url=preview_url)
    except RuntimeError as exc:
        if telegram_api.message_unavailable(exc):
            return send_new_panel(chat_id, text, reply_markup, preview_url=preview_url)
        raise


ACTIVE_MENUS = {}
ACTIVE_MEDIA = {}  # cache do tipo do painel ("text"/"photo"); evita ler o arquivo a cada edição


def active_menu_is_photo(chat_id, message_id=None):
    if message_id is None:
        message_id = active_menu_message_id(chat_id)
    if message_id is None:
        return False
    if ACTIVE_MENUS.get(chat_id) == message_id and chat_id in ACTIVE_MEDIA:
        return ACTIVE_MEDIA[chat_id] == "photo"
    data = storage._read()
    is_active = data["active_menus"].get(str(chat_id)) == message_id
    media = data["active_menu_media"].get(str(chat_id))
    if is_active and media:
        ACTIVE_MENUS[chat_id] = message_id
        ACTIVE_MEDIA[chat_id] = media
    return is_active and media == "photo"


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
    unchanged = ACTIVE_MENUS.get(chat_id) == message_id and (media is None or ACTIVE_MEDIA.get(chat_id) == media)
    ACTIVE_MENUS[chat_id] = message_id
    if media is not None:
        ACTIVE_MEDIA[chat_id] = media
    if unchanged:
        return  # o mesmo painel de antes: não precisa regravar o arquivo
    with storage._LOCK:
        data = storage._read()
        key = str(chat_id)
        if data["active_menus"].get(key) == int(message_id) and (
            media is None or data["active_menu_media"].get(key) == media
        ):
            return
        data["active_menus"][key] = int(message_id)
        if media is not None:
            data["active_menu_media"][key] = media
        storage._write(data)
