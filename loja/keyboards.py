"""Teclados (botões inline) do bot."""

from . import config


def inline(rows):
    return {"inline_keyboard": rows}


def main_menu(admin=False):
    rows = [
        [{"text": "🛍 Produtos", "callback_data": "catalog"}],
        [{"text": "👤 Meu perfil", "callback_data": "profile"},
         {"text": "📦 Minhas compras", "callback_data": "history"}],
        [{"text": "💰 Adicionar saldo", "callback_data": "wallet:deposit"}],
    ]
    if config.SUPPORT_USERNAME:
        rows.append([{"text": "💬 Suporte", "url": f"https://t.me/{config.SUPPORT_USERNAME}"}])
    if admin:
        rows.append([{"text": "⚙️ Painel administrativo", "callback_data": "admin"}])
    return inline(rows)


def back_menu():
    return inline([[{"text": "⬅️ Menu principal", "callback_data": "home"}]])


def pix_back_menu():
    return inline([[{"text": "⬅️ Voltar ao menu", "callback_data": "home"}]])


def pix_deposit_keyboard(deposit):
    rows = []
    pix_code = deposit.get("pix_code") or ""
    if 0 < len(pix_code) <= 256:
        rows.append([{"text": "📋 Copiar código Pix", "copy_text": {"text": pix_code}}])
    rows.append([{"text": "🔄 Verificar pagamento", "callback_data": f"pix:check:{deposit['id']}"}])
    rows.append([{"text": "❌ Cancelar", "callback_data": "home"}])
    return inline(rows)


def pix_creation_pending_keyboard():
    rows = []
    if config.SUPPORT_USERNAME:
        rows.append([{"text": "💬 Chamar suporte", "url": f"https://t.me/{config.SUPPORT_USERNAME}"}])
    rows.append([{"text": "🏠 Menu principal", "callback_data": "home"}])
    return inline(rows)


def purchase_actions():
    rows = []
    if config.SUPPORT_USERNAME:
        rows.append([{"text": "💬 Chamar suporte", "url": f"https://t.me/{config.SUPPORT_USERNAME}"}])
    rows.append([{"text": "⬅️ Voltar ao menu principal", "callback_data": "home"}])
    return inline(rows)


def purchase_complete_menu():
    return inline([[{"text": "⬅️ Voltar ao menu", "callback_data": "home"}]])


def product_buttons(products):
    rows = []
    for product in products:
        rows.append([{
            "text": f"{product['name']} — R$ {product['price']:.2f} | Estoque: {product['stock']}",
            "callback_data": f"product:{product['id']}"
        }])
    rows.append([{"text": "⬅️ Menu principal", "callback_data": "home"}])
    return inline(rows)


def confirm_purchase(product_id):
    return inline([
        [{"text": "✅ Confirmar compra", "callback_data": f"buy:{product_id}"}],
        [{"text": "❌ Cancelar", "callback_data": "catalog"}]
    ])


def admin_menu():
    return inline([
        [{"text": "➕ Cadastrar produto", "callback_data": "adm:add_product"}],
        [{"text": "🔐 Adicionar licenças ao estoque", "callback_data": "adm:add_license"}],
        [{"text": "💰 Adicionar saldo", "callback_data": "adm:add_balance"}],
        [{"text": "📨 Reenviar entrega pendente", "callback_data": "adm:resend"}],
        [{"text": "⬅️ Menu principal", "callback_data": "home"}]
    ])
