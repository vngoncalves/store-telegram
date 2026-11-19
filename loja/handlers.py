"""Fluxo do bot: comandos, botões, compras e recargas Pix."""
import json
import unicodedata
from datetime import datetime
from decimal import InvalidOperation
from html import escape

import qrcode.exceptions

from . import config, keyboards, logs, panels, payments, security, storage, telegram_api, validators, views


# Estado em memória: cobrança Pix exibida em cada chat e etapa atual de cada fluxo de texto.
ACTIVE_PIX_DEPOSITS = {}
STATES = {}


def is_admin(user_id):
    return user_id in config.ADMIN_IDS


def home(chat_id, user_id, message_id=None, new_message=False, note=None):
    text = views.home_text(user_id)
    if note:
        text = f"{note}\n\n{text}"
    markup = keyboards.main_menu(is_admin(user_id))
    if new_message:
        panels.send_new_panel(chat_id, text, markup)
        return
    if message_id is None:
        panels.edit_active_panel(chat_id, text, markup)
        return
    try:
        panels.edit_message(chat_id, message_id, text, markup)
    except RuntimeError as exc:
        if not telegram_api.message_unavailable(exc):
            raise
        panels.send_new_panel(chat_id, text, markup)


def admin_only(user_id):
    if user_id not in config.ADMIN_IDS:
        raise PermissionError("Acesso restrito ao administrador.")


def _fold(text):
    """Minúsculas e sem acento, para a busca achar 'Música' digitando 'musica'."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def catalog_products():
    return sorted(storage.list_products(), key=lambda item: (_fold(item["name"]), item["id"]))


def catalog_view(user_id, page=0):
    products = catalog_products()
    if not products:
        return "Ainda não há produtos disponíveis.", keyboards.back_menu()
    return views.catalog_text(user_id, products), keyboards.product_buttons(products, page)


def show_product(chat_id, product_id, message_id=None, index=0, note=None):
    """Tela do produto: um login do estoque por vez (⬅️ ➡️), com Comprar / Cancelar.

    Com imagem, a foto vai junto. Ao navegar entre logins, só a legenda da foto é trocada.
    """
    product = storage.get_product(product_id)
    if not product:
        raise ValueError("Produto indisponível.")
    accesses = storage.public_accesses(product_id)
    index = index % len(accesses) if accesses else 0
    access_id = accesses[index]["id"] if accesses else None
    markup = keyboards.product_logins(product_id, index, len(accesses), access_id)

    def text_for(caption):
        text = views.product_login_text(product, accesses, index, caption=caption)
        return f"{note}\n\n{text}" if note else text

    if product["image"]:
        if message_id is not None and panels.active_menu_is_photo(chat_id, message_id):
            try:  # já está mostrando a foto: troca só a legenda (navegação entre logins)
                panels.edit_message(chat_id, message_id, text_for(True), markup, keep_photo=True)
                return
            except Exception as exc:
                print(f"[PRODUTO] Não foi possível atualizar a legenda do produto #{product_id}: {exc}")
        replace_id = message_id if message_id is not None else panels.active_menu_message_id(chat_id)
        try:
            panels.send_photo_panel(chat_id, product["image"], text_for(True), markup, replace_id)
            return
        except Exception as exc:
            print(f"[PRODUTO] Imagem do produto #{product_id} não pôde ser enviada "
                  f"({product['image']}): {exc}. Mostrando só o texto.")
    if message_id is None:
        show_as_new_panel(chat_id, text_for(False), markup)
    else:
        panels.edit_message(chat_id, message_id, text_for(False), markup)


def show_as_new_panel(chat_id, text, markup):
    """Responde a uma mensagem do usuário com um painel novo, embaixo, e apaga o antigo."""
    panels.send_new_panel(chat_id, text, markup, replace_message_id=panels.active_menu_message_id(chat_id))


INLINE_PAGE_SIZE = 50  # limite da Bot API por resposta


def handle_inline_query(query):
    """Busca do botão 🔍 Buscar Login: o usuário digita após @bot e vê os produtos na hora."""
    if query.get("chat_type") not in (None, "sender"):
        # Busca feita em outro chat (grupo ou conversa com outra pessoa): o resultado iria parar lá.
        telegram_api.call("answerInlineQuery", {
            "inline_query_id": query["id"], "results": "[]", "cache_time": 0, "is_personal": "true",
            "button": json.dumps({"text": "🛍 Abrir a loja no privado", "start_parameter": "loja"}),
        })
        return
    words = _fold(query.get("query", "")).split()
    products = [
        product for product in catalog_products()
        if all(word in _fold(product["name"]) for word in words)
    ]
    try:
        offset = max(0, int(query.get("offset") or 0))
    except ValueError:
        offset = 0
    page = products[offset:offset + INLINE_PAGE_SIZE]
    results = []
    for product in page:
        stock = f"📊 Estoque: {product['stock']}" if product["stock"] else "⚠️ Esgotado"
        description = f"{validators.money(product['price'])} · {stock}"
        if product["description"]:
            description += f"\n{product['description'][:80]}"
        thumbnail = {"thumbnail_url": product["image"]} if product["image"] else {}
        results.append({
            **thumbnail,
            "type": "article",
            "id": f"p{product['id']}",
            "title": product["name"],
            "description": description,
            "input_message_content": {"message_text": f"/produto {product['id']}"},
        })
    if not results and offset == 0:
        results.append({
            "type": "article",
            "id": "none",
            "title": "Nenhum login encontrado",
            "description": "Toque para abrir a lista completa de produtos.",
            "input_message_content": {"message_text": "/produtos"},
        })
    more = offset + INLINE_PAGE_SIZE < len(products)
    telegram_api.answer_inline_query(query["id"], results, str(offset + INLINE_PAGE_SIZE) if more else "")


ADMIN_STEPS = {
    "product_name", "product_description", "product_price", "stock_add", "admin_pick",
    "edit_price", "edit_name", "edit_desc", "balance_user", "balance_amount", "resend_order",
    "cfg_welcome", "cfg_ref", "cfg_logs", "edit_image",
}


def admin_settings_panel(chat_id, user_id, note=None, message_id=None):
    STATES.pop(user_id, None)
    settings = storage.get_settings()
    _show(chat_id, message_id, views.admin_settings_text(settings, note), keyboards.admin_settings(settings))


def handle_admin_settings(chat_id, message_id, user_id, data):
    action = data.split(":", 2)[2] if data.count(":") >= 2 else "view"
    defaults = storage.DEFAULT_SETTINGS
    settings = storage.get_settings()
    note = None
    if action in {"welcome", "ref"}:
        key = "welcome_bonus" if action == "welcome" else "referral_bonus"
        label = "boas-vindas" if action == "welcome" else "indicação"
        STATES[user_id] = {"state": f"cfg_{action}", "data": {}}
        panels.edit_message(
            chat_id, message_id,
            f"✏️ <b>Bônus de {label}</b>\n\nValor atual: <b>{validators.money(settings[key])}</b>\n\n"
            "Envie o novo valor em reais. Exemplo: <code>10</code> ou <code>7,50</code>\n"
            "Envie <code>0</code> para desativar.",
            keyboards.admin_settings_back()
        )
        return
    if action == "welcome_off":
        storage.update_settings(welcome_bonus="0")
        note = "⏸ Bônus de boas-vindas desativado."
    elif action == "welcome_on":
        storage.update_settings(welcome_bonus=defaults["welcome_bonus"])
        note = "▶️ Bônus de boas-vindas ativado."
    elif action == "ref_off":
        storage.update_settings(referral_bonus="0")
        note = "⏸ Bônus de indicação desativado."
    elif action == "ref_on":
        storage.update_settings(referral_bonus=defaults["referral_bonus"])
        note = "▶️ Bônus de indicação ativado."
    elif action == "ref_mode":
        updated = storage.update_settings(referral_requires_deposit=not settings["referral_requires_deposit"])
        note = ("📌 Indicação paga no 1º depósito do indicado." if updated["referral_requires_deposit"]
                else "📌 Indicação paga assim que o indicado entra pelo link.")
    elif action == "logs":
        STATES[user_id] = {"state": "cfg_logs", "data": {}}
        panels.edit_message(
            chat_id, message_id,
            "📢 <b>Canal de logs</b>\n\n"
            "1️⃣ Adicione este bot como <b>administrador</b> do canal, com permissão para <b>publicar mensagens</b>.\n"
            "2️⃣ Depois, faça um destes:\n"
            " • <b>encaminhe aqui</b> qualquer post do canal, ou\n"
            " • envie o <b>ID</b> do canal (ex.: <code>-1001234567890</code>) ou o <b>@</b> dele, se for público.\n\n"
            "O bot manda uma mensagem de teste no canal para confirmar.",
            keyboards.admin_settings_back()
        )
        return
    elif action == "logs_test":
        report = logs.diagnose()
        if not logs.channel_id():
            note = "⚠️ Nenhum canal de logs configurado. Toque em 📢 Definir canal de logs."
        elif not report["ok"]:
            note = _channel_problems_text([(logs.describe(report), report["problems"])])
        else:
            try:
                logs.test_message(report["chat_id"], user_id)
                note = f"🧪 Teste enviado ao canal {escape(logs.describe(report))}."
            except Exception as exc:
                note = _channel_problems_text([(logs.describe(report), [logs.explain_error(exc)])])
        admin_settings_panel(chat_id, user_id, note, message_id)
        return
    elif action == "logs_off":
        storage.update_settings(log_channel_id="")
        logs.out(f"Canal de logs desligado pelo admin {user_id}.")
        note = "⏸ Canal de logs desligado." + (
            " (Ainda existe LOG_CHANNEL_ID no .env; apague a linha para desligar de vez.)"
            if config.LOG_CHANNEL_ID else ""
        )
    if note:
        logs.settings_changed(user_id, note)
    admin_settings_panel(chat_id, user_id, note, message_id)


def _channel_problems_text(failures):
    lines = ["⚠️ <b>O bot não consegue publicar no canal de logs</b>", ""]
    for candidate, problems in failures:
        lines.append(f"<code>{escape(str(candidate))}</code>")
        lines.extend(f" • {escape(problem, quote=False)}" for problem in problems)
    lines += [
        "",
        "<b>Como resolver:</b> no canal, abra <b>Administradores → Adicionar administrador</b>, "
        "escolha este bot e deixe <b>Publicar mensagens</b> ligado. Depois <b>encaminhe um post do canal</b> para cá.",
        "<i>Os detalhes também aparecem no terminal, nas linhas [LOGS].</i>",
    ]
    return "\n".join(lines)


def set_log_channel_from_message(chat_id, user_id, message, text):
    if text.strip() in {"0", "-"}:
        storage.update_settings(log_channel_id="")
        admin_settings_panel(chat_id, user_id, "⏸ Canal de logs desligado.")
        return
    candidates = logs.parse_channel_input(text, message)
    report, failures = logs.connect(candidates, user_id)
    if not report:
        panels.edit_active_panel(
            chat_id, _channel_problems_text(failures) + "\n\nEnvie de novo ou toque em Cancelar.",
            keyboards.admin_settings_back()
        )
        return
    note = (f"✅ Canal de logs conectado: <b>{escape(logs.describe(report))}</b>.\n"
            "Uma mensagem de teste foi publicada no canal.")
    logs.settings_changed(user_id, f"Canal de logs conectado: {logs.describe(report)}")
    admin_settings_panel(chat_id, user_id, note)


def admin_product_panel(chat_id, product_id, note=None, message_id=None):
    """Mostra o painel de um produto com o estoque lido agora do produtos.json."""
    product = storage.get_product(product_id, include_inactive=True)
    if not product:
        text, markup = "Produto não encontrado. Ele pode ter sido removido do produtos.json.", \
            keyboards.admin_products(storage.list_products(include_inactive=True))
    else:
        ids = [item["id"] for item in admin_all_products()]
        position = ids.index(product["id"]) if product["id"] in ids else 0
        prev_id = ids[position - 1] if position > 0 else None
        next_id = ids[position + 1] if position + 1 < len(ids) else None
        text = views.admin_product_text(product, note)
        markup = keyboards.admin_product(product, prev_id, next_id)
    if message_id is None:
        panels.edit_active_panel(chat_id, text, markup)
    else:
        panels.edit_message(chat_id, message_id, text, markup)


def _show(chat_id, message_id, text, markup):
    if message_id is None:
        panels.edit_active_panel(chat_id, text, markup)
    else:
        panels.edit_message(chat_id, message_id, text, markup)


def admin_all_products():
    return sorted(
        storage.list_products(include_inactive=True), key=lambda item: (_fold(item["name"]), item["id"])
    )


def admin_home(chat_id, user_id, message_id=None):
    STATES.pop(user_id, None)
    _show(chat_id, message_id, views.admin_home_text(admin_all_products()), keyboards.admin_menu())


def admin_picker_panel(chat_id, user_id, mode="products", page=0, message_id=None):
    """Lista para escolher o produto. Enquanto aberta, o admin também pode digitar nome ou ID."""
    STATES[user_id] = {"state": "admin_pick", "data": {"mode": mode}}
    products = admin_all_products()
    _show(chat_id, message_id, views.admin_picker_text(products, mode), keyboards.admin_picker(products, page, mode))



def start_stock_add(chat_id, user_id, product_id, note=None, message_id=None):
    product = storage.get_product(product_id, include_inactive=True)
    if not product:
        raise ValueError("Produto não encontrado.")
    STATES[user_id] = {"state": "stock_add", "data": {"product_id": product_id}}
    text = views.stock_add_prompt(product)
    if note:
        text = f"{note}\n\n{text}"
    _show(chat_id, message_id, text, keyboards.stock_add_menu(product_id))


def admin_pick_from_text(chat_id, user_id, mode, text):
    products = admin_all_products()
    value = text.strip().lstrip("#")
    matches = [product for product in products if value.isdigit() and product["id"] == int(value)]
    if not matches:
        words = _fold(value).split()
        matches = [product for product in products if all(word in _fold(product["name"]) for word in words)]
    if len(matches) == 1:
        if mode == "stock":
            start_stock_add(chat_id, user_id, matches[0]["id"])
        else:
            STATES.pop(user_id, None)
            admin_product_panel(chat_id, matches[0]["id"])
        return
    panels.edit_active_panel(
        chat_id,
        views.admin_picker_text(matches, mode, search=text.strip()[:60])
        + ("" if matches else "\n\n⚠️ Nenhum produto encontrado. Tente outro nome."),
        keyboards.admin_picker(matches[:20], 0, mode, filtered=True),
    )


def handle_admin_product_action(chat_id, message_id, user_id, data):
    parts = data.split(":")
    product_id = validators.parse_id(parts[2], "produto")
    action = parts[3] if len(parts) > 3 else "view"
    product = storage.get_product(product_id, include_inactive=True)
    if not product:
        STATES.pop(user_id, None)
        admin_product_panel(chat_id, product_id, message_id=message_id)
        return
    name = escape(product["name"])
    if action == "view":
        STATES.pop(user_id, None)
        admin_product_panel(chat_id, product_id, message_id=message_id)
    elif action == "add":
        start_stock_add(chat_id, user_id, product_id, message_id=message_id)
    elif action == "v":
        STATES.pop(user_id, None)
        show_access(chat_id, message_id, product, int(parts[4]) if len(parts) > 4 else 0)
    elif action == "xa":
        access_id, index = int(parts[4]), int(parts[5])
        panels.edit_message(
            chat_id, message_id,
            f"🗑 Remover o acesso <b>#{access_id}</b> de <b>{name}</b> do estoque?\nIsso não pode ser desfeito.",
            keyboards.access_remove_confirm(product_id, access_id, index)
        )
    elif action == "xa!":
        access_id, index = int(parts[4]), int(parts[5])
        stock = storage.remove_access(product_id, access_id)
        logs.stock_changed(user_id, product_id, -1, f"🗑 Acesso #{access_id} removido pelo visualizador")
        note = f"🗑 Acesso #{access_id} removido. Estoque: {stock}."
        product = storage.get_product(product_id, include_inactive=True)
        if stock:
            show_access(chat_id, message_id, product, min(index, stock - 1), note)
        else:
            admin_product_panel(chat_id, product_id, note, message_id)
    elif action in {"rm1", "clear", "clear!"} and not product["stock"]:
        admin_product_panel(chat_id, product_id, "ℹ️ O estoque já está vazio.", message_id)
    elif action == "rm1":
        removed, stock = storage.remove_licenses(product_id, 1)
        logs.stock_changed(user_id, product_id, -removed, "➖ Remover 1 (o mais recente)")
        admin_product_panel(chat_id, product_id, f"➖ {removed} acesso removido. Estoque: {stock}.", message_id)
    elif action == "clear":
        panels.edit_message(
            chat_id, message_id,
            f"🧹 Remover <b>todos os {product['stock']}</b> acessos de <b>{name}</b>?\nIsso não pode ser desfeito.",
            keyboards.admin_confirm(product_id, "clear", "esvaziar")
        )
    elif action == "clear!":
        removed, _ = storage.remove_licenses(product_id)
        logs.stock_changed(user_id, product_id, -removed, "🧹 Esvaziar estoque")
        admin_product_panel(chat_id, product_id, f"🧹 {removed} acesso(s) removido(s).", message_id)
    elif action == "toggle":
        updated = storage.update_product(product_id, active=not product["active"])
        logs.product_event(user_id, "VENDAS ATIVADAS" if updated["active"] else "VENDAS PAUSADAS", product_id)
        note = "▶️ Produto à venda." if updated["active"] else "⏸ Vendas pausadas: o produto saiu da loja."
        admin_product_panel(chat_id, product_id, note, message_id)
    elif action == "del":
        panels.edit_message(
            chat_id, message_id,
            f"🗑 Excluir <b>{name}</b> e os {product['stock']} acesso(s) em estoque?\n"
            "Compras já feitas continuam no histórico dos clientes.",
            keyboards.admin_confirm(product_id, "del", "excluir")
        )
    elif action == "del!":
        STATES.pop(user_id, None)
        removed_stock = storage.delete_product(product_id)
        logs.product_event(
            user_id, "PRODUTO EXCLUÍDO", product_id,
            f"{removed_stock} acesso(s) em estoque descartado(s)", name=product["name"]
        )
        products = storage.list_products(include_inactive=True)
        panels.edit_message(
            chat_id, message_id, f"🗑 Produto <b>{name}</b> excluído.\n\n" + views.admin_products_text(products),
            keyboards.admin_products(products)
        )
    elif action == "img":
        STATES[user_id] = {"state": "edit_image", "data": {"product_id": product_id}}
        current = (f"Imagem atual:\n<code>{escape(product['image'])}</code>" if product["image"]
                   else "Este produto ainda não tem imagem.")
        panels.edit_message(
            chat_id, message_id,
            f"🖼 <b>Imagem de {name}</b>\n\n{current}\n\n"
            "Envie o <b>link direto</b> da imagem (JPG, PNG ou WEBP), ex.:\n"
            "<code>https://i.imgur.com/abc123.jpg</code>\n\n"
            "Ela aparece junto da mensagem quando o cliente abre o produto.\n"
            "Envie <code>0</code> para remover a imagem.",
            keyboards.admin_product_back(product_id, "❌ Cancelar")
        )
    elif action in {"price", "name", "desc"}:
        STATES[user_id] = {"state": f"edit_{action}", "data": {"product_id": product_id}}
        prompt = {
            "price": f"💲 Preço atual: <b>{validators.money(product['price'])}</b>\nEnvie o novo preço. Exemplo: 29,90",
            "name": f"✏️ Nome atual: <b>{name}</b>\nEnvie o novo nome:",
            "desc": "📝 Envie a nova descrição do produto:",
        }[action]
        panels.edit_message(chat_id, message_id, prompt, keyboards.admin_product_back(product_id, "❌ Cancelar"))
    else:
        raise ValueError("Ação desconhecida.")


def set_product_image(chat_id, user_id, product_id, text):
    """Salva o link só depois de o Telegram conseguir usar a imagem (mostra a prévia ao admin)."""
    url = validators.parse_image_url(text)
    product = storage.get_product(product_id, include_inactive=True)
    if not product:
        raise ValueError("Produto não encontrado.")
    STATES.pop(user_id, None)
    if not url:
        storage.update_product(product_id, image="")
        logs.product_event(user_id, "PRODUTO EDITADO", product_id, "Imagem removida")
        admin_product_panel(chat_id, product_id, "🖼 Imagem removida.")
        return
    preview = {**product, "image": url}
    caption = views.admin_product_text(preview, "🖼 <b>Prévia:</b> é assim que a foto aparece para o cliente.")
    try:
        panels.send_photo_panel(
            chat_id, url, caption[:views.CAPTION_LIMIT], keyboards.admin_product(preview),
            panels.active_menu_message_id(chat_id)
        )
    except Exception as exc:
        print(f"[PRODUTO] Link de imagem recusado pelo Telegram para o produto #{product_id} ({url}): {exc}")
        STATES[user_id] = {"state": "edit_image", "data": {"product_id": product_id}}
        panels.edit_active_panel(
            chat_id,
            "⚠️ <b>O Telegram não conseguiu usar essa imagem.</b>\n\n"
            "Use um <b>link direto</b> para o arquivo da imagem (termina em .jpg, .png ou .webp e abre só a foto "
            "no navegador). Links de páginas, do Google Drive ou do Instagram não funcionam.\n\n"
            f"<i>Erro do Telegram:</i> <code>{escape(str(exc))[:200]}</code>\n\n"
            "Envie outro link ou toque em Cancelar.",
            keyboards.admin_product_back(product_id, "❌ Cancelar")
        )
        return
    storage.update_product(product_id, image=url)
    logs.product_event(user_id, "PRODUTO EDITADO", product_id, "Imagem definida")


def show_access(chat_id, message_id, product, index, note=None):
    """Mostra um acesso do estoque por vez (site, login e senha) com ⬅️ ➡️ para navegar."""
    accesses = storage.list_accesses(product["id"])
    if not accesses:
        admin_product_panel(chat_id, product["id"], "ℹ️ Este produto está sem estoque.", message_id)
        return
    index = min(max(index, 0), len(accesses) - 1)
    access = accesses[index]
    try:
        credentials = json.loads(security.decrypt_text(access["dados"]))
        if not isinstance(credentials, dict):
            credentials = None
    except Exception as exc:
        print("Não foi possível descriptografar o acesso", access["id"], type(exc).__name__)
        credentials = None
    panels.edit_message(
        chat_id, message_id,
        views.access_view_text(product, access, index, len(accesses), credentials, note),
        keyboards.access_viewer(product["id"], index, len(accesses), access["id"], credentials)
    )


def add_stock_from_text(chat_id, message, product_id, text):
    if not storage.get_product(product_id, include_inactive=True):
        raise ValueError("Produto não encontrado.")
    payloads, errors = [], []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            credentials, info = validators.parse_access_line(line)
        except ValueError as exc:
            errors.append(f"linha {number}: {exc}")
            continue
        payloads.append({"dados": security.encrypt_text(json.dumps(credentials, ensure_ascii=False)), **info})
    if payloads:
        storage.add_licenses(product_id, payloads)
        logs.stock_changed(
            message.get("from", {}).get("id", chat_id), product_id, len(payloads),
            f"{len(errors)} linha(s) ignorada(s) por formato inválido" if errors else None
        )
    # A mensagem do admin contém senhas: apaga do chat depois de salvar (criptografado).
    if message.get("message_id") is not None:
        try:
            telegram_api.delete_message(chat_id, message["message_id"])
        except Exception as exc:
            print("Não foi possível apagar a mensagem com os acessos:", exc)
    product = storage.get_product(product_id, include_inactive=True)
    panels.edit_active_panel(
        chat_id, views.stock_add_prompt(product, len(payloads), errors),
        keyboards.stock_add_menu(product_id)
    )


def send_license(chat_id, order_id, product_name, encrypted_payload, price=None):
    balance = (storage.get_user(chat_id) or {}).get("balance")
    text, markup = views.delivery_view(product_name, encrypted_payload, price, balance)
    panels.edit_active_panel(chat_id, text, markup)
    storage.set_order_status(order_id, "completed")


def show_pix_creation_result(chat_id, deposit, customer):
    try:
        deposit = payments.create_pix_charge(deposit, customer)
    except payments.PaymentAPIError as exc:
        print(
            "Falha na criação da transação ROASClub:",
            f"HTTP {exc.http_status}" if exc.http_status else "sem status HTTP",
            exc.code
        )
        logs.pix_creation_failed(
            deposit, f"{exc.code} (HTTP {exc.http_status})" + (" — resultado incerto" if exc.retryable else "")
        )
        if exc.retryable:
            storage.mark_pix_deposit_uncertain(deposit["id"])
            ACTIVE_PIX_DEPOSITS[chat_id] = deposit["id"]
            panels.edit_active_panel(
                chat_id,
                views.pix_creation_pending_text()
                + f"\n\n<b>Referência:</b> <code>{escape(deposit['external_ref'])}</code>",
                keyboards.pix_creation_pending_keyboard()
            )
        else:
            storage.fail_pix_deposit(deposit["id"])
            ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
            detail = escape(exc.detail) if exc.detail else "Verifique as credenciais e tente novamente."
            api_status = f"HTTP {exc.http_status} — " if exc.http_status else ""
            panels.edit_active_panel(
                chat_id,
                f"Não foi possível criar o Pix na ROASClub.\n\n{api_status}{detail}",
                keyboards.pix_back_menu()
            )
        return
    except Exception as exc:
        print(
            "Falha inesperada durante a criação da cobrança Pix:",
            type(exc).__name__,
            str(exc)[:300],
        )
        logs.pix_creation_failed(deposit, f"{type(exc).__name__} — resultado incerto")
        storage.mark_pix_deposit_uncertain(deposit["id"])
        ACTIVE_PIX_DEPOSITS[chat_id] = deposit["id"]
        panels.edit_active_panel(
            chat_id,
            views.pix_creation_pending_text()
            + f"\n\n<b>Referência:</b> <code>{escape(deposit['external_ref'])}</code>",
            keyboards.pix_creation_pending_keyboard()
        )
        return
    ACTIVE_PIX_DEPOSITS[chat_id] = deposit["id"]
    logs.pix_created(deposit)
    try:
        show_pix_qr_panel(chat_id, deposit)
    except (RuntimeError, ValueError, OSError, qrcode.exceptions.DataOverflowError) as exc:
        print("Não foi possível exibir a imagem do QR Pix; usando o código copia e cola:", exc)
        panels.edit_active_panel(chat_id, views.pix_deposit_text(deposit), keyboards.pix_deposit_keyboard(deposit))


def show_pix_qr_panel(chat_id, deposit):
    photo_data = views.pix_qr_png(deposit["pix_code"])
    old_message_id = panels.active_menu_message_id(chat_id)
    result = telegram_api.send_photo(
        chat_id,
        photo_data,
        views.pix_deposit_caption(deposit),
        keyboards.pix_deposit_keyboard(deposit),
    )
    if not isinstance(result, dict) or result.get("message_id") is None:
        raise RuntimeError("Telegram não retornou o ID da mensagem com o QR Pix.")
    photo_message_id = result["message_id"]
    panels.remember_active_menu(chat_id, photo_message_id, media="photo")
    if old_message_id is not None and old_message_id != photo_message_id:
        try:
            telegram_api.delete_message(chat_id, old_message_id)
        except Exception as exc:
            print("Não foi possível remover o painel anterior após exibir o QR Pix:", exc)


def notify(chat_id, text):
    """Aviso avulso (não substitui o painel que a pessoa está usando)."""
    try:
        telegram_api.send_message(chat_id, text, keyboards.inline([[{"text": "👤 Ver meu perfil", "callback_data": "profile"}]]))
    except Exception as exc:
        print(f"Não foi possível avisar {chat_id}:", exc)


def send_welcome_bonus_message(chat_id, user_id):
    """Mensagem própria avisando o usuário novo de que o bônus de boas-vindas caiu na conta."""
    user = storage.get_user(user_id) or {}
    bonus = user.get("welcome_bonus")
    if not bonus:
        return
    name = escape(user.get("first_name") or "")
    try:
        telegram_api.send_message(
            chat_id,
            f"🎁 <b>Bônus de boas-vindas creditado!</b>\n\n"
            f"Olá{', ' + name if name else ''}! Os <b>{validators.money(bonus)}</b> de boas-vindas "
            f"foram creditados na sua conta.\n\n"
            f"🏦 Saldo atual: <b>{validators.money(user.get('balance', '0'))}</b>\n"
            "Use o saldo para comprar na loja. 🛍"
        )
    except Exception as exc:
        print(f"Não foi possível enviar a mensagem de boas-vindas para {chat_id}:", exc)


def register_referral(user, text):
    """Primeiro /start vindo de t.me/bot?start=ID_DE_QUEM_INDICOU."""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        return
    referrer_id = int(parts[1].strip())
    bonus = storage.set_referrer(user["id"], referrer_id)
    if bonus is None:
        return
    name = escape(user.get("first_name") or "Um novo usuário")
    referral_bonus = storage.get_settings()["referral_bonus"]
    if bonus > 0:
        reward = f"💰 Você recebeu <b>{validators.money(bonus)}</b> de bônus de indicação."
    elif referral_bonus > 0:
        reward = f"🎁 Você ganha <b>{validators.money(referral_bonus)}</b> quando ele fizer o 1º depósito."
    else:
        reward = ""
    notify(referrer_id, f"🎉 <b>Novo indicado!</b>\n\n<b>{name}</b> entrou pelo seu link.\n{reward}".rstrip())
    if bonus > 0:
        logs.referral_bonus(referrer_id, user["id"], bonus, "ao entrar pelo link")


def on_pix_confirmed(deposit_id):
    """Chamado uma vez quando um Pix é confirmado: log no canal + aviso/log da indicação."""
    deposit = storage.get_pix_deposit(deposit_id)
    if deposit:
        logs.pix_confirmed(deposit)
    notify_referral_reward(deposit_id)


def on_pix_failed(deposit):
    logs.pix_failed(deposit)


def notify_referral_reward(deposit_id):
    reward = storage.take_referral_notification(deposit_id)
    if not reward:
        return
    logs.referral_bonus(reward["referrer_id"], reward["referred_id"], reward["bonus"], "no 1º depósito")
    referred = storage.get_user(reward["referred_id"]) or {}
    name = escape(referred.get("first_name") or "Seu indicado")
    lines = [
        f"🤝 <b>Ganho de indicação!</b>\n\n<b>{name}</b> fez o primeiro depósito.",
        f"🎁 Bônus: <b>{validators.money(reward['bonus'])}</b>",
    ]
    balance = (storage.get_user(reward["referrer_id"]) or {}).get("balance", "0.00")
    lines.append(f"\n🏦 Saldo atual: <b>{validators.money(balance)}</b>")
    notify(reward["referrer_id"], "\n".join(lines))


def show_pix_payment_confirmation(chat_id, deposit):
    user = storage.get_user(deposit["telegram_id"])
    balance = user["balance"] if user else None
    ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
    panels.edit_active_panel(chat_id, views.pix_confirmed_text(deposit, balance), keyboards.pix_back_menu())


def notify_pix_payment_confirmation(deposit):
    chat_id = deposit["telegram_id"]
    if ACTIVE_PIX_DEPOSITS.get(chat_id) == deposit["id"]:
        try:
            show_pix_payment_confirmation(chat_id, deposit)
            return
        except Exception as exc:
            print("Não foi possível atualizar o painel da recarga Pix:", exc)
    try:
        panels.edit_active_panel(
            chat_id,
            views.pix_confirmed_text(deposit, (storage.get_user(chat_id) or {}).get("balance")),
            keyboards.pix_back_menu()
        )
    except Exception as exc:
        print("Não foi possível notificar o usuário sobre a recarga Pix:", exc)


def notify_pix_deposit_failure(deposit):
    chat_id = deposit["telegram_id"]
    text = "⚠️ Esta cobrança Pix não foi confirmada e nenhum saldo foi adicionado. Você pode iniciar uma nova recarga."
    if ACTIVE_PIX_DEPOSITS.get(chat_id) == deposit["id"]:
        try:
            ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
            panels.edit_active_panel(chat_id, text, keyboards.pix_back_menu())
            return
        except Exception as exc:
            print("Não foi possível atualizar o painel da cobrança Pix:", exc)
    try:
        panels.edit_active_panel(chat_id, text, keyboards.pix_back_menu())
    except Exception as exc:
        print("Não foi possível notificar falha na cobrança Pix:", exc)


def show_pix_status(chat_id, message_id, deposit, note):
    """Mostra um aviso sobre a cobrança sem remover o QR Code da tela."""
    if panels.active_menu_is_photo(chat_id, message_id):
        panels.edit_message(
            chat_id, message_id, views.pix_deposit_caption(deposit) + "\n\n" + note,
            keyboards.pix_deposit_keyboard(deposit), keep_photo=True
        )
    else:
        panels.edit_message(
            chat_id, message_id, note + "\n\n" + views.pix_deposit_text(deposit), keyboards.pix_deposit_keyboard(deposit)
        )


def request_pix_amount(chat_id, user_id):
    problems = payments.pix_config_problems()
    if problems:
        print("Recarga Pix indisponível:", "; ".join(problems))
        panels.edit_active_panel(
            chat_id,
            "A recarga via Pix está indisponível no momento. Fale com o suporte.",
            keyboards.back_menu()
        )
        return
    ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
    STATES[user_id] = {"state": "pix_amount", "data": {}}
    panels.edit_active_panel(
        chat_id,
        "💰 <b>Adicionar saldo via Pix</b>\n\n"
        "Envie apenas o valor que deseja depositar — nenhum dado pessoal é solicitado, "
        "a cobrança é gerada automaticamente.\n\n"
        "O valor integral será adicionado ao saldo após a confirmação do pagamento.\n\n"
        "Valor mínimo: <b>R$ 10,00</b>.\n\nUse /cancel para cancelar.",
        keyboards.pix_back_menu()
    )


def reconcile_pix_deposits():
    for deposit in storage.pending_pix_deposits():
        try:
            if payments.check_pix_payment(deposit):
                confirmed = storage.get_pix_deposit(deposit["id"])
                if confirmed:
                    notify_pix_payment_confirmation(confirmed)
                on_pix_confirmed(deposit["id"])
            else:
                updated = storage.get_pix_deposit(deposit["id"])
                if updated and updated["status"] == "failed":
                    on_pix_failed(updated)
                    notify_pix_deposit_failure(updated)
        except payments.PaymentAPIError as exc:
            print("Não foi possível verificar cobrança Pix:", exc.code)
        except Exception as exc:
            print("Falha ao reconciliar cobrança Pix:", exc)


def handle_message(message):
    chat_id = message.get("chat", {}).get("id")
    user = message.get("from", {})
    user_id = user.get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not user_id:
        return
    is_new_user = storage.ensure_user(user)
    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
    if command == "/start" and is_new_user:
        register_referral(user, text)
    if is_new_user:
        logs.new_user(user_id)
        send_welcome_bonus_message(chat_id, user_id)
    if command == "/pix":
        request_pix_amount(chat_id, user_id)
        return
    if command == "/start":
        STATES.pop(user_id, None)
        ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
        home(chat_id, user_id, new_message=True)  # o menu vem logo abaixo da mensagem de boas-vindas
        return
    if command == "/cancel":
        STATES.pop(user_id, None)
        ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
        home(chat_id, user_id)
        return
    if command in {"/produto", "/produtos"}:
        # "/produto ID" chega quando o usuário escolhe um resultado da busca inline.
        STATES.pop(user_id, None)
        ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
        if message.get("message_id") is not None:
            try:
                telegram_api.delete_message(chat_id, message["message_id"])
            except Exception as exc:
                print("Não foi possível apagar a mensagem da busca:", exc)
        parts = text.split()
        try:
            if command == "/produto" and len(parts) == 2:
                show_product(chat_id, validators.parse_id(parts[1], "produto"))
            else:
                show_as_new_panel(chat_id, *catalog_view(user_id))
        except ValueError as exc:
            show_as_new_panel(chat_id, f"⚠️ {escape(str(exc))}", keyboards.product_buttons(catalog_products()))
        return
    if command == "/admin":
        if not is_admin(user_id):
            panels.edit_active_panel(chat_id, "Você não tem permissão para acessar essa função.", keyboards.back_menu())
            return
        admin_home(chat_id, user_id)
        return
    if command == "/saldo":
        if not is_admin(user_id):
            panels.edit_active_panel(chat_id, "Você não tem permissão para acessar essa função.", keyboards.back_menu())
            return
        parts = text.split()
        if len(parts) != 3:
            panels.edit_active_panel(chat_id, "Use: <code>/saldo ID_TELEGRAM VALOR</code>", keyboards.back_menu())
            return
        try:
            target_id = validators.parse_id(parts[1], "ID Telegram")
            amount = validators.parse_money(parts[2])
            storage.add_balance(target_id, amount)
            logs.admin_balance(user_id, target_id, amount)
            panels.edit_active_panel(chat_id, f"Saldo de {target_id} atualizado em {validators.money(amount)}.", keyboards.admin_menu())
            # Aviso em mensagem separada: não sobrescreve a tela que o cliente está usando (ex.: QR Pix).
            balance = (storage.get_user(target_id) or {}).get("balance", "0")
            notify(target_id, f"💰 Seu saldo recebeu um crédito de <b>{validators.money(amount)}</b>.\n"
                              f"🏦 Saldo atual: <b>{validators.money(balance)}</b>")
        except Exception as exc:
            print("Falha ao adicionar saldo pelo comando:", exc)
            detail = escape(str(exc)) if isinstance(exc, ValueError) else (
                "Não foi possível concluir a operação."
            )
            try:
                panels.edit_active_panel(
                    chat_id,
                    f"⚠️ {detail}\nConfira o ID e o valor e tente novamente.",
                    keyboards.admin_menu()
                )
            except Exception as notify_exc:
                print("Não foi possível avisar o administrador:", notify_exc)
        return

    state = STATES.get(user_id)
    if not state:
        if command:
            panels.edit_active_panel(chat_id, "Use /start para abrir o menu principal.", keyboards.back_menu())
        return
    step = state["state"]
    if step in ADMIN_STEPS and not is_admin(user_id):
        STATES.pop(user_id, None)
        return
    if not text and step != "cfg_logs":  # post encaminhado do canal pode ser só foto
        panels.edit_active_panel(chat_id, "Envie a resposta como texto ou use /cancel.", keyboards.back_menu())
        return
    try:
        if step == "product_name":
            state["data"]["name"] = text[:180]
            state["state"] = "product_description"
            panels.edit_active_panel(chat_id, "Envie a descrição do produto:", keyboards.back_menu())
        elif step == "product_description":
            state["data"]["description"] = text
            state["state"] = "product_price"
            panels.edit_active_panel(chat_id, "Envie o preço em reais. Exemplo: 29,90", keyboards.back_menu())
        elif step == "product_price":
            price = validators.parse_money(text)
            product_id = storage.create_product(state["data"]["name"], state["data"]["description"], price)
            logs.product_event(user_id, "PRODUTO CADASTRADO", product_id)
            start_stock_add(
                chat_id, user_id, product_id,
                f"✅ Produto #{product_id} cadastrado! Já pode enviar os acessos do estoque."
            )
        elif step in {"cfg_welcome", "cfg_ref"}:
            amount = validators.parse_money_or_zero(text)
            if step == "cfg_welcome":
                storage.update_settings(welcome_bonus=amount)
                label = "boas-vindas"
            else:
                storage.update_settings(referral_bonus=amount)
                label = "indicação"
            status = f"agora é {validators.money(amount)}" if amount > 0 else "desativado"
            logs.settings_changed(user_id, f"Bônus de {label} {status}")
            admin_settings_panel(chat_id, user_id, f"✅ Bônus de {label} {status}.")
        elif step == "cfg_logs":
            set_log_channel_from_message(chat_id, user_id, message, text)
        elif step == "admin_pick":
            admin_pick_from_text(chat_id, user_id, state["data"]["mode"], text)
        elif step == "stock_add":
            add_stock_from_text(chat_id, message, state["data"]["product_id"], text)
        elif step == "edit_image":
            set_product_image(chat_id, user_id, state["data"]["product_id"], text)
        elif step in {"edit_price", "edit_name", "edit_desc"}:
            product_id = state["data"]["product_id"]
            before = storage.get_product(product_id, include_inactive=True) or {}
            if step == "edit_price":
                price = validators.parse_money(text)
                storage.update_product(product_id, price=price)
                note = f"💲 Preço alterado para {validators.money(price)}."
                detail = f"Preço: {validators.money(before.get('price', 0))} → {validators.money(price)}"
            elif step == "edit_name":
                storage.update_product(product_id, name=text[:180])
                note = "✏️ Nome alterado."
                detail = f"Nome: {escape(before.get('name', ''))} → {escape(text[:180])}"
            else:
                storage.update_product(product_id, description=text)
                note = "📝 Descrição alterada."
                detail = "Descrição alterada"
            logs.product_event(user_id, "PRODUTO EDITADO", product_id, detail)
            STATES.pop(user_id, None)
            admin_product_panel(chat_id, product_id, note)
        elif step == "balance_user":
            target_id = validators.parse_id(text, "ID Telegram")
            if not storage.get_user(target_id):
                raise ValueError("Usuário não encontrado. Peça para ele iniciar o bot com /start.")
            state["data"]["user_id"] = target_id
            state["state"] = "balance_amount"
            panels.edit_active_panel(chat_id, "Envie o valor do crédito em reais:", keyboards.back_menu())
        elif step == "balance_amount":
            amount = validators.parse_money(text)
            storage.add_balance(state["data"]["user_id"], amount)
            logs.admin_balance(user_id, state["data"]["user_id"], amount)
            STATES.pop(user_id, None)
            panels.edit_active_panel(chat_id, f"Saldo adicionado: {validators.money(amount)}.", keyboards.admin_menu())
        elif step == "pix_amount":
            try:
                amount_cents = validators.parse_pix_amount_cents(text)
                customer = payments.configured_customer()
            except payments.PaymentAPIError as exc:
                print("Recarga Pix indisponível:", exc.code)
                STATES.pop(user_id, None)
                panels.edit_active_panel(
                    chat_id, "A recarga via Pix está indisponível no momento. Fale com o suporte.", keyboards.back_menu()
                )
                return
            except (InvalidOperation, ValueError) as exc:
                panels.edit_active_panel(
                    chat_id,
                    f"Valor inválido: {escape(str(exc))}\nEnvie o valor que deseja pagar via Pix.",
                    keyboards.back_menu()
                )
                return
            deposit = storage.start_pix_deposit(user_id, amount_cents)
            STATES.pop(user_id, None)
            show_pix_creation_result(chat_id, deposit, customer)
        elif step == "resend_order":
            order_id = validators.parse_id(text, "pedido")
            order = storage.get_failed_order(order_id)
            if not order or not order["encrypted_payload"]:
                raise ValueError("Pedido pendente/falho não encontrado.")
            send_license(order["telegram_id"], order["id"], order["name"], order["encrypted_payload"], order["amount"])
            logs.delivery_resent(user_id, order)
            STATES.pop(user_id, None)
            panels.edit_active_panel(chat_id, "Entrega reenviada.", keyboards.admin_menu())
    except (ValueError, InvalidOperation) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else "valor inválido."
        panels.edit_active_panel(
            chat_id,
            f"⚠️ {escape(detail)}\nTente novamente ou use /cancel.",
            keyboards.back_menu()
        )
    except Exception as exc:
        print("Falha ao processar mensagem:", exc)
        try:
            panels.edit_active_panel(
                chat_id,
                "Não foi possível concluir agora. Tente novamente em instantes ou use /cancel.",
                keyboards.back_menu()
            )
        except Exception as notify_exc:
            print("Não foi possível avisar o usuário:", notify_exc)


def handle_callback(query):
    user = query.get("from", {})
    user_id = user.get("id")
    message = query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    data = query.get("data", "")
    if not user_id or not chat_id:
        return
    if storage.ensure_user(user):
        logs.new_user(user_id)  # primeiro contato veio por um botão, não pelo /start
        send_welcome_bonus_message(chat_id, user_id)
    if message_id is not None:
        panels.remember_active_menu(chat_id, message_id, media="photo" if message.get("photo") else "text")
    if data != "wallet:deposit" and not data.startswith("pix:"):
        ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
    try:
        try:
            telegram_api.answer_callback(
                query["id"],
                text="Você já está nesta página." if data in {"history:page", "catalog:page"} else None
            )
        except Exception as exc:
            print("Não foi possível confirmar o callback do Telegram:", exc)
        if data == "home":
            STATES.pop(user_id, None)
            home(chat_id, user_id, message_id)
        elif data == "wallet:deposit":
            request_pix_amount(chat_id, user_id)
        elif data == "catalog:page":
            return
        elif data == "catalog" or data.startswith("catalog:"):
            page = int(data.split(":", 1)[1]) if ":" in data else 0
            panels.edit_message(chat_id, message_id, *catalog_view(user_id, page))
        elif data == "howto":
            panels.edit_message(chat_id, message_id, views.how_it_works_text(), keyboards.howto_menu())
        elif data.startswith("product:"):
            parts = data.split(":")
            show_product(chat_id, int(parts[1]), message_id, int(parts[2]) if len(parts) > 2 else 0)
        elif data.startswith("buy:"):
            parts = data.split(":")
            product_id = int(parts[1])
            access_id = int(parts[2]) if len(parts) > 2 else None
            try:
                result = storage.purchase(user_id, product_id, access_id)
            except ValueError as exc:
                if "acabou de ser vendido" not in str(exc):
                    raise
                show_product(chat_id, product_id, message_id, 0, f"⚠️ {escape(str(exc))}")
                return
            logs.purchase(user_id, result)
            try:
                balance = (storage.get_user(user_id) or {}).get("balance")
                delivery_text, delivery_keyboard = views.delivery_view(
                    result["product"]["name"], result["encrypted_payload"], result["product"]["price"], balance
                )
                panels.edit_message(chat_id, message_id, delivery_text, delivery_keyboard)
                storage.set_order_status(result["order_id"], "completed")
            except Exception as exc:
                storage.set_order_status(result["order_id"], "delivery_failed")
                print("Falha na entrega:", exc)
                logs.delivery_failed(user_id, result["order_id"], result["product"]["name"], exc)
                try:
                    panels.edit_message(
                        chat_id, message_id,
                        f"⚠️ O pagamento do pedido #{result['order_id']} foi registrado, mas a entrega está pendente. "
                        "Entre em contato com o suporte para receber sua compra.",
                        keyboards.purchase_actions()
                    )
                except Exception as notify_exc:
                    print("Não foi possível exibir as opções de suporte:", notify_exc)
                    return
        elif data == "profile":
            panels.edit_message(chat_id, message_id, *views.profile_view(user_id))
        elif data == "history":
            text, keyboard = views.order_history_view(user_id, 0)
            panels.edit_message(chat_id, message_id, text, keyboard)
        elif data == "history:page":
            return
        elif data.startswith("history:"):
            index = int(data.split(":", 1)[1])
            text, keyboard = views.order_history_view(user_id, index)
            panels.edit_message(chat_id, message_id, text, keyboard)
        elif data.startswith("pix:retry:"):
            deposit_id = data.split(":", 2)[2]
            deposit = storage.get_pix_deposit(deposit_id)
            if not deposit or deposit["telegram_id"] != int(user_id):
                raise ValueError("Cobrança Pix não encontrada.")
            if deposit["status"] == "pending":
                ACTIVE_PIX_DEPOSITS[chat_id] = deposit_id
                panels.edit_message(chat_id, message_id, views.pix_deposit_text(deposit), keyboards.pix_deposit_keyboard(deposit))
            elif deposit["status"] == "creation_unknown":
                panels.edit_message(
                    chat_id, message_id,
                    views.pix_creation_pending_text()
                    + f"\n\n<b>Referência:</b> <code>{escape(deposit['external_ref'])}</code>",
                    keyboards.pix_creation_pending_keyboard()
                )
            elif deposit["status"] == "confirmed":
                panels.edit_message(chat_id, message_id, views.pix_confirmed_text(deposit), keyboards.pix_back_menu())
            else:
                panels.edit_message(
                    chat_id, message_id,
                    "Esta cobrança Pix não está mais disponível. Inicie uma nova recarga pelo menu.",
                    keyboards.pix_back_menu()
                )
        elif data.startswith("pix:check:"):
            deposit_id = data.split(":", 2)[2]
            deposit = storage.get_pix_deposit(deposit_id)
            if not deposit or deposit["telegram_id"] != int(user_id):
                raise ValueError("Cobrança Pix não encontrada.")
            if deposit["status"] == "confirmed":
                panels.edit_message(chat_id, message_id, views.pix_confirmed_text(deposit), keyboards.pix_back_menu())
            elif deposit["status"] == "pending":
                ACTIVE_PIX_DEPOSITS[chat_id] = deposit_id
                try:
                    is_confirmed = payments.check_pix_payment(deposit)
                except payments.PaymentAPIError as exc:
                    print("Falha na verificação manual do Pix:", exc.code)
                    show_pix_status(
                        chat_id, message_id, deposit,
                        "⚠️ <b>Não foi possível consultar o pagamento agora.</b> "
                        "Seu Pix continua válido; tente verificar novamente em alguns segundos."
                    )
                else:
                    if is_confirmed:
                        on_pix_confirmed(deposit_id)
                        confirmed = storage.get_pix_deposit(deposit_id)
                        panels.edit_message(
                            chat_id, message_id,
                            views.pix_confirmed_text(confirmed, storage.get_user(user_id)["balance"]),
                            keyboards.pix_back_menu()
                        )
                        ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
                    else:
                        updated = storage.get_pix_deposit(deposit_id)
                        if updated and updated["status"] == "failed":
                            on_pix_failed(updated)
                            ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
                            panels.edit_message(
                                chat_id, message_id,
                                "⚠️ Esta cobrança Pix não foi confirmada e nenhum saldo foi adicionado. "
                                "Você pode iniciar uma nova recarga.",
                                keyboards.pix_back_menu()
                            )
                        else:
                            show_pix_status(
                                chat_id, message_id, deposit,
                                f"⏳ <b>Pagamento ainda não confirmado</b> ({datetime.now():%H:%M:%S}). "
                                "Se você já pagou, aguarde o processamento e verifique novamente."
                            )
            elif deposit["status"] == "creating":
                ACTIVE_PIX_DEPOSITS[chat_id] = deposit_id
                panels.edit_message(
                    chat_id, message_id,
                    views.pix_creation_pending_text(),
                    keyboards.pix_creation_pending_keyboard()
                )
            else:
                panels.edit_message(
                    chat_id, message_id,
                    "Esta cobrança Pix não está mais disponível. Inicie uma nova recarga pelo menu.",
                    keyboards.pix_back_menu()
                )
        elif data == "admin":
            admin_only(user_id)
            admin_home(chat_id, user_id, message_id)
        elif data == "adm:add_product":
            admin_only(user_id)
            STATES[user_id] = {"state": "product_name", "data": {}}
            panels.edit_active_panel(chat_id, "Envie o nome do produto. Use /cancel para cancelar.", keyboards.back_menu())
        elif data.split(":")[:2] in (["adm", "products"], ["adm", "stock"]):
            admin_only(user_id)
            parts = data.split(":")
            page = int(parts[2]) if len(parts) > 2 else 0
            admin_picker_panel(chat_id, user_id, parts[1], page, message_id)
        elif data == "adm:cfg" or data.startswith("adm:cfg:"):
            admin_only(user_id)
            handle_admin_settings(chat_id, message_id, user_id, data)
        elif data.startswith("adm:p:"):
            admin_only(user_id)
            handle_admin_product_action(chat_id, message_id, user_id, data)
        elif data == "adm:add_balance":
            admin_only(user_id)
            STATES[user_id] = {"state": "balance_user", "data": {}}
            panels.edit_active_panel(chat_id, "Envie o ID Telegram do usuário:", keyboards.back_menu())
        elif data == "adm:resend":
            admin_only(user_id)
            STATES[user_id] = {"state": "resend_order", "data": {}}
            panels.edit_active_panel(chat_id, "Envie o número do pedido que deseja reenviar:", keyboards.back_menu())
    except PermissionError:
        panels.edit_message(chat_id, message_id, "Você não tem permissão para acessar essa função.", keyboards.back_menu())
    except ValueError as exc:
        # Mensagens de regra de negócio (saldo insuficiente, estoque esgotado...) vão para o usuário.
        print("Operação recusada:", exc)
        try:
            panels.edit_message(chat_id, message_id, f"⚠️ {escape(str(exc))}", keyboards.back_menu())
        except Exception as notify_exc:
            print("Não foi possível atualizar a mensagem do menu:", notify_exc)
    except Exception as exc:
        print("Falha ao processar botão:", exc)
        try:
            # O admin vê o motivo (ex.: produtos.json com erro de digitação); o cliente não.
            detail = f"\n\n<code>{escape(str(exc)[:500])}</code>" if is_admin(user_id) else ""
            panels.edit_message(
                chat_id, message_id,
                "Não foi possível concluir agora. Tente novamente em instantes." + detail,
                keyboards.back_menu()
            )
        except Exception as notify_exc:
            print("Não foi possível atualizar a mensagem do menu:", notify_exc)


def _is_private(message):
    return (message or {}).get("chat", {}).get("type", "private") == "private"


def process_update(update):
    # A loja só funciona no chat privado com o bot: em grupos, o painel (saldo, ID, compras)
    # ficaria visível para todo mundo.
    if "message" in update:
        if not _is_private(update["message"]):
            return
        handle_message(update["message"])
    elif "callback_query" in update:
        if not _is_private(update["callback_query"].get("message")):
            try:
                telegram_api.answer_callback(update["callback_query"]["id"], "Use a loja no chat privado com o bot.")
            except Exception as exc:
                print("Não foi possível responder ao botão fora do privado:", exc)
            return
        handle_callback(update["callback_query"])
    elif "inline_query" in update:
        handle_inline_query(update["inline_query"])
