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

from . import config, keyboards, security, storage, telegram_api, validators


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
        return "📦 Você ainda não realizou compras.", keyboards.profile_back()
    if index < 0 or index >= len(orders):
        raise ValueError("Pedido do histórico não encontrado.")

    order = orders[index]
    if not order["encrypted_payload"]:
        raise ValueError("Os dados de acesso deste pedido não estão disponíveis.")
    payload = json.loads(security.decrypt_text(order["encrypted_payload"]))
    site = str(payload.get("url") or "")
    login = str(payload["login"])
    password = str(payload["password"])
    text = (
        f"🧾 <b>DETALHES DA COMPRA</b>  ·  <i>Compra {index + 1} de {len(orders)}</i>\n\n"
        f"📦 <b>{escape(order['name'])}</b>\n"
        "\n"
        + (f"🌐 <b>Site:</b>\n<code>{escape(site)}</code>\n\n" if site else "")
        + f"👤 <b>Login:</b>\n<code>{escape(login)}</code>\n\n"
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
    rows.append([{"text": "🔙 Voltar ao perfil", "callback_data": "profile"},
                 {"text": "🏠 Menu", "callback_data": "home"}])
    return text, keyboards.inline(rows)


def referral_link(user_id):
    try:
        username = telegram_api.bot_username()
    except Exception as exc:
        print("Não foi possível obter o @ do bot para o link de indicação:", exc)
        return None
    return f"https://t.me/{username}?start={user_id}" if username else None


def profile_view(user_id):
    user = storage.get_user(user_id) or {"balance": "0.00"}
    referrals = storage.referral_stats(user_id)
    purchases = storage.purchase_stats(user_id)
    settings = storage.get_settings()
    link = referral_link(user_id)
    name = escape(user.get("first_name") or "—")
    username = f" (@{escape(user['username'])})" if user.get("username") else ""
    if settings["referral_bonus"] <= 0:
        how_it_pays = "indicação sem bônus no momento"
    elif settings["referral_requires_deposit"]:
        how_it_pays = "pago quando ele fizer o 1º depósito"
    else:
        how_it_pays = "pago assim que ele entrar pelo link"
    lines = [
        "👤 <b>MEU PERFIL</b>",
        "",
        "📋 <b>Informações pessoais</b>",
        f" ├ 👤 Nome: <b>{name}</b>{username}",
        f" ├ 🆔 ID: <code>{user_id}</code>",
        f" └ 📅 Cliente desde: <b>{_date_label(user.get('created_at'))[:10]}</b>",
        "",
        "💼 <b>Carteira</b>",
        f" ├ 💸 Saldo: <b>{validators.money(user['balance'])}</b>",
        f" ├ 🛍 Compras: <b>{purchases['count']}</b>",
        f" └ 💳 Total gasto: <b>{validators.money(purchases['spent'])}</b>",
        "",
        "🤝 <b>Indique e ganhe</b>",
        f" ├ 🎁 Ganhe <b>{validators.money(settings['referral_bonus'])}</b> por indicado",
        f" │   <i>({how_it_pays})</i>",
        f" ├ 👥 Indicados: <b>{referrals['count']}</b> ({referrals['active']} já depositaram)",
        f" └ 💰 Total ganho: <b>{validators.money(referrals['earnings'])}</b>",
    ]
    if link:
        lines += [
            "",
            "🔗 <b>Seu link de indicação:</b>",
            f"<code>{escape(link)}</code>",
            "<i>Toque em 📋 Copiar link e envie para quem quiser indicar. "
            "Os ganhos caem direto no seu saldo.</i>",
        ]
    return "\n".join(lines), keyboards.profile_menu(link)


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


def delivery_view(product_name, encrypted_payload, price=None, balance=None):
    """Tela de entrega da compra: texto + botões (abrir site, copiar login/senha, voltar)."""
    payload = json.loads(security.decrypt_text(encrypted_payload))
    site = str(payload.get("url") or "")
    login = str(payload["login"])
    password = str(payload["password"])
    lines = ["✅ <b>COMPRA CONFIRMADA</b>", ""]
    if price is not None:
        lines.append(f"- Preço: <b>{validators.money(price)}</b>")
    if balance is not None:
        lines.append(f"- Novo Saldo: <b>{validators.money(balance)}</b>")
    if price is not None or balance is not None:
        lines.append("")
    parsed_site = urllib.parse.urlsplit(site if "://" in site else f"https://{site}")
    site_url = (
        parsed_site.geturl()
        if site and parsed_site.scheme in {"http", "https"} and parsed_site.netloc and " " not in site
        else None
    )
    lines += [f"📦 <b>{escape(product_name)}</b>", "", "🔐 <b>Dados de acesso:</b>", ""]
    if site:
        lines += ["🌐 <b>Site:</b>", f"<code>{escape(site_url or site)}</code>", ""]
    lines += ["👤 <b>Login:</b>", f"<code>{escape(login)}</code>", ""]
    lines += ["🔑 <b>Senha:</b>", f"<code>{escape(password)}</code>"]

    rows = []
    if site_url:
        rows.append([{"text": "🌐 Acessar site", "url": site_url}])
    copy_buttons = []
    if 0 < len(login) <= 256:
        copy_buttons.append({"text": "📋 Copiar login", "copy_text": {"text": login}})
    if 0 < len(password) <= 256:
        copy_buttons.append({"text": "📋 Copiar senha", "copy_text": {"text": password}})
    if copy_buttons:
        rows.append(copy_buttons)
    rows.append([{"text": "🔙 Voltar", "callback_data": "home"}])
    return "\n".join(lines), keyboards.inline(rows)


def catalog_text(user_id, products):
    user = storage.get_user(user_id) or {"balance": "0.00"}
    total = sum(product["stock"] for product in products)
    return (
        "💾 <b>Comprar Unitário</b>\n\n"
        "📊 <b>Estoque / vitrine:</b>\n"
        f"- Total de unidades: <b>{total}</b>\n"
        f"- Tipos nesta lista: <b>{len(products)}</b>\n\n"
        "🧾 <b>Seu perfil:</b>\n"
        f" ├👤 ID: <code>{user_id}</code>\n"
        f" └💸 Saldo: <b>{validators.money(user['balance'])}</b>"
    )


CAPTION_LIMIT = 1024  # limite do Telegram para legenda de foto


def _login_info_lines(access):
    orders = access.get("orders")
    total = access.get("total")
    return [
        "📋 <b>Informações do login</b>",
        f" ├ 🧾 Quantidade de pedidos: <b>{orders if orders is not None else '—'}</b>",
        f" └ 💰 Valor total dos pedidos: <b>{validators.money(total) if total is not None else '—'}</b>",
    ]


def product_login_text(product, accesses, index, caption=False):
    """Tela do produto para o cliente: um login por vez, com 'Login X de Y' no topo."""
    total = len(accesses)
    position = f"🔐 <b>Login {index + 1} de {total}</b>" if total else "🔐 <b>Sem logins</b>"
    raw_description = product["description"] or ""
    if caption and len(raw_description) > 600:
        raw_description = raw_description[:600].rstrip() + "…"
    lines = [f"📦 <b>{escape(product['name'])}</b>   ·   {position}"]
    if raw_description:
        lines += ["", escape(raw_description)]
    lines.append("")
    if total:
        lines += _login_info_lines(accesses[index])
        lines += ["", f"💲 Preço: <b>{validators.money(product['price'])}</b>"]
    else:
        lines += [f"💲 Preço: <b>{validators.money(product['price'])}</b>", "",
                  "⚠️ Nenhum login disponível no momento. Volte mais tarde!"]
    return "\n".join(lines)


def how_it_works_text():
    support = f" com @{escape(config.SUPPORT_USERNAME)}" if config.SUPPORT_USERNAME else ""
    return (
        "ℹ️ <b>Como funciona?</b>\n\n"
        "1️⃣ <b>Adicione saldo</b> pelo menu principal (💰 Adicionar saldo). A recarga é via Pix "
        "e cai na hora após a confirmação.\n\n"
        "2️⃣ <b>Escolha o login</b> na lista ou toque em 🔍 <b>Buscar Login</b> e digite o nome.\n\n"
        "3️⃣ <b>Confirme a compra.</b> O valor sai do seu saldo e os dados de acesso "
        "(site, login e senha) chegam na mesma hora.\n\n"
        "4️⃣ Suas compras ficam salvas em 📦 <b>Minhas compras</b>.\n\n"
        f"⚠️ Teve algum problema com o acesso? Fale{support} e informe o número do pedido."
    )


def admin_home_text(products):
    total = sum(product["stock"] for product in products)
    on_sale = sum(1 for product in products if product["active"])
    sold_out = sum(1 for product in products if product["active"] and not product["stock"])
    return (
        "⚙️ <b>Painel administrativo</b>\n\n"
        "📊 <b>Resumo do estoque</b>\n"
        f" ├ Produtos: <b>{len(products)}</b> ({on_sale} à venda)\n"
        f" ├ Unidades em estoque: <b>{total}</b>\n"
        f" └ Esgotados: <b>{sold_out}</b>\n\n"
        "➕ <b>Adicionar estoque</b>: escolha o produto e envie os acessos.\n"
        "📦 <b>Gerenciar produtos</b>: preço, nome, remover estoque, pausar ou excluir."
    )


def admin_settings_text(settings, note=None):
    welcome = settings["welcome_bonus"]
    referral = settings["referral_bonus"]
    welcome_status = f"<b>{validators.money(welcome)}</b> 🟢 ativo" if welcome > 0 else "⏸ desativado"
    referral_status = f"<b>{validators.money(referral)}</b> 🟢 ativo" if referral > 0 else "⏸ desativado"
    rule = "no 1º depósito Pix do indicado" if settings["referral_requires_deposit"] else "assim que ele entra pelo link"
    channel = settings.get("log_channel_id") or config.LOG_CHANNEL_ID
    if channel:
        source = "" if settings.get("log_channel_id") else " (do .env)"
        log_status = f"🟢 Enviando para <code>{escape(str(channel))}</code>{source}"
    else:
        log_status = "⏸ Não configurado"
    text = (
        "⚙️ <b>Configurações</b>\n\n"
        "🎁 <b>Bônus de boas-vindas</b>\n"
        f" ├ Valor: {welcome_status}\n"
        " └ Saldo dado a cada usuário <b>novo</b> no primeiro acesso.\n\n"
        "🤝 <b>Bônus de indicação</b>\n"
        f" ├ Valor: {referral_status}\n"
        f" └ Pago a quem indicou, uma vez por indicado, <b>{rule}</b>.\n\n"
        "📢 <b>Canal de logs</b>\n"
        f" └ {log_status}\n\n"
        "<i>As mudanças valem na hora, para os próximos usuários.</i>"
    )
    return f"{note}\n\n{text}" if note else text


def admin_picker_text(products, mode="products", search=None):
    title = "➕ <b>Adicionar estoque</b>" if mode == "stock" else "📦 <b>Gerenciar produtos</b>"
    if not products and search is None:
        return f"{title}\n\nNenhum produto cadastrado ainda. Toque em 🆕 Cadastrar produto."
    action = "receber os acessos" if mode == "stock" else "gerenciar"
    lines = [title, ""]
    if search is not None:
        lines.append(f"🔍 Resultado para <b>{escape(search)}</b>: {len(products)} produto(s).")
        if len(products) > 20:
            lines.append("Mostrando os 20 primeiros. Digite mais do nome para filtrar.")
        lines.append("")
    lines.append(f"Toque no produto para {action}.")
    lines.append("Ou <b>digite o nome ou o ID</b> do produto para buscar.")
    lines.append("\nO número entre parênteses é o estoque atual · ⏸ = vendas pausadas")
    return "\n".join(lines)


def admin_products_text(products):
    return admin_picker_text(products, "products")


def admin_product_text(product, note=None):
    status = "🟢 À venda" if product["active"] else "⏸ Vendas pausadas"
    stock_line = f"<b>{product['stock']}</b>" if product["stock"] else "<b>0</b> ⚠️ esgotado"
    text = (
        f"⚙️ <b>Produto #{product['id']}</b>\n\n"
        f"📦 <b>{escape(product['name'])}</b>\n"
        f"{escape(product['description'] or '—')}\n\n"
        f"💲 Preço: <b>{validators.money(product['price'])}</b>\n"
        f"📊 Estoque: {stock_line}\n"
        f"🖼 Imagem: {'✅ definida' if product.get('image') else '— nenhuma'}\n"
        f"Status: {status}"
    )
    if note:
        text = f"{note}\n\n{text}"
    return text


def _date_label(value):
    try:
        return datetime.fromisoformat(str(value)).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return "—"


def access_view_text(product, access, index, total, credentials=None, note=None):
    position = "▶️ próximo a ser vendido" if index == 0 else f"{index + 1}º da fila de venda"
    lines = [
        f"🔐 <b>Acesso {index + 1} de {total}</b> · {escape(product['name'])}",
        f"<i>#{access['id']} · adicionado em {_date_label(access.get('criado_em'))} · {position}</i>\n",
    ]
    if credentials is None:
        lines.append("⚠️ Não foi possível ler este acesso (ele foi criptografado em outro computador ou conta do Windows).")
    else:
        site = str(credentials.get("url") or "")
        lines.append(f"🌐 <b>Site:</b>\n<code>{escape(site)}</code>\n" if site else "🌐 <b>Site:</b> —\n")
        lines.append(f"👤 <b>Login:</b>\n<code>{escape(str(credentials.get('login', '')))}</code>\n")
        lines.append(f"🔑 <b>Senha:</b>\n<code>{escape(str(credentials.get('password', '')))}</code>")
    total = access.get("valor_total")
    lines += [""] + _login_info_lines({
        "orders": access.get("pedidos"), "total": Decimal(total) if total is not None else None,
    })
    text = "\n".join(lines)
    return f"{note}\n\n{text}" if note else text


def stock_add_prompt(product, added=None, errors=()):
    lines = [f"➕ <b>Adicionar acessos — {escape(product['name'])}</b>\n"]
    if added is not None:
        lines.append(f"✅ {added} acesso(s) adicionado(s).")
    if errors:
        lines.append("⚠️ Linhas ignoradas:")
        lines.extend(f"• {escape(error)}" for error in errors[:15])
        if len(errors) > 15:
            lines.append(f"• … e mais {len(errors) - 15}")
    if added is not None or errors:
        lines.append("")
    lines.extend([
        f"📊 Estoque atual: <b>{product['stock']}</b>\n",
        "Envie um login por linha (pode mandar vários de uma vez):",
        "<code>site | login | senha | pedidos | valor total</code>\n",
        "<b>pedidos</b> = quantidade de pedidos do login · <b>valor total</b> = soma dos pedidos.",
        "O site é opcional: <code>login | senha | pedidos | valor total</code>\n",
        "Exemplo:",
        "<code>https://site.com | joao@gmail.com | senha123 | 12 | 350,00</code>\n",
        "Continue enviando para adicionar mais. Toque em <b>Concluir</b> quando terminar.",
    ])
    return "\n".join(lines)
