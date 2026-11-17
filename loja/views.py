"""Textos das telas e geração da imagem do QR Pix."""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from io import BytesIO

import qrcode
import qrcode.constants
import qrcode.exceptions

from . import config, keyboards, security, storage, validators


def pix_expiration_label(deposit):
    expires_at = deposit.get("expires_at")
    if not expires_at:
        created_at = datetime.fromisoformat(deposit["created_at"])
        expires_at = (created_at + timedelta(days=config.PIX_DEPOSIT_EXPIRY_DAYS)).isoformat()
    expiration = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    if expiration.tzinfo is not None:
        expiration = expiration.astimezone()
    return expiration.strftime("%d/%m/%Y às %H:%M")


def pix_deposit_text(deposit):
    amount = Decimal(deposit["amount_cents"]) / 100
    return (
        "💠 <b>Pagamento Pix</b>\n\n"
        f"💳 <b>Valor do Pix:</b> {validators.money(amount)}\n"
        f"💰 <b>Valor que será adicionado ao saldo:</b> {validators.money(amount)}\n\n"
        f"⏳ <b>Válido até:</b> {pix_expiration_label(deposit)}\n"
        "O pagamento é verificado automaticamente a cada 30 segundos. Cancelar volta ao menu, mas não cancela a cobrança na ROASClub.\n\n"
        "Copie o código abaixo e pague no aplicativo do seu banco:\n"
        f"<code>{escape(deposit['pix_code'])}</code>\n\n"
        "O saldo será atualizado após a confirmação do pagamento."
    )


def pix_deposit_caption(deposit):
    amount = Decimal(deposit["amount_cents"]) / 100
    return (
        "💠 <b>Pagamento Pix</b>\n\n"
        f"💳 <b>Valor:</b> {validators.money(amount)}\n"
        f"💰 <b>Crédito após confirmação:</b> {validators.money(amount)}\n\n"
        f"⏳ <b>Válido até:</b> {pix_expiration_label(deposit)}\n\n"
        "Escaneie o QR Code para pagar. O bot verifica automaticamente a cada 30 segundos; "
        "se quiser, use o botão para consultar agora.\n"
        "Cancelar volta ao menu, mas a cobrança continua ativa na ROASClub até expirar."
    )


def pix_qr_png(payload):
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def pix_creation_pending_text():
    return (
        "⚠️ <b>Não foi possível confirmar a criação do Pix</b>\n\n"
        "A solicitação pode ter sido processada pela ROASClub. Não faça outra cobrança agora. "
        "Confira a transação no painel da ROASClub usando a referência informada ou chame o suporte."
    )


def pix_confirmed_text(deposit, balance=None):
    credit = Decimal(deposit["amount_cents"]) / 100
    text = (
        "✅ <b>Recarga Pix confirmada</b>\n\n"
        f"💰 Saldo adicionado: <b>{validators.money(credit)}</b>"
    )
    if balance is not None:
        text += f"\n🏦 Saldo atual: <b>{validators.money(balance)}</b>"
    return text


def order_history_view(user_id, index):
    orders = storage.user_orders(user_id)
    if not orders:
        return "Você ainda não realizou compras.", keyboards.back_menu()
    if index < 0 or index >= len(orders):
        raise ValueError("Pedido do histórico não encontrado.")

    order = orders[index]
    if not order["encrypted_payload"]:
        raise ValueError("Os dados de acesso deste pedido não estão disponíveis.")
    payload = json.loads(security.decrypt_text(order["encrypted_payload"]))
    site = str(payload["url"])
    login = str(payload["login"])
    password = str(payload["password"])
    text = (
        f"🧾 <b>DETALHES DA COMPRA</b>  ·  <i>Compra {index + 1} de {len(orders)}</i>\n\n"
        f"📦 <b>{escape(order['name'])}</b>\n"
        "\n"
        f"🌐 <b>Site:</b>\n<code>{escape(site)}</code>\n\n"
        f"👤 <b>Login:</b>\n<code>{escape(login)}</code>\n\n"
        f"🔑 <b>Senha:</b>\n<code>{escape(password)}</code>"
    )
    rows = []
    parsed_site = urllib.parse.urlsplit(site)
    if parsed_site.scheme in {"http", "https"} and parsed_site.netloc:
        rows.append([{"text": "🌐 Acessar site", "url": site}])

    credential_buttons = []
    if 0 < len(login) <= 256:
        credential_buttons.append({"text": "📋 Copiar login", "copy_text": {"text": login}})
    if 0 < len(password) <= 256:
        credential_buttons.append({"text": "📋 Copiar senha", "copy_text": {"text": password}})
    if credential_buttons:
        rows.append(credential_buttons)

    navigation = [
        {"text": "⬅️", "callback_data": f"history:{max(0, index - 1)}"},
        {"text": f"📄 {index + 1}/{len(orders)}", "callback_data": "history:page"},
        {"text": "➡️", "callback_data": f"history:{min(len(orders) - 1, index + 1)}"},
    ]
    rows.append(navigation)
    rows.append([{"text": "🏠 Menu principal", "callback_data": "home"}])
    return text, keyboards.inline(rows)


def home_text(user_id):
    user = storage.get_user(user_id) or {"balance": "0.00"}
    lines = [
        "📦 <b>Seja bem-vindo(a) a maior loja de logins do telegram!</b>\n",
        "🛍 | <b>Qualidade:</b> Logins",
        "✅ | <b>Garantia:</b> Acesso garantido.\n",
    ]
    if config.STORE_CHANNEL:
        lines.append(f"🔔 | <b>Canal: @{escape(config.STORE_CHANNEL)}</b>")
    if config.SUPPORT_USERNAME:
        lines.append(f"👤 | <b>Suporte: @{escape(config.SUPPORT_USERNAME)}</b>")
    if config.STORE_CHANNEL or config.SUPPORT_USERNAME:
        lines[-1] += "\n"
    lines.extend([
        "<b>🏦 Carteira:</b>",
        f"<b> ├ ID:</b> <code>{user_id}</code>",
        f"<b> ├💰 Saldo: {validators.money(user['balance'])}</b>",
    ])
    return "\n".join(lines)


def license_message_text(product_name, encrypted_payload):
    payload = json.loads(security.decrypt_text(encrypted_payload))
    return (
        "✅ <b>COMPRA CONFIRMADA</b>\n\n"
        f"📦 <b>{escape(product_name)}</b>\n\n"
        "🔐 <b>Dados de acesso</b>\n\n"
        f"🌐 <b>Site:</b>\n<code>{escape(str(payload['url']))}</code>\n\n"
        f"👤 <b>Login:</b>\n<code>{escape(str(payload['login']))}</code>\n\n"
        f"🔑 <b>Senha:</b>\n<code>{escape(str(payload['password']))}</code>"
    )
