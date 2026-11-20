"""Teclados (botões inline) do bot."""

from . import config, validators


def inline(rows):
    return {"inline_keyboard": rows}


def main_menu(admin=False):
    rows = [
        [{"text": "🛒 Logins", "callback_data": "catalog"}],
        [{"text": "👤 Meu perfil", "callback_data": "profile"},
         {"text": "💰 Adicionar saldo", "callback_data": "wallet:deposit"}],
    ]
    if config.SUPPORT_USERNAME:
        rows.append([{"text": "💬 Suporte", "url": f"https://t.me/{config.SUPPORT_USERNAME}"}])
    if admin:
        rows.append([{"text": "⚙️ Painel administrativo", "callback_data": "admin"}])
    return inline(rows)


def profile_menu(referral_link=None):
    row = [{"text": "📦 Minhas compras", "callback_data": "history"}]
    if referral_link:
        row.append({"text": "📋 Copiar link", "copy_text": {"text": referral_link}})
    return inline([row, [{"text": "🔙 Voltar", "callback_data": "home"}]])


def profile_back():
    return inline([[{"text": "🔙 Voltar ao perfil", "callback_data": "profile"}]])


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


CATALOG_PAGE_SIZE = 20  # 10 linhas × 2 colunas


def catalog_page_count(products):
    return max(1, -(-len(products) // CATALOG_PAGE_SIZE))


def product_buttons(products, page=0):
    pages = catalog_page_count(products)
    page = min(max(page, 0), pages - 1)
    rows = [
        [{"text": "ℹ️ Como Funciona? (Leia Antes)", "callback_data": "howto"}],
        [{"text": "🔍 Buscar Login", "switch_inline_query_current_chat": ""}],
    ]
    chunk = products[page * CATALOG_PAGE_SIZE:(page + 1) * CATALOG_PAGE_SIZE]
    for start in range(0, len(chunk), 2):
        rows.append([
            {"text": f"{product['name']} | {validators.money(product['price'])}",
             "callback_data": f"product:{product['id']}"}
            for product in chunk[start:start + 2]
        ])
    navigation = []
    if page > 0:
        navigation.append({"text": "◀️ Anterior", "callback_data": f"catalog:{page - 1}"})
    navigation.append({"text": f"📄 {page + 1}/{pages}", "callback_data": "catalog:page"})
    if page < pages - 1:
        navigation.append({"text": "Próxima ▶️", "callback_data": f"catalog:{page + 1}"})
    rows.append(navigation)
    rows.append([{"text": "🔙 Voltar", "callback_data": "home"}])
    return inline(rows)


def howto_menu():
    rows = [[{"text": "🛍 Ver produtos", "callback_data": "catalog"}]]
    if config.SUPPORT_USERNAME:
        rows.append([{"text": "💬 Suporte", "url": f"https://t.me/{config.SUPPORT_USERNAME}"}])
    rows.append([{"text": "🔙 Voltar", "callback_data": "catalog"}])
    return inline(rows)


def product_logins(product_id, index, total, access_id=None):
    """Rolagem entre os logins do produto + Comprar / Cancelar."""
    rows = []
    if total > 1:
        rows.append([
            {"text": "⬅️", "callback_data": f"product:{product_id}:{(index - 1) % total}"},
            {"text": f"🔐 {index + 1}/{total}", "callback_data": "catalog:page"},
            {"text": "➡️", "callback_data": f"product:{product_id}:{(index + 1) % total}"},
        ])
    if total and access_id is not None:
        rows.append([
            {"text": "✅ Comprar", "callback_data": f"buy:{product_id}:{access_id}"},
            {"text": "❌ Cancelar", "callback_data": "catalog"},
        ])
    else:
        rows.append([{"text": "❌ Cancelar", "callback_data": "catalog"}])
    return inline(rows)


def admin_menu():
    return inline([
        [{"text": "➕ Adicionar estoque", "callback_data": "adm:stock"}],
        [{"text": "📦 Gerenciar produtos", "callback_data": "adm:products"},
         {"text": "🆕 Cadastrar produto", "callback_data": "adm:add_product"}],
        [{"text": "💰 Adicionar saldo", "callback_data": "adm:add_balance"},
         {"text": "📨 Reenviar entrega", "callback_data": "adm:resend"}],
        [{"text": "⚙️ Configurações (bônus)", "callback_data": "adm:cfg"}],
        [{"text": "⬅️ Menu principal", "callback_data": "home"}]
    ])


def admin_settings(settings):
    welcome_toggle = (
        {"text": "⏸ Desativar", "callback_data": "adm:cfg:welcome_off"} if settings["welcome_bonus"] > 0
        else {"text": "▶️ Ativar (R$ 10,00)", "callback_data": "adm:cfg:welcome_on"}
    )
    referral_toggle = (
        {"text": "⏸ Desativar", "callback_data": "adm:cfg:ref_off"} if settings["referral_bonus"] > 0
        else {"text": "▶️ Ativar (R$ 5,00)", "callback_data": "adm:cfg:ref_on"}
    )
    rule = "📌 Pagar: no 1º depósito" if settings["referral_requires_deposit"] else "📌 Pagar: ao entrar pelo link"
    return inline([
        [{"text": "🎁 Alterar boas-vindas", "callback_data": "adm:cfg:welcome"}, welcome_toggle],
        [{"text": "🤝 Alterar indicação", "callback_data": "adm:cfg:ref"}, referral_toggle],
        [{"text": f"{rule} (trocar)", "callback_data": "adm:cfg:ref_mode"}],
        [{"text": "🖼 Imagens das telas", "callback_data": "adm:cfg:images"},
         {"text": "📝 Avisos do menu", "callback_data": "adm:cfg:home_text"}],
        [{"text": "📢 Definir canal de logs", "callback_data": "adm:cfg:logs"}],
        [{"text": "🧪 Enviar teste", "callback_data": "adm:cfg:logs_test"},
         {"text": "⏸ Desligar logs", "callback_data": "adm:cfg:logs_off"}],
        [{"text": "⬅️ Painel admin", "callback_data": "admin"}],
    ])


def screen_images_menu(screens):
    """screens: lista de (chave, nome) das telas que podem ter imagem."""
    rows = []
    for index in range(0, len(screens), 2):
        rows.append([{"text": label, "callback_data": f"adm:cfg:img:{key}"} for key, label in screens[index:index + 2]])
    rows.append([{"text": "⬅️ Configurações", "callback_data": "adm:cfg"}])
    return inline(rows)


def admin_images_back():
    return inline([[{"text": "❌ Cancelar", "callback_data": "adm:cfg:images"}]])


def admin_settings_back():
    return inline([[{"text": "❌ Cancelar", "callback_data": "adm:cfg"}]])


def admin_picker(products, page=0, mode="products", filtered=False):
    """Lista de produtos do admin em 2 colunas. mode='stock' abre direto a adição de acessos."""
    prefix = "adm:stock" if mode == "stock" else "adm:products"
    pages = catalog_page_count(products)
    page = min(max(page, 0), pages - 1)
    rows = []
    chunk = products[page * CATALOG_PAGE_SIZE:(page + 1) * CATALOG_PAGE_SIZE]
    for start in range(0, len(chunk), 2):
        row = []
        for product in chunk[start:start + 2]:
            status = "" if product["active"] else "⏸ "
            target = f"adm:p:{product['id']}:add" if mode == "stock" else f"adm:p:{product['id']}"
            row.append({"text": f"{status}{product['name']} ({product['stock']})", "callback_data": target})
        rows.append(row)
    if not filtered and pages > 1:
        navigation = []
        if page > 0:
            navigation.append({"text": "◀️", "callback_data": f"{prefix}:{page - 1}"})
        navigation.append({"text": f"📄 {page + 1}/{pages}", "callback_data": "catalog:page"})
        if page < pages - 1:
            navigation.append({"text": "▶️", "callback_data": f"{prefix}:{page + 1}"})
        rows.append(navigation)
    if filtered:
        rows.append([{"text": "📋 Ver todos", "callback_data": prefix}])
    rows.append([{"text": "🆕 Cadastrar produto", "callback_data": "adm:add_product"}])
    rows.append([{"text": "⬅️ Painel admin", "callback_data": "admin"}])
    return inline(rows)


def admin_products(products):
    return admin_picker(products, 0, "products")


def stock_add_menu(product_id):
    return inline([
        [{"text": "✅ Concluir", "callback_data": f"adm:p:{product_id}"}],
        [{"text": "➕ Estoque de outro produto", "callback_data": "adm:stock"}],
    ])


def admin_product(product, prev_id=None, next_id=None):
    pid = product["id"]
    toggle = "⏸ Pausar vendas" if product["active"] else "▶️ Ativar vendas"
    navigation = []
    if prev_id is not None:
        navigation.append({"text": "◀️ Anterior", "callback_data": f"adm:p:{prev_id}"})
    if next_id is not None:
        navigation.append({"text": "Próximo ▶️", "callback_data": f"adm:p:{next_id}"})
    return inline(([navigation] if navigation else []) + [
        [{"text": f"👁 Ver acessos ({product['stock']})", "callback_data": f"adm:p:{pid}:v:0"}],
        [{"text": "➕ Adicionar acessos", "callback_data": f"adm:p:{pid}:add"}],
        [{"text": "➖ Remover 1", "callback_data": f"adm:p:{pid}:rm1"},
         {"text": "🧹 Esvaziar estoque", "callback_data": f"adm:p:{pid}:clear"}],
        [{"text": "💵 Preço", "callback_data": f"adm:p:{pid}:price"},
         {"text": "✏️ Nome", "callback_data": f"adm:p:{pid}:name"},
         {"text": "📝 Descrição", "callback_data": f"adm:p:{pid}:desc"}],
        [{"text": "🖼 Imagem", "callback_data": f"adm:p:{pid}:img"}, {"text": toggle, "callback_data": f"adm:p:{pid}:toggle"}],
        [{"text": "🗑 Excluir produto", "callback_data": f"adm:p:{pid}:del"}],
        [{"text": "🔄 Atualizar", "callback_data": f"adm:p:{pid}"},
         {"text": "⬅️ Produtos", "callback_data": "adm:products"}],
    ])


def access_viewer(product_id, index, total, access_id, credentials=None):
    rows = []
    if credentials:
        site = credentials.get("url") or ""
        if site.startswith(("http://", "https://")) and len(site) <= 2048:
            rows.append([{"text": "🌐 Abrir site", "url": site}])
        copy = []
        for label, value in (("📋 Login", credentials.get("login")), ("📋 Senha", credentials.get("password"))):
            if value and len(str(value)) <= 256:
                copy.append({"text": label, "copy_text": {"text": str(value)}})
        if copy:
            rows.append(copy)
    rows.append([
        {"text": "⏮", "callback_data": f"adm:p:{product_id}:v:0"},
        {"text": "⬅️", "callback_data": f"adm:p:{product_id}:v:{max(0, index - 1)}"},
        {"text": f"📄 {index + 1}/{total}", "callback_data": "catalog:page"},
        {"text": "➡️", "callback_data": f"adm:p:{product_id}:v:{min(total - 1, index + 1)}"},
        {"text": "⏭", "callback_data": f"adm:p:{product_id}:v:{total - 1}"},
    ])
    rows.append([{"text": "🗑 Remover este acesso", "callback_data": f"adm:p:{product_id}:xa:{access_id}:{index}"}])
    rows.append([{"text": "➕ Adicionar acessos", "callback_data": f"adm:p:{product_id}:add"},
                 {"text": "⬅️ Produto", "callback_data": f"adm:p:{product_id}"}])
    return inline(rows)


def access_remove_confirm(product_id, access_id, index):
    return inline([
        [{"text": "✅ Sim, remover", "callback_data": f"adm:p:{product_id}:xa!:{access_id}:{index}"}],
        [{"text": "❌ Não", "callback_data": f"adm:p:{product_id}:v:{index}"}],
    ])


def admin_confirm(product_id, action, label):
    return inline([
        [{"text": f"✅ Sim, {label}", "callback_data": f"adm:p:{product_id}:{action}!"}],
        [{"text": "❌ Não", "callback_data": f"adm:p:{product_id}"}],
    ])


def admin_product_back(product_id, done_label="⬅️ Voltar ao produto"):
    return inline([[{"text": done_label, "callback_data": f"adm:p:{product_id}"}]])
