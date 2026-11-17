"""Fluxo do bot: comandos, botões, compras e recargas Pix."""
import json
from datetime import datetime
from decimal import InvalidOperation
from html import escape

import qrcode.exceptions

from . import config, keyboards, panels, payments, security, storage, telegram_api, validators, views


# Estado em memória: cobrança Pix exibida em cada chat e etapa atual de cada fluxo de texto.
ACTIVE_PIX_DEPOSITS = {}
STATES = {}


def is_admin(user_id):
    return user_id in config.ADMIN_IDS


def home(chat_id, user_id, message_id=None, new_message=False):
    text = views.home_text(user_id)
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


def send_license(chat_id, order_id, product_name, encrypted_payload):
    text = views.license_message_text(product_name, encrypted_payload)
    panels.edit_active_panel(chat_id, text, keyboards.purchase_complete_menu())
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
            else:
                updated = storage.get_pix_deposit(deposit["id"])
                if updated and updated["status"] == "failed":
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
    storage.ensure_user(user)
    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
    if command == "/pix":
        request_pix_amount(chat_id, user_id)
        return
    if command == "/start":
        STATES.pop(user_id, None)
        ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
        home(chat_id, user_id, new_message=True)
        return
    if command == "/cancel":
        STATES.pop(user_id, None)
        ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
        home(chat_id, user_id)
        return
    if command == "/admin":
        if not is_admin(user_id):
            panels.edit_active_panel(chat_id, "Você não tem permissão para acessar essa função.", keyboards.back_menu())
            return
        panels.edit_active_panel(chat_id, "<b>Painel administrativo</b>", keyboards.admin_menu())
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
            panels.edit_active_panel(chat_id, f"Saldo de {target_id} atualizado em {validators.money(amount)}.", keyboards.admin_menu())
            try:
                panels.edit_active_panel(
                    target_id, f"💰 Seu saldo recebeu um crédito de <b>{validators.money(amount)}</b>.", keyboards.back_menu()
                )
            except Exception as notify_exc:
                print("Não foi possível notificar o usuário sobre o crédito:", notify_exc)
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
    if not text:
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
            STATES.pop(user_id, None)
            panels.edit_active_panel(
                chat_id, f"Produto #{product_id} cadastrado. Agora adicione licenças pelo painel.", keyboards.admin_menu()
            )
        elif step == "license_product":
            product_id = validators.parse_id(text, "produto")
            product = storage.get_product(product_id)
            if not product:
                raise ValueError("Produto não encontrado ou inativo.")
            state["data"]["product_id"] = product_id
            state["state"] = "license_url"
            panels.edit_active_panel(chat_id, "Envie a URL de acesso:", keyboards.back_menu())
        elif step == "license_url":
            state["data"]["url"] = text
            state["state"] = "license_login"
            panels.edit_active_panel(chat_id, "Envie o login:", keyboards.back_menu())
        elif step == "license_login":
            state["data"]["login"] = text
            state["state"] = "license_password"
            panels.edit_active_panel(chat_id, "Envie a senha:", keyboards.back_menu())
        elif step == "license_password":
            state["data"]["password"] = text
            encrypted = security.encrypt_text(json.dumps(state["data"], ensure_ascii=False))
            storage.add_license(state["data"]["product_id"], encrypted)
            STATES.pop(user_id, None)
            panels.edit_active_panel(chat_id, "✅ Licença adicionada ao estoque.", keyboards.admin_menu())
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
            send_license(order["telegram_id"], order["id"], order["name"], order["encrypted_payload"])
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
    storage.ensure_user(user)
    if message_id is not None:
        panels.remember_active_menu(chat_id, message_id, media="photo" if message.get("photo") else "text")
    if data != "wallet:deposit" and not data.startswith("pix:"):
        ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
    try:
        try:
            telegram_api.answer_callback(
                query["id"],
                text="Você já está nesta página." if data == "history:page" else None
            )
        except Exception as exc:
            print("Não foi possível confirmar o callback do Telegram:", exc)
        if data == "home":
            STATES.pop(user_id, None)
            home(chat_id, user_id, message_id)
        elif data == "wallet:deposit":
            request_pix_amount(chat_id, user_id)
        elif data == "catalog":
            products = storage.list_products()
            if not products:
                panels.edit_message(chat_id, message_id, "Ainda não há produtos disponíveis.", keyboards.back_menu())
            else:
                lines = ["<b>Produtos disponíveis</b>\n"]
                for product in products:
                    lines.append(
                        f"<b>{escape(product['name'])}</b> — {validators.money(product['price'])} | Estoque: {product['stock']}"
                    )
                    if product["description"]:
                        lines.append(escape(product["description"]))
                    lines.append("")
                catalog_text = "\n".join(lines)
                if len(catalog_text) > 4000:
                    catalog_text = "<b>Produtos disponíveis</b>\n\nEscolha um produto abaixo para ver os detalhes."
                panels.edit_message(chat_id, message_id, catalog_text, keyboards.product_buttons(products))
        elif data.startswith("product:"):
            product_id = int(data.split(":")[1])
            product = storage.get_product(product_id)
            if not product:
                raise ValueError("Produto indisponível.")
            panels.edit_message(
                chat_id, message_id,
                f"<b>{escape(product['name'])}</b>\n\n{escape(product['description'] or '')}\n\n"
                f"Preço: <b>{validators.money(product['price'])}</b>\n\nConfirmar compra?",
                keyboards.confirm_purchase(product_id)
            )
        elif data.startswith("buy:"):
            product_id = int(data.split(":")[1])
            result = storage.purchase(user_id, product_id)
            try:
                delivery_text = views.license_message_text(
                    result["product"]["name"], result["encrypted_payload"]
                )
                panels.edit_message(chat_id, message_id, delivery_text, keyboards.purchase_complete_menu())
                storage.set_order_status(result["order_id"], "completed")
            except Exception as exc:
                storage.set_order_status(result["order_id"], "delivery_failed")
                print("Falha na entrega:", exc)
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
            user_record = storage.get_user(user_id)
            panels.edit_message(
                chat_id, message_id,
                f"<b>Meu perfil</b>\nID: <code>{user_id}</code>\nSaldo: <b>{validators.money(user_record['balance'])}</b>",
                keyboards.back_menu()
            )
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
            panels.edit_message(chat_id, message_id, "<b>Painel administrativo</b>", keyboards.admin_menu())
        elif data == "adm:add_product":
            admin_only(user_id)
            STATES[user_id] = {"state": "product_name", "data": {}}
            panels.edit_active_panel(chat_id, "Envie o nome do produto. Use /cancel para cancelar.", keyboards.back_menu())
        elif data == "adm:add_license":
            admin_only(user_id)
            STATES[user_id] = {"state": "license_product", "data": {}}
            panels.edit_active_panel(chat_id, "Envie o ID do produto que receberá a licença:", keyboards.back_menu())
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
            panels.edit_message(
                chat_id, message_id,
                "Não foi possível concluir agora. Tente novamente em instantes.",
                keyboards.back_menu()
            )
        except Exception as notify_exc:
            print("Não foi possível atualizar a mensagem do menu:", notify_exc)


def process_update(update):
    if "message" in update:
        handle_message(update["message"])
    elif "callback_query" in update:
        handle_callback(update["callback_query"])
