"""Testes de ponta a ponta do bot (pacote loja) com Telegram e ROASClub simulados.

Executar na pasta do projeto:  python -m unittest discover -s tests -v
Nenhuma chamada real é feita à internet; os dados ficam numa pasta temporária.
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

TMP_DIR = tempfile.mkdtemp(prefix="store_bot_test_")
os.environ.update({
    "BOT_TOKEN": "123456789:" + "A" * 35,
    "ADMIN_IDS": "1",
    "SUPPORT_USERNAME": "suporte_teste",
    "STORE_CHANNEL": "canal_teste",
    "DATA_FILE": os.path.join(TMP_DIR, "store.json"),
    "PRODUCTS_FILE": os.path.join(TMP_DIR, "produtos.json"),
    "ROASCLUB_PUBLIC_KEY": "pk_test",
    "ROASCLUB_SECRET_KEY": "sk_test",
    "ROASCLUB_CUSTOMER_NAME": "Loja Exemplo Ltda",
    "ROASCLUB_CUSTOMER_EMAIL": "financeiro@example.com",
    "ROASCLUB_CUSTOMER_CPF": "529.982.247-25",
    "ROASCLUB_CUSTOMER_PHONE": "(11) 99999-8888",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loja import (  # noqa: E402
    app, config, handlers, panels, payments, security, storage, telegram_api, validators,
)
from loja import logs as logs_module  # noqa: E402

ADMIN = 1
USER = 500


class FakeTelegram:
    """Simula a Bot API: guarda as mensagens existentes e as chamadas feitas."""

    def __init__(self):
        self.calls = []
        self.messages = {}  # (chat_id, message_id) -> {"kind": "text"|"photo", "content": str}
        self.next_id = 100
        self.fail_next = {}
        self.unknown_chats = set()
        self.bad_photos = set()
        self.bot_member = {"status": "administrator", "can_post_messages": True}

    def _new(self, chat_id, kind, content):
        self.next_id += 1
        self.messages[(int(chat_id), self.next_id)] = {"kind": kind, "content": content}
        return {"message_id": self.next_id, "chat": {"id": int(chat_id)}}

    def call(self, method, data=None, timeout=45):
        data = dict(data or {})
        self.calls.append((method, data))
        if method in self.fail_next:
            failure = self.fail_next.pop(method)
            raise failure if isinstance(failure, Exception) else RuntimeError(failure)
        if method == "getMe":
            return {"id": 42, "username": "loja_teste_bot"}
        if method in {"deleteWebhook", "answerCallbackQuery", "answerInlineQuery"}:
            return True
        if method == "getChat":
            chat = str(data["chat_id"])
            if chat in self.unknown_chats:
                raise telegram_api.TelegramAPIError("Telegram HTTP 400", 400, "Bad Request: chat not found")
            numeric = chat if chat.lstrip("-").isdigit() else "-1004242"
            return {"id": int(numeric), "type": "channel", "title": "Logs da Loja"}
        if method == "getChatMember":
            return dict(self.bot_member)
        if method == "sendPhoto":  # foto por link
            if data["photo"] in self.bad_photos:
                raise telegram_api.TelegramAPIError(
                    "Telegram HTTP 400", 400, "Bad Request: wrong type of the web page content")
            message = self._new(data["chat_id"], "photo", data["caption"])
            self.messages[(int(data["chat_id"]), message["message_id"])]["photo"] = data["photo"]
            return message
        if method == "sendMessage":
            return self._new(data["chat_id"], "text", data["text"])
        if method == "deleteMessage":
            self.messages.pop((int(data["chat_id"]), int(data["message_id"])), None)
            return True
        if method in {"editMessageText", "editMessageCaption"}:
            key = (int(data["chat_id"]), int(data["message_id"]))
            if key not in self.messages:
                raise RuntimeError("Bad Request: message to edit not found")
            message = self.messages[key]
            expected = "photo" if method == "editMessageCaption" else "text"
            if message["kind"] != expected:
                raise RuntimeError(f"Bad Request: wrong message type for {method}")
            content = data["caption" if expected == "photo" else "text"]
            if message["content"] == content:
                raise RuntimeError("Bad Request: message is not modified")
            message["content"] = content
            return True
        raise AssertionError(f"Método inesperado: {method}")

    def send_photo(self, chat_id, photo_data, caption, reply_markup=None, parse_mode="HTML"):
        assert photo_data.startswith(b"\x89PNG"), "o QR deve ser um PNG"
        self.calls.append(("sendPhoto", {"chat_id": chat_id, "caption": caption}))
        return self._new(chat_id, "photo", caption)

    def methods(self):
        return [method for method, _ in self.calls]

    def last_text(self, chat_id):
        chat_messages = [(mid, m) for (cid, mid), m in self.messages.items() if cid == chat_id]
        return max(chat_messages)[1]["content"] if chat_messages else None


class FakeRoasClub:
    def __init__(self):
        self.created = []
        self.status = "PENDING"
        self.amount_override = None

    def __call__(self, method, path, body=None):
        if method == "POST" and path == "/v1/payment-transaction/create":
            self.created.append(body)
            return {"success": True, "data": {
                "id": "tx_1", "amount": body["amount"], "status": "PENDING",
                "pix": {"qr_code": "00020126580014BR.GOV.BCB.PIX0136teste5204000053039865802BR",
                        "expiration_date": "2030-01-01T12:00:00Z"},
            }}
        if method == "GET" and path == "/v1/payment-transaction/info/tx_1":
            amount = self.amount_override or self.created[-1]["amount"]
            return {"data": {"id": "tx_1", "status": self.status, "amount": amount}}
        raise AssertionError(f"Chamada ROASClub inesperada: {method} {path}")


def fake_encrypt(text):
    return "test:" + base64.b64encode(text.encode()).decode()


def fake_decrypt(encoded):
    assert encoded.startswith("test:")
    return base64.b64decode(encoded[5:]).decode()


class BotTestCase(unittest.TestCase):
    def setUp(self):
        for path in (storage._PATH, storage._PRODUCTS_PATH):
            if path.exists():
                path.unlink()
        panels.ACTIVE_MENUS.clear()
        panels.ACTIVE_MEDIA.clear()
        handlers.ACTIVE_PIX_DEPOSITS.clear()
        handlers.STATES.clear()
        self.tg = FakeTelegram()
        self.roas = FakeRoasClub()
        self._originals = {
            (module, name): getattr(module, name)
            for module, name in (
                (telegram_api, "call"), (telegram_api, "send_photo"), (payments, "roas_api_call"),
                (security, "encrypt_text"), (security, "decrypt_text"),
            )
        }
        telegram_api.call = self.tg.call
        telegram_api.send_photo = self.tg.send_photo
        payments.roas_api_call = self.roas
        security.encrypt_text = fake_encrypt
        security.decrypt_text = fake_decrypt
        # Testes antigos assumem saldo inicial 0; os de bônus ligam o valor explicitamente.
        self._default_settings = dict(storage.DEFAULT_SETTINGS)
        logs_module.ASYNC = False  # envia na hora para os testes conferirem
        storage.DEFAULT_SETTINGS["welcome_bonus"] = "0.00"

    def tearDown(self):
        for (module, name), value in self._originals.items():
            setattr(module, name, value)
        storage.DEFAULT_SETTINGS.clear()
        storage.DEFAULT_SETTINGS.update(self._default_settings)

    # Auxiliares que imitam o que o Telegram envia ao bot
    def send(self, user_id, text):
        handlers.process_update({"update_id": 1, "message": {
            "chat": {"id": user_id}, "from": {"id": user_id, "first_name": "T"}, "text": text,
        }})

    def click(self, user_id, data, message_id=None):
        message_id = message_id or panels.active_menu_message_id(user_id)
        message = {"chat": {"id": user_id}, "message_id": message_id}
        if self.tg.messages.get((user_id, message_id), {}).get("kind") == "photo":
            message["photo"] = [{"file_id": "x"}]
        handlers.process_update({"update_id": 2, "callback_query": {
            "id": "cb", "from": {"id": user_id, "first_name": "T"}, "message": message, "data": data,
        }})

    def screen(self, user_id):
        message_id = panels.active_menu_message_id(user_id)
        return self.tg.messages[(user_id, message_id)]

    def create_product_with_license(self, price="29,90"):
        self.send(ADMIN, "/start")
        self.click(ADMIN, "adm:add_product")
        self.send(ADMIN, "Curso Teste")
        self.send(ADMIN, "Descrição do curso")
        self.send(ADMIN, price)
        self.assertIn("Produto #1 cadastrado", self.screen(ADMIN)["content"])
        self.click(ADMIN, "adm:p:1:add")
        self.send(ADMIN, "https://exemplo.com/login | aluno@exemplo.com | s3nh@<forte> | 12 | 350,00")
        self.assertIn("1 acesso(s) adicionado(s)", self.screen(ADMIN)["content"])
        self.click(ADMIN, "adm:p:1")


class StartupTests(BotTestCase):
    def test_startup_checks_pass_with_valid_config(self):
        self.assertTrue(app.startup_checks())
        self.assertEqual(self.tg.methods()[:2], ["getMe", "deleteWebhook"])

    def test_startup_fails_cleanly_with_bad_token(self):
        self.tg.fail_next["getMe"] = "Telegram HTTP 401: Unauthorized"
        self.assertFalse(app.startup_checks())

    def test_config_errors_detect_placeholders(self):
        original = (config.BOT_TOKEN, config.ADMIN_IDS, config._RAW_ADMIN_IDS)
        try:
            config.BOT_TOKEN, config.ADMIN_IDS, config._RAW_ADMIN_IDS = "TOKEN_DO_BOTFATHER", set(), ["SEU_ID"]
            errors = config.config_errors()
            self.assertEqual(len(errors), 3)
        finally:
            config.BOT_TOKEN, config.ADMIN_IDS, config._RAW_ADMIN_IDS = original


class MenuTests(BotTestCase):
    def test_start_creates_user_and_panel(self):
        self.send(USER, "/start")
        self.assertIsNotNone(storage.get_user(USER))
        content = self.screen(USER)["content"]
        self.assertIn("Saldo: R$ 0,00", content)
        self.assertIn("@canal_teste", content)
        self.assertIn("Acesse sempre via navegador 4G!", content)
        self.assertIn(f"<code>{USER}</code>", content)
        rows = json.loads(self.tg.calls[-1][1]["reply_markup"])["inline_keyboard"]
        self.assertIn("t.me/suporte_teste", json.dumps(rows))  # suporte continua como botão

    def test_admin_command_without_panel_sends_new_message(self):
        self.send(ADMIN, "/admin")
        self.assertIn("Painel administrativo", self.screen(ADMIN)["content"])

    def test_deleted_panel_is_recreated(self):
        self.send(USER, "/start")
        old_id = panels.active_menu_message_id(USER)
        del self.tg.messages[(USER, old_id)]
        self.send(USER, "/cancel")
        self.assertNotEqual(panels.active_menu_message_id(USER), old_id)
        self.assertIn("Carteira", self.screen(USER)["content"])

    def test_clicking_same_button_twice_is_harmless(self):
        self.send(USER, "/start")
        self.click(USER, "profile")
        self.click(USER, "profile")
        self.assertIn("MEU PERFIL", self.screen(USER)["content"])

    def test_non_admin_cannot_open_admin_panel(self):
        self.send(USER, "/start")
        self.click(USER, "admin")
        self.assertIn("não tem permissão", self.screen(USER)["content"])


class PurchaseTests(BotTestCase):
    def test_full_purchase_flow(self):
        self.create_product_with_license("29,90")
        self.send(USER, "/start")
        self.send(ADMIN, f"/saldo {USER} 50,00")
        self.assertEqual(storage.get_user(USER)["balance"], "50.00")

        self.click(USER, "catalog")
        catalog = self.screen(USER)["content"]
        self.assertIn("Comprar Unitário", catalog)
        self.assertIn("Total de unidades: <b>1</b>", catalog)
        self.assertIn(f"ID: <code>{USER}</code>", catalog)
        self.assertIn("Saldo: <b>R$ 50,00</b>", catalog)
        buttons = json.loads(self.tg.calls[-1][1]["reply_markup"])["inline_keyboard"]
        self.assertEqual(buttons[0][0]["callback_data"], "howto")
        self.assertEqual(buttons[1][0]["switch_inline_query_current_chat"], "")
        self.assertEqual(buttons[2][0]["text"], "Curso Teste | R$ 29,90")
        self.click(USER, "product:1")
        self.click(USER, "buy:1")
        delivered = self.screen(USER)["content"]
        self.assertIn("COMPRA CONFIRMADA", delivered)
        self.assertIn("s3nh@&lt;forte&gt;", delivered)  # HTML escapado
        self.assertIn("- Preço: <b>R$ 29,90</b>", delivered)
        self.assertIn("- Novo Saldo: <b>R$ 20,10</b>", delivered)
        self.assertIn(
            "🌐 <b>Site:</b>\n<code>https://exemplo.com/login</code>\n\n"
            "👤 <b>Login:</b>\n<code>aluno@exemplo.com</code>\n\n"
            "🔑 <b>Senha:</b>\n<code>s3nh@&lt;forte&gt;</code>", delivered
        )
        rows = json.loads(self.tg.calls[-1][1]["reply_markup"])["inline_keyboard"]
        self.assertEqual(rows[0][0], {"text": "🌐 Acessar site", "url": "https://exemplo.com/login"})
        self.assertEqual([b["copy_text"]["text"] for b in rows[1]], ["aluno@exemplo.com", "s3nh@<forte>"])
        self.assertEqual(rows[2][0]["callback_data"], "home")
        self.assertEqual(storage.get_user(USER)["balance"], "20.10")
        self.assertEqual(storage.user_orders(USER)[0]["status"], "completed")
        self.assertEqual(storage.list_products()[0]["stock"], 0)

        self.click(USER, "history")
        self.assertIn("Compra 1 de 1", self.screen(USER)["content"])

        storage.add_balance(USER, Decimal("100"))
        self.click(USER, "buy:1")  # sem estoque
        self.assertIn("Estoque esgotado", self.screen(USER)["content"])
        self.assertEqual(storage.get_user(USER)["balance"], "120.10")

    def test_insufficient_balance_does_not_create_order(self):
        self.create_product_with_license("29,90")
        self.send(USER, "/start")
        self.click(USER, "buy:1")
        self.assertIn("Saldo insuficiente", self.screen(USER)["content"])
        self.assertEqual(storage.user_orders(USER), [])
        self.assertEqual(storage.list_products()[0]["stock"], 1)

    def test_admin_input_validation_messages(self):
        self.send(ADMIN, "/start")
        self.click(ADMIN, "adm:add_product")
        self.send(ADMIN, "Produto")
        self.send(ADMIN, "Desc")
        self.send(ADMIN, "abc")
        self.assertIn("duas casas decimais", self.screen(ADMIN)["content"])
        self.send(ADMIN, "10,999")
        self.assertIn("duas casas decimais", self.screen(ADMIN)["content"])
        self.send(ADMIN, "R$ 10,50")
        self.assertEqual(storage.get_product(1)["price"], Decimal("10.50"))
        self.click(ADMIN, "adm:p:1:add")
        self.send(ADMIN, "só uma coisa")
        content = self.screen(ADMIN)["content"]
        self.assertIn("linha 1", content)
        self.assertIn("0 acesso(s) adicionado(s)", content)

    def test_saldo_rejects_unknown_user_and_bad_amount(self):
        self.send(ADMIN, "/start")
        self.send(ADMIN, "/saldo 999 10")
        self.assertIn("Usuário não encontrado", self.screen(ADMIN)["content"])
        self.send(USER, "/start")
        self.send(ADMIN, f"/saldo {USER} -5")
        self.assertEqual(storage.get_user(USER)["balance"], "0.00")

    def test_resend_delivery_to_user(self):
        self.create_product_with_license()
        self.send(USER, "/start")
        storage.add_balance(USER, Decimal("100"))
        original_edit = panels.edit_message

        def failing_edit(chat_id, message_id, text, *args, **kwargs):
            if "COMPRA CONFIRMADA" in text:
                raise RuntimeError("falha simulada")
            return original_edit(chat_id, message_id, text, *args, **kwargs)

        panels.edit_message = failing_edit
        try:
            self.click(USER, "buy:1")
        finally:
            panels.edit_message = original_edit
        self.assertEqual(storage.user_orders(USER)[0]["status"], "delivery_failed")
        self.assertIn("t.me/suporte_teste", json.dumps(self.tg.calls[-1][1], ensure_ascii=False))

        self.click(ADMIN, "adm:resend")
        self.send(ADMIN, "#1")
        self.assertIn("Entrega reenviada", self.screen(ADMIN)["content"])
        self.assertIn("COMPRA CONFIRMADA", self.screen(USER)["content"])
        self.assertEqual(storage.user_orders(USER)[0]["status"], "completed")


class StockAdminTests(BotTestCase):
    def catalog_file(self):
        return json.loads(storage._PRODUCTS_PATH.read_text(encoding="utf-8"))

    def test_admin_home_shows_summary(self):
        self.create_product_with_license()
        storage.create_product("Sem estoque", "", Decimal("5"))
        self.send(ADMIN, "/admin")
        content = self.screen(ADMIN)["content"]
        self.assertIn("Produtos: <b>2</b>", content)
        self.assertIn("Unidades em estoque: <b>1</b>", content)
        self.assertIn("Esgotados: <b>1</b>", content)

    def test_add_stock_button_lists_products_and_opens_entry(self):
        self.create_product_with_license()
        self.click(ADMIN, "admin")
        self.click(ADMIN, "adm:stock")
        buttons = json.loads(self.tg.calls[-1][1]["reply_markup"])["inline_keyboard"]
        self.assertEqual(buttons[0][0], {"text": "Curso Teste (1)", "callback_data": "adm:p:1:add"})
        self.click(ADMIN, "adm:p:1:add")
        self.send(ADMIN, "a | b | 1 | 10")
        self.assertEqual(storage.get_product(1)["stock"], 2)

    def test_add_stock_by_typing_name_or_id(self):
        storage.create_product("Música Premium", "", Decimal("10"))
        storage.create_product("Música Família", "", Decimal("10"))
        storage.create_product("Editor", "", Decimal("10"))
        self.send(ADMIN, "/admin")
        self.click(ADMIN, "adm:stock")
        self.send(ADMIN, "musica")  # 2 resultados: mostra a lista filtrada
        self.assertIn("2 produto(s)", self.screen(ADMIN)["content"])
        self.send(ADMIN, "editor")  # 1 resultado: abre direto
        self.assertIn("Adicionar acessos — Editor", self.screen(ADMIN)["content"])
        self.send(ADMIN, "x | y | 0 | 0\nz | w | 3 | 99,90")
        self.assertEqual(storage.get_product(3)["stock"], 2)

        self.click(ADMIN, "adm:products")
        self.send(ADMIN, "#1")
        self.assertIn("Produto #1", self.screen(ADMIN)["content"])

    def test_new_product_goes_straight_to_stock_entry(self):
        self.send(ADMIN, "/start")
        self.click(ADMIN, "adm:add_product")
        self.send(ADMIN, "Novo")
        self.send(ADMIN, "Desc")
        self.send(ADMIN, "10")
        self.assertIn("Já pode enviar os acessos", self.screen(ADMIN)["content"])
        self.send(ADMIN, "login | senha | 5 | 50")
        self.assertEqual(storage.get_product(1)["stock"], 1)

    def test_view_accesses_navigate_and_remove(self):
        self.create_product_with_license()
        self.click(ADMIN, "adm:p:1:add")
        self.send(ADMIN, "login2 | senha2 | 2 | 20\nhttps://c.com | login3 | senha3 | 3 | 30")
        self.click(ADMIN, "adm:p:1:v:0")
        content = self.screen(ADMIN)["content"]
        self.assertIn("Acesso 1 de 3", content)
        self.assertIn("próximo a ser vendido", content)
        self.assertIn("https://exemplo.com/login", content)
        self.assertIn("aluno@exemplo.com", content)
        self.assertIn("s3nh@&lt;forte&gt;", content)
        rows = json.loads(self.tg.calls[-1][1]["reply_markup"])["inline_keyboard"]
        self.assertEqual(rows[0][0]["url"], "https://exemplo.com/login")
        self.assertEqual(rows[1][1]["copy_text"]["text"], "s3nh@<forte>")

        self.click(ADMIN, "adm:p:1:v:1")
        content = self.screen(ADMIN)["content"]
        self.assertIn("Acesso 2 de 3", content)
        self.assertIn("login2", content)
        self.assertIn("Site:</b> —", content)

        access_id = storage.list_accesses(1)[1]["id"]
        self.click(ADMIN, f"adm:p:1:xa:{access_id}:1")
        self.assertIn("Remover o acesso", self.screen(ADMIN)["content"])
        self.click(ADMIN, f"adm:p:1:xa!:{access_id}:1")
        content = self.screen(ADMIN)["content"]
        self.assertIn("removido. Estoque: 2", content)
        self.assertIn("login3", content)  # passou para o próximo
        self.assertEqual([a["id"] for a in storage.list_accesses(1)].count(access_id), 0)

    def test_product_panel_navigates_between_products(self):
        storage.create_product("Alfa", "", Decimal("1"))
        storage.create_product("Beta", "", Decimal("1"))
        self.send(ADMIN, "/admin")
        self.click(ADMIN, "adm:p:1")
        first_row = json.loads(self.tg.calls[-1][1]["reply_markup"])["inline_keyboard"][0]
        self.assertEqual(first_row, [{"text": "Próximo ▶️", "callback_data": "adm:p:2"}])

    def test_bulk_add_remove_and_clear_stock(self):
        self.create_product_with_license()
        self.click(ADMIN, "adm:p:1:add")
        self.send(ADMIN, "u2 | p2 | 1 | 1\n\nhttps://x.com | u3 | p3 | 2 | 2\nlinha ruim\nu4 | p4 | 3 | 3")
        content = self.screen(ADMIN)["content"]
        self.assertIn("3 acesso(s) adicionado(s)", content)
        self.assertIn("Estoque atual: <b>4</b>", content)
        self.assertIn("linha 4", content)
        self.assertEqual(len(self.catalog_file()["produtos"][0]["acessos"]), 4)
        # acessos ficam criptografados no arquivo
        self.assertNotIn("p3", storage._PRODUCTS_PATH.read_text(encoding="utf-8"))

        self.click(ADMIN, "adm:p:1:rm1")
        self.assertIn("Estoque: 3", self.screen(ADMIN)["content"])
        self.click(ADMIN, "adm:p:1:clear")
        self.assertIn("Remover", self.screen(ADMIN)["content"])
        self.click(ADMIN, "adm:p:1:clear!")
        self.assertIn("3 acesso(s) removido(s)", self.screen(ADMIN)["content"])
        self.assertEqual(storage.get_product(1)["stock"], 0)
        self.click(ADMIN, "adm:p:1:rm1")
        self.assertIn("já está vazio", self.screen(ADMIN)["content"])

    def test_sale_removes_oldest_access_and_history_survives_delete(self):
        self.create_product_with_license()
        self.click(ADMIN, "adm:p:1:add")
        self.send(ADMIN, "segundo | senha2 | 7 | 70")
        self.send(USER, "/start")
        storage.add_balance(USER, Decimal("100"))
        self.click(USER, "buy:1")
        self.assertIn("s3nh@&lt;forte&gt;", self.screen(USER)["content"])  # o mais antigo sai primeiro
        self.assertEqual(storage.get_product(1)["stock"], 1)

        self.click(ADMIN, "adm:p:1:del")
        self.click(ADMIN, "adm:p:1:del!")
        self.assertIsNone(storage.get_product(1, include_inactive=True))
        self.click(USER, "history")
        history = self.screen(USER)["content"]
        self.assertIn("Curso Teste", history)
        self.assertIn("aluno@exemplo.com", history)

    def test_pause_price_and_rename(self):
        self.create_product_with_license()
        self.click(ADMIN, "adm:p:1:toggle")
        self.assertEqual(storage.list_products(), [])
        self.send(USER, "/start")
        self.click(USER, "buy:1")
        self.assertIn("Produto indisponível", self.screen(USER)["content"])
        self.click(ADMIN, "adm:p:1:toggle")
        self.click(ADMIN, "adm:p:1:price")
        self.send(ADMIN, "15")
        self.assertIn("R$ 15,00", self.screen(ADMIN)["content"])
        self.click(ADMIN, "adm:p:1:name")
        self.send(ADMIN, "Curso Novo")
        self.assertEqual(storage.get_product(1)["name"], "Curso Novo")

    def test_hand_edited_file_is_read_live(self):
        storage.list_products()  # cria o arquivo vazio
        catalog = self.catalog_file()
        catalog["produtos"].append({
            "nome": "Feito à mão", "preco": "9,90",
            "acessos": [{"url": "https://a.com", "login": "eu", "senha": "segredo"}],
        })
        storage._PRODUCTS_PATH.write_text(json.dumps(catalog), encoding="utf-8")

        product = storage.list_products()[0]  # sem reiniciar o bot
        self.assertEqual((product["name"], product["price"], product["stock"]), ("Feito à mão", Decimal("9.90"), 1))
        saved = self.catalog_file()["produtos"][0]
        self.assertEqual(saved["id"], 1)
        self.assertNotIn("senha", saved["acessos"][0])  # texto puro foi criptografado
        self.assertTrue(saved["acessos"][0]["dados"].startswith("test:"))

    def test_broken_file_shows_reason_to_admin(self):
        storage._PRODUCTS_PATH.write_text("{ quebrado", encoding="utf-8")
        self.send(ADMIN, "/start")
        self.click(ADMIN, "adm:products")
        self.assertIn("JSON inválido", self.screen(ADMIN)["content"])

    def test_non_admin_cannot_manage_stock(self):
        self.create_product_with_license()
        self.send(USER, "/start")
        self.click(USER, "adm:p:1:clear!")
        self.assertIn("não tem permissão", self.screen(USER)["content"])
        self.assertEqual(storage.get_product(1)["stock"], 1)

    def test_migrates_old_products_from_store_json(self):
        storage._write({
            **json.loads(json.dumps(storage._EMPTY)),
            "users": {str(USER): {"telegram_id": USER, "username": None, "first_name": "T",
                                  "balance": "0.00", "created_at": "x", "updated_at": "x"}},
            "products": [{"id": 7, "name": "Antigo", "description": "", "price": "5.00", "active": True}],
            "licenses": [
                {"id": 1, "product_id": 7, "encrypted_payload": fake_encrypt(json.dumps(
                    {"url": "", "login": "velho", "password": "1"})), "status": "sold"},
                {"id": 2, "product_id": 7, "encrypted_payload": "test:e30=", "status": "available"},
            ],
            "orders": [{"id": 1, "telegram_id": USER, "product_id": 7, "license_id": 1, "amount": "5.00",
                        "status": "completed", "created_at": "x", "delivered_at": "x"}],
        })
        self.assertEqual(storage.get_product(7)["stock"], 1)
        self.assertEqual(storage._read()["products"], [])
        self.assertEqual(storage.user_orders(USER)[0]["name"], "Antigo")


class CatalogTests(BotTestCase):
    def make_products(self, count):
        for number in range(1, count + 1):
            storage.create_product(f"Produto {number:02d}", "", Decimal("20"))

    def last_markup(self):
        return json.loads(self.tg.calls[-1][1]["reply_markup"])["inline_keyboard"]

    def test_two_columns_and_pagination(self):
        self.make_products(25)
        self.send(USER, "/start")
        self.click(USER, "catalog")
        self.assertIn("Tipos nesta lista: <b>25</b>", self.screen(USER)["content"])
        rows = self.last_markup()
        product_rows = rows[2:-2]
        self.assertEqual(len(product_rows), 10)
        self.assertTrue(all(len(row) == 2 for row in product_rows))
        self.assertEqual(product_rows[0][0]["text"], "Produto 01 | R$ 20,00")
        self.assertEqual([b["text"] for b in rows[-2]], ["📄 1/2", "Próxima ▶️"])

        self.click(USER, "catalog:1")
        rows = self.last_markup()
        self.assertEqual(rows[2][0]["text"], "Produto 21 | R$ 20,00")
        self.assertEqual(len(rows[4]), 1)  # 5 itens: 2 + 2 + 1
        self.assertEqual([b["text"] for b in rows[-2]], ["◀️ Anterior", "📄 2/2"])

    def test_how_it_works(self):
        self.make_products(1)
        self.send(USER, "/start")
        self.click(USER, "howto")
        self.assertIn("Como funciona", self.screen(USER)["content"])

    def test_inline_search_and_open_result(self):
        storage.create_product("Música Premium", "Plano anual", Decimal("15"))
        storage.create_product("Editor de Vídeo", "", Decimal("25"))
        handlers.process_update({"update_id": 3, "inline_query": {
            "id": "iq", "from": {"id": USER}, "query": "musica", "offset": "",
        }})
        method, data = self.tg.calls[-1]
        self.assertEqual(method, "answerInlineQuery")
        results = json.loads(data["results"])
        self.assertEqual([r["title"] for r in results], ["Música Premium"])
        self.assertIn("R$ 15,00", results[0]["description"])
        self.assertEqual(results[0]["input_message_content"]["message_text"], "/produto 1")

        handlers.process_update({"update_id": 4, "inline_query": {
            "id": "iq2", "from": {"id": USER}, "query": "xyz", "offset": "",
        }})
        self.assertEqual(json.loads(self.tg.calls[-1][1]["results"])[0]["id"], "none")

        self.send(USER, "/start")
        old_panel = panels.active_menu_message_id(USER)
        self.send(USER, "/produto 1")
        self.assertNotEqual(panels.active_menu_message_id(USER), old_panel)
        self.assertNotIn((USER, old_panel), self.tg.messages)
        self.assertIn("Música Premium", self.screen(USER)["content"])
        self.assertIn("Sem logins", self.screen(USER)["content"])

    def test_inline_search_empty_query_lists_everything_with_offset(self):
        self.make_products(60)
        handlers.process_update({"update_id": 5, "inline_query": {
            "id": "iq", "from": {"id": USER}, "query": "", "offset": "",
        }})
        data = self.tg.calls[-1][1]
        self.assertEqual(len(json.loads(data["results"])), 50)
        self.assertEqual(data["next_offset"], "50")


class ReferralTests(BotTestCase):
    REFERRER = 700
    FRIEND = 800

    def pay_pix(self, user_id, amount):
        self.click(user_id, "wallet:deposit")
        self.send(user_id, amount)
        self.roas.status = "PAID"
        handlers.reconcile_pix_deposits()
        self.roas.status = "PENDING"

    def notifications_to(self, chat_id):
        return [data["text"] for method, data in self.tg.calls
                if method == "sendMessage" and data["chat_id"] == chat_id and "indica" in data["text"].lower()]

    def test_profile_screen_and_buttons(self):
        self.send(USER, "/start")
        self.click(USER, "profile")
        content = self.screen(USER)["content"]
        for expected in ("MEU PERFIL", f"ID: <code>{USER}</code>", "Saldo: <b>R$ 0,00</b>",
                         "Indicados: <b>0</b>", "Ganhe <b>R$ 5,00</b> por indicado",
                         f"https://t.me/loja_teste_bot?start={USER}"):
            self.assertIn(expected, content)
        rows = json.loads(self.tg.calls[-1][1]["reply_markup"])["inline_keyboard"]
        self.assertEqual(rows[0][0]["callback_data"], "history")
        self.assertEqual(rows[0][1]["copy_text"]["text"], f"https://t.me/loja_teste_bot?start={USER}")
        self.assertEqual(rows[1][0]["callback_data"], "home")
        # "Minhas compras" saiu do menu principal
        self.click(USER, "home")
        self.assertNotIn('"history"', json.dumps(self.tg.calls[-1][1].get("reply_markup", "")))

    def test_default_pays_5_on_signup_without_deposit(self):
        self.send(self.REFERRER, "/start")
        self.send(self.FRIEND, f"/start {self.REFERRER}")
        self.assertEqual(storage.get_user(self.REFERRER)["balance"], "5.00")
        self.assertIn("R$ 5,00</b> de bônus", self.notifications_to(self.REFERRER)[0])
        self.pay_pix(self.FRIEND, "50")  # depósito não paga mais nada
        self.assertEqual(storage.get_user(self.REFERRER)["balance"], "5.00")
        self.click(self.REFERRER, "profile")
        content = self.screen(self.REFERRER)["content"]
        self.assertIn("pago assim que ele entrar pelo link", content)
        self.assertIn("Total ganho: <b>R$ 5,00</b>", content)

    def test_referral_pays_5_once_on_first_deposit(self):
        storage.update_settings(referral_requires_deposit=True)
        self.send(self.REFERRER, "/start")
        self.send(self.FRIEND, f"/start {self.REFERRER}")
        self.assertEqual(storage.get_user(self.FRIEND)["referred_by"], self.REFERRER)
        self.assertEqual(storage.get_user(self.REFERRER)["balance"], "0.00")  # ainda sem bônus
        self.assertIn("Novo indicado", self.notifications_to(self.REFERRER)[0])

        self.pay_pix(self.FRIEND, "50")
        self.assertEqual(storage.get_user(self.FRIEND)["balance"], "50.00")  # indicado recebe tudo
        self.assertEqual(storage.get_user(self.REFERRER)["balance"], "5.00")  # só o bônus, sem comissão
        self.assertIn("Bônus: <b>R$ 5,00</b>", self.notifications_to(self.REFERRER)[-1])

        self.roas.created.clear()
        self.pay_pix(self.FRIEND, "25,50")
        handlers.reconcile_pix_deposits()
        self.assertEqual(storage.get_user(self.REFERRER)["balance"], "5.00")  # não paga de novo
        stats = storage.referral_stats(self.REFERRER)
        self.assertEqual((stats["count"], stats["active"], stats["earnings"]), (1, 1, Decimal("5.00")))

        self.click(self.REFERRER, "profile")
        self.assertIn("Total ganho: <b>R$ 5,00</b>", self.screen(self.REFERRER)["content"])

    def test_referral_rejects_self_existing_users_and_unknown_referrer(self):
        self.send(USER, f"/start {USER}")  # a si mesmo
        self.assertIsNone(storage.get_user(USER).get("referred_by"))
        self.send(self.FRIEND, "/start 999999")  # padrinho inexistente
        self.assertIsNone(storage.get_user(self.FRIEND).get("referred_by"))
        self.send(self.REFERRER, "/start")
        self.send(USER, f"/start {self.REFERRER}")  # já era usuário
        self.assertIsNone(storage.get_user(USER).get("referred_by"))
        self.assertEqual(storage.referral_stats(self.REFERRER)["count"], 0)

    def test_admin_credit_does_not_pay_referral_bonus(self):
        storage.update_settings(referral_requires_deposit=True)
        self.send(self.REFERRER, "/start")
        self.send(self.FRIEND, f"/start {self.REFERRER}")
        self.send(ADMIN, f"/saldo {self.FRIEND} 100")
        self.assertEqual(storage.get_user(self.REFERRER)["balance"], "0.00")

    def test_bonus_on_signup_when_configured(self):
        storage.update_settings(referral_requires_deposit=False)
        self.send(self.REFERRER, "/start")
        self.send(self.FRIEND, f"/start {self.REFERRER}")
        self.assertEqual(storage.get_user(self.REFERRER)["balance"], "5.00")
        self.pay_pix(self.FRIEND, "10")
        self.assertEqual(storage.get_user(self.REFERRER)["balance"], "5.00")  # bônus não se repete

    def test_ensure_user_keeps_referral_fields(self):
        self.send(self.REFERRER, "/start")
        self.send(self.FRIEND, f"/start {self.REFERRER}")
        self.send(self.FRIEND, "/start")
        self.assertEqual(storage.get_user(self.FRIEND)["referred_by"], self.REFERRER)


class WelcomeBonusTests(BotTestCase):
    def setUp(self):
        super().setUp()
        storage.DEFAULT_SETTINGS["welcome_bonus"] = "10.00"  # valor padrão real

    def welcome_messages(self, chat_id):
        return [d["text"] for m, d in self.tg.calls
                if m == "sendMessage" and d["chat_id"] == chat_id and "boas-vindas creditado" in d["text"]]

    def test_new_user_gets_10_once(self):
        self.send(USER, "/start")
        self.assertEqual(storage.get_user(USER)["balance"], "10.00")
        messages = self.welcome_messages(USER)
        self.assertEqual(len(messages), 1)
        self.assertIn("Os <b>R$ 10,00</b> de boas-vindas foram creditados na sua conta", messages[0])
        self.assertIn("Saldo atual: <b>R$ 10,00</b>", messages[0])
        self.assertIn("Carteira", self.screen(USER)["content"])  # o menu vem logo depois, embaixo
        self.send(USER, "/start")  # voltar não dá bônus nem mensagem de novo
        self.assertEqual(storage.get_user(USER)["balance"], "10.00")
        self.assertEqual(len(self.welcome_messages(USER)), 1)

    def test_welcome_message_when_first_contact_is_a_button(self):
        self.click(USER, "catalog", message_id=1)
        self.assertEqual(len(self.welcome_messages(USER)), 1)

    def test_no_message_when_bonus_disabled(self):
        storage.update_settings(welcome_bonus="0")
        self.send(USER, "/start")
        self.assertEqual(self.welcome_messages(USER), [])

    def test_admin_changes_and_disables_welcome_bonus(self):
        self.send(ADMIN, "/admin")
        self.click(ADMIN, "adm:cfg")
        self.assertIn("R$ 10,00</b> 🟢 ativo", self.screen(ADMIN)["content"])
        self.click(ADMIN, "adm:cfg:welcome")
        self.send(ADMIN, "7,50")
        self.assertIn("agora é R$ 7,50", self.screen(ADMIN)["content"])
        self.send(USER, "/start")
        self.assertEqual(storage.get_user(USER)["balance"], "7.50")

        self.click(ADMIN, "adm:cfg:welcome_off")
        self.send(700, "/start")
        self.assertEqual(storage.get_user(700)["balance"], "0.00")
        self.click(ADMIN, "adm:cfg:welcome")
        self.send(ADMIN, "abc")
        self.assertIn("duas casas decimais", self.screen(ADMIN)["content"])
        self.send(ADMIN, "0")
        self.assertIn("desativado", self.screen(ADMIN)["content"])

    def test_admin_changes_referral_bonus_and_rule(self):
        self.send(ADMIN, "/admin")
        self.click(ADMIN, "adm:cfg:ref")
        self.send(ADMIN, "3")
        self.assertIn("assim que ele entra pelo link", self.screen(ADMIN)["content"])
        self.click(ADMIN, "adm:cfg:ref_mode")
        self.assertIn("1º depósito do indicado", self.screen(ADMIN)["content"])
        self.click(ADMIN, "adm:cfg:ref_mode")
        self.send(700, "/start")
        self.send(800, "/start 700")
        self.assertEqual(storage.get_user(700)["balance"], "13.00")  # 10 boas-vindas + 3 indicação
        self.assertEqual(storage.get_user(800)["balance"], "10.00")

    def test_non_admin_cannot_change_settings(self):
        self.send(USER, "/start")
        self.click(USER, "adm:cfg:welcome_off")
        self.assertIn("não tem permissão", self.screen(USER)["content"])
        self.assertEqual(storage.get_settings()["welcome_bonus"], Decimal("10.00"))


class LogChannelTests(BotTestCase):
    CHANNEL = -1001234567890

    def setUp(self):
        super().setUp()
        storage.update_settings(log_channel_id=str(self.CHANNEL))

    def logs(self):
        return [data["text"] for method, data in self.tg.calls
                if method == "sendMessage" and str(data["chat_id"]) == str(self.CHANNEL)]

    def assertLogs_terminal(self):
        import contextlib
        import io
        buffer = io.StringIO()

        @contextlib.contextmanager
        def capture():
            with contextlib.redirect_stdout(buffer):
                yield buffer
        return capture()

    def test_no_channel_sends_nothing(self):
        storage.update_settings(log_channel_id="")
        self.send(USER, "/start")
        self.assertEqual(self.logs(), [])

    def test_new_user_with_welcome_and_referral(self):
        storage.DEFAULT_SETTINGS["welcome_bonus"] = "10.00"
        self.send(700, "/start")
        self.send(800, "/start 700")
        entries = self.logs()
        new_user = [text for text in entries if "NOVO USUÁRIO" in text][-1]
        for expected in ("<code>800</code>", "Bônus de boas-vindas: R$ 10,00", "Saldo atual: <b>R$ 10,00</b>",
                         "Indicado por", "<code>700</code>", "Total de usuários: <b>2</b>", "🕒"):
            self.assertIn(expected, new_user)
        referral = [text for text in entries if "BÔNUS DE INDICAÇÃO" in text][0]
        self.assertIn("<b>R$ 5,00</b> (ao entrar pelo link)", referral)
        self.assertIn("Saldo de quem indicou: <b>R$ 15,00</b>", referral)

    def test_purchase_log(self):
        self.create_product_with_license("29,90")
        self.send(USER, "/start")
        storage.add_balance(USER, Decimal("50"))
        self.click(USER, "buy:1")
        purchase = [text for text in self.logs() if "NOVA COMPRA" in text][0]
        for expected in ("Curso Teste", "R$ 29,90", "R$ 50,00 → <b>R$ 20,10</b>", "Pedido: #1",
                         "Estoque restante: <b>0</b> ⚠️ ESGOTOU", f"<code>{USER}</code>"):
            self.assertIn(expected, purchase)
        self.assertNotIn("s3nh", purchase)  # senha nunca vai para o canal

    def test_pix_created_and_confirmed_logs(self):
        self.send(USER, "/start")
        self.click(USER, "wallet:deposit")
        self.send(USER, "25,50")
        created = [text for text in self.logs() if "PIX GERADO" in text][0]
        self.assertIn("R$ 25,50", created)
        self.assertIn("tx_1", created)
        self.roas.status = "PAID"
        handlers.reconcile_pix_deposits()
        handlers.reconcile_pix_deposits()
        confirmed = [text for text in self.logs() if "DEPÓSITO CONFIRMADO" in text]
        self.assertEqual(len(confirmed), 1)  # não duplica
        self.assertIn("R$ 0,00 → <b>R$ 25,50</b>", confirmed[0])

    def test_pix_expired_log(self):
        self.send(USER, "/start")
        self.click(USER, "wallet:deposit")
        self.send(USER, "15")
        self.roas.status = "EXPIRED"
        handlers.reconcile_pix_deposits()
        self.assertTrue(any("PIX NÃO PAGO" in text for text in self.logs()))

    def test_admin_actions_are_logged(self):
        self.create_product_with_license()
        self.send(USER, "/start")
        self.send(ADMIN, f"/saldo {USER} 20")
        self.click(ADMIN, "adm:p:1:add")
        self.send(ADMIN, "a | b | 1 | 10\nruim")
        self.click(ADMIN, "adm:p:1:rm1")
        self.click(ADMIN, "adm:p:1:price")
        self.send(ADMIN, "15")
        self.click(ADMIN, "adm:cfg:welcome_off")
        joined = "\n".join(self.logs())
        for expected in ("PRODUTO CADASTRADO", "ESTOQUE ADICIONADO", "+1", "1 linha(s) ignorada(s)",
                         "SALDO ADICIONADO PELO ADMIN", "R$ 0,00 → <b>R$ 20,00</b>",
                         "ESTOQUE REMOVIDO", "PRODUTO EDITADO", "R$ 29,90 → R$ 15,00",
                         "CONFIGURAÇÃO ALTERADA"):
            self.assertIn(expected, joined)

    def test_admin_sets_channel_by_id_and_by_forward(self):
        storage.update_settings(log_channel_id="")
        self.send(ADMIN, "/admin")
        self.click(ADMIN, "adm:cfg:logs")
        self.send(ADMIN, "-1009999")
        self.assertEqual(storage.get_settings()["log_channel_id"], "-1009999")
        self.assertIn("Canal de logs conectado", self.screen(ADMIN)["content"])
        self.assertTrue(any(m == "sendMessage" and d["chat_id"] == "-1009999" and "CONECTADO" in d["text"]
                            for m, d in self.tg.calls))

        self.click(ADMIN, "adm:cfg:logs")
        handlers.process_update({"update_id": 9, "message": {
            "chat": {"id": ADMIN}, "from": {"id": ADMIN, "first_name": "A"}, "message_id": 55,
            "forward_origin": {"type": "channel", "chat": {"id": -1005555, "type": "channel"}},
        }})
        self.assertEqual(storage.get_settings()["log_channel_id"], "-1005555")

    def test_channel_error_is_reported_and_not_saved(self):
        storage.update_settings(log_channel_id="")
        self.send(ADMIN, "/admin")
        self.click(ADMIN, "adm:cfg:logs")
        self.tg.unknown_chats.add("-1007777")
        with self.assertLogs_terminal() as terminal:
            self.send(ADMIN, "-1007777")
        content = self.screen(ADMIN)["content"]
        self.assertIn("não consegue publicar", content)
        self.assertIn("canal não encontrado", content)
        self.assertEqual(storage.get_settings()["log_channel_id"], "")
        self.assertIn("[LOGS", terminal.getvalue())
        self.assertIn("canal não encontrado", terminal.getvalue())

    def test_bot_not_admin_is_detected_before_sending(self):
        storage.update_settings(log_channel_id="")
        self.tg.bot_member = {"status": "member"}
        self.send(ADMIN, "/admin")
        self.click(ADMIN, "adm:cfg:logs")
        self.send(ADMIN, "-1007777")
        self.assertIn("precisa ser ADMINISTRADOR", self.screen(ADMIN)["content"])
        self.assertEqual(storage.get_settings()["log_channel_id"], "")
        self.tg.bot_member = {"status": "administrator", "can_post_messages": False}
        self.send(ADMIN, "-1007777")
        self.assertIn("Publicar mensagens' está desligada", self.screen(ADMIN)["content"])

    def test_username_is_saved_as_numeric_id(self):
        storage.update_settings(log_channel_id="")
        self.send(ADMIN, "/admin")
        self.click(ADMIN, "adm:cfg:logs")
        self.send(ADMIN, "@meus_logs")
        self.assertEqual(storage.get_settings()["log_channel_id"], "-1004242")

    def test_failed_send_prints_reason_in_terminal(self):
        self.tg.fail_next["sendMessage"] = telegram_api.TelegramAPIError(
            "Telegram HTTP 403", 403, "Forbidden: bot is not a member of the channel chat")
        with self.assertLogs_terminal() as terminal:
            self.send(USER, "/start")  # gera log de novo usuário
        output = terminal.getvalue()
        self.assertIn("NÃO enviado", output)
        self.assertIn("o bot não está no canal", output)
        self.assertIn("NOVO USUÁRIO", output)

    def test_not_configured_is_reported_in_terminal(self):
        storage.update_settings(log_channel_id="")
        logs_module._last_not_configured = 0.0
        with self.assertLogs_terminal() as terminal:
            self.send(USER, "/start")
        self.assertIn("nenhum canal de logs configurado", terminal.getvalue())

    def test_rate_limit_waits_and_retries(self):
        waits = []
        original_sleep = logs_module.time.sleep
        logs_module.time.sleep = waits.append
        self.tg.fail_next["sendMessage"] = telegram_api.TelegramAPIError(
            "Telegram HTTP 429", 429, "Too Many Requests: retry after 3", {"retry_after": 3})
        try:
            self.send(USER, "/start")
        finally:
            logs_module.time.sleep = original_sleep
        self.assertEqual(waits, [3])
        self.assertTrue(any("NOVO USUÁRIO" in text for text in self.logs()))

    def test_html_error_falls_back_to_plain_text(self):
        self.tg.fail_next["sendMessage"] = telegram_api.TelegramAPIError(
            "Telegram HTTP 400", 400, "Bad Request: can't parse entities")
        self.send(USER, "/start")
        plain = [d for m, d in self.tg.calls if m == "sendMessage" and str(d["chat_id"]) == str(self.CHANNEL)]
        self.assertNotIn("parse_mode", plain[-1])
        self.assertNotIn("<b>", plain[-1]["text"])

    def test_startup_report(self):
        with self.assertLogs_terminal() as terminal:
            logs_module.startup_report()
        self.assertIn("Canal de logs OK", terminal.getvalue())
        self.tg.bot_member = {"status": "left"}
        with self.assertLogs_terminal() as terminal:
            logs_module.startup_report()
        self.assertIn("COM PROBLEMA", terminal.getvalue())

    def test_queue_mode_delivers_in_background(self):
        logs_module.ASYNC = True
        try:
            self.send(USER, "/start")
            logs_module.flush(timeout=5)
        finally:
            logs_module.ASYNC = False
        self.assertTrue(any("NOVO USUÁRIO" in text for text in self.logs()))

    def test_channel_id_without_prefix_and_links(self):
        candidates = logs_module.parse_channel_input
        self.assertEqual(candidates("1234567890"), ["-1001234567890", "-1234567890"])
        self.assertEqual(candidates("-1234567890"), ["-1001234567890", "-1234567890"])
        self.assertEqual(candidates("https://t.me/c/1234567890/15"), ["-1001234567890"])
        self.assertEqual(candidates("t.me/meus_logs"), ["@meus_logs"])
        with self.assertRaises(ValueError):
            candidates("https://t.me/+AbCdEf")

        storage.update_settings(log_channel_id="")
        self.send(ADMIN, "/admin")
        self.click(ADMIN, "adm:cfg:logs")
        self.send(ADMIN, "1234567890")
        self.assertEqual(storage.get_settings()["log_channel_id"], "-1001234567890")

    def test_channel_error_explained_in_portuguese(self):
        storage.update_settings(log_channel_id="")
        self.send(ADMIN, "/admin")
        self.click(ADMIN, "adm:cfg:logs")
        self.tg.fail_next["sendMessage"] = "Telegram HTTP 403: Forbidden: bot is not a member of the channel chat"
        self.send(ADMIN, "-1007777")
        self.assertIn("administrador", self.screen(ADMIN)["content"])

    def test_log_failure_never_breaks_purchase(self):
        self.create_product_with_license()
        self.send(USER, "/start")
        storage.add_balance(USER, Decimal("50"))
        original = logs_module._post
        logs_module._post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("canal fora"))
        try:
            self.click(USER, "buy:1")
        finally:
            logs_module._post = original
        self.assertIn("COMPRA CONFIRMADA", self.screen(USER)["content"])


class LoginCarouselTests(BotTestCase):
    def setup_logins(self):
        self.create_product_with_license("20")  # login 1: aluno@exemplo.com, 12 pedidos, R$ 350,00
        self.click(ADMIN, "adm:p:1:add")
        self.send(ADMIN, "segundo@x.com | s2 | 3 | 45,50\nterceiro@x.com | s3 | 0 | 0")
        self.send(USER, "/start")
        storage.add_balance(USER, Decimal("100"))

    def markup(self):
        return json.loads(self.tg.calls[-1][1]["reply_markup"])["inline_keyboard"]

    def test_browse_logins_with_arrows(self):
        self.setup_logins()
        self.click(USER, "product:1")
        content = self.screen(USER)["content"]
        self.assertIn("Login 1 de 3", content.split("\n")[0])  # no topo, ao lado do nome
        self.assertIn("Quantidade de pedidos: <b>12</b>", content)
        self.assertIn("Valor total dos pedidos: <b>R$ 350,00</b>", content)
        self.assertNotIn("aluno@exemplo.com", content)  # credenciais nunca aparecem antes da compra
        self.assertNotIn("Estoque", content)
        rows = self.markup()
        self.assertEqual([b["text"] for b in rows[0]], ["⬅️", "🔐 1/3", "➡️"])
        self.assertEqual([b["text"] for b in rows[1]], ["✅ Comprar", "❌ Cancelar"])

        self.click(USER, "product:1:1")
        content = self.screen(USER)["content"]
        self.assertIn("Login 2 de 3", content)
        self.assertIn("Quantidade de pedidos: <b>3</b>", content)
        self.assertIn("R$ 45,50", content)
        self.click(USER, "product:1:-1")  # ⬅️ no primeiro volta para o último
        self.assertIn("Login 3 de 3", self.screen(USER)["content"])
        self.click(USER, "catalog")
        self.assertIn("Comprar Unitário", self.screen(USER)["content"])

    def test_buys_exactly_the_login_shown(self):
        self.setup_logins()
        self.click(USER, "product:1:1")
        buy = self.markup()[1][0]["callback_data"]
        self.click(USER, buy)
        delivered = self.screen(USER)["content"]
        self.assertIn("segundo@x.com", delivered)
        self.assertEqual([a["orders"] for a in storage.public_accesses(1)], [12, 0])
        self.assertEqual(storage.purchase_stats(USER)["count"], 1)

    def test_login_sold_meanwhile_shows_warning(self):
        self.setup_logins()
        self.click(USER, "product:1")
        buy = self.markup()[1][0]["callback_data"]
        storage.purchase(USER, 1, int(buy.split(":")[2]))  # outro cliente comprou antes
        self.click(USER, buy)
        content = self.screen(USER)["content"]
        self.assertIn("acabou de ser vendido", content)
        self.assertIn("Login 1 de 2", content)
        self.assertEqual(storage.purchase_stats(USER)["count"], 1)

    def test_add_stock_requires_orders_and_total(self):
        self.create_product_with_license()
        self.click(ADMIN, "adm:p:1:add")
        self.send(ADMIN, "so | login | senha\nx | y | abc | 10\nx | y | 2 | -5\nok@x.com | p | 4 | 80")
        content = self.screen(ADMIN)["content"]
        self.assertIn("1 acesso(s) adicionado(s)", content)
        for expected in ("linha 1", "linha 2", "linha 3", "pedidos | valor total"):
            self.assertIn(expected, content)
        self.assertEqual(storage.public_accesses(1)[-1], {"id": 2, "orders": 4, "total": Decimal("80.00")})

    def test_admin_viewer_shows_login_info(self):
        self.create_product_with_license()
        self.click(ADMIN, "adm:p:1:v:0")
        content = self.screen(ADMIN)["content"]
        self.assertIn("Quantidade de pedidos: <b>12</b>", content)
        self.assertIn("aluno@exemplo.com", content)

    def test_photo_caption_changes_when_browsing(self):
        self.setup_logins()
        storage.update_product(1, image="https://i.imgur.com/a.jpg")
        self.click(USER, "product:1")
        photo_id = panels.active_menu_message_id(USER)
        self.click(USER, "product:1:2")
        self.assertEqual(panels.active_menu_message_id(USER), photo_id)  # mesma foto, só a legenda mudou
        self.assertIn("Login 3 de 3", self.screen(USER)["content"])
        self.assertIn("editMessageCaption", self.tg.methods())

    def test_purchase_log_shows_login_info(self):
        storage.update_settings(log_channel_id="-1001")
        self.setup_logins()
        self.click(USER, "product:1")
        self.click(USER, self.markup()[1][0]["callback_data"])
        purchase = [d["text"] for m, d in self.tg.calls if m == "sendMessage" and "NOVA COMPRA" in d["text"]][0]
        self.assertIn("12 pedidos · R$ 350,00", purchase)

    def test_hand_written_access_with_info(self):
        storage.create_product("Manual", "", Decimal("10"))
        catalog = json.loads(storage._PRODUCTS_PATH.read_text(encoding="utf-8"))
        catalog["produtos"][0]["acessos"] = [{"login": "a", "senha": "b", "pedidos": "7", "valor_total": "70,5"}]
        storage._PRODUCTS_PATH.write_text(json.dumps(catalog), encoding="utf-8")
        self.assertEqual(storage.public_accesses(1)[0]["orders"], 7)
        self.assertEqual(storage.public_accesses(1)[0]["total"], Decimal("70.50"))


class ProductImageTests(BotTestCase):
    IMAGE = "https://i.imgur.com/abc123.jpg"

    def test_admin_sets_image_and_customer_sees_photo(self):
        self.create_product_with_license("29,90")
        self.click(ADMIN, "adm:p:1:img")
        self.send(ADMIN, "não é link")
        self.assertIn("https://", self.screen(ADMIN)["content"])
        self.send(ADMIN, self.IMAGE)
        self.assertEqual(storage.get_product(1)["image"], self.IMAGE)
        preview = self.screen(ADMIN)
        self.assertEqual(preview["kind"], "photo")
        self.assertIn("Prévia", preview["content"])
        self.assertEqual(json.loads(storage._PRODUCTS_PATH.read_text(encoding="utf-8"))["produtos"][0]["imagem"],
                         self.IMAGE)

        self.send(USER, "/start")
        storage.add_balance(USER, Decimal("50"))
        self.click(USER, "catalog")
        catalog_id = panels.active_menu_message_id(USER)
        self.click(USER, "product:1")
        screen = self.screen(USER)
        self.assertEqual(screen["kind"], "photo")
        self.assertEqual(screen["photo"], self.IMAGE)
        self.assertIn("Curso Teste", screen["content"])
        self.assertIn("Login 1 de 1", screen["content"])
        self.assertNotIn((USER, catalog_id), self.tg.messages)  # painel antigo foi trocado pela foto

        self.click(USER, "buy:1")  # a entrega volta a ser texto
        self.assertEqual(self.screen(USER)["kind"], "text")
        self.assertIn("COMPRA CONFIRMADA", self.screen(USER)["content"])

    def test_bad_image_link_is_not_saved(self):
        self.create_product_with_license()
        self.tg.bad_photos.add("https://site.com/pagina")
        self.click(ADMIN, "adm:p:1:img")
        self.send(ADMIN, "https://site.com/pagina")
        self.assertIn("não conseguiu usar essa imagem", self.screen(ADMIN)["content"])
        self.assertEqual(storage.get_product(1)["image"], "")

    def test_remove_image_and_broken_image_falls_back_to_text(self):
        self.create_product_with_license()
        storage.update_product(1, image="https://quebrado.com/x.jpg")
        self.tg.bad_photos.add("https://quebrado.com/x.jpg")
        self.send(USER, "/start")
        self.click(USER, "product:1")
        self.assertEqual(self.screen(USER)["kind"], "text")
        self.assertIn("Login 1 de 1", self.screen(USER)["content"])

        self.click(ADMIN, "adm:p:1:img")
        self.send(ADMIN, "0")
        self.assertEqual(storage.get_product(1)["image"], "")
        self.assertIn("Imagem removida", self.screen(ADMIN)["content"])

    def test_image_from_inline_search_and_thumbnail(self):
        storage.create_product("Com foto", "", Decimal("10"))
        storage.update_product(1, image=self.IMAGE)
        handlers.process_update({"update_id": 7, "inline_query": {
            "id": "iq", "from": {"id": USER}, "query": "foto", "offset": "",
        }})
        self.assertEqual(json.loads(self.tg.calls[-1][1]["results"])[0]["thumbnail_url"], self.IMAGE)
        self.send(USER, "/start")
        self.send(USER, "/produto 1")
        self.assertEqual(self.screen(USER)["kind"], "photo")

    def test_hand_edited_image_field(self):
        storage.create_product("Manual", "", Decimal("10"))
        catalog = json.loads(storage._PRODUCTS_PATH.read_text(encoding="utf-8"))
        catalog["produtos"][0]["imagem"] = "  " + self.IMAGE + "  "
        storage._PRODUCTS_PATH.write_text(json.dumps(catalog), encoding="utf-8")
        self.assertEqual(storage.get_product(1)["image"], self.IMAGE)


class HomeMenuTests(BotTestCase):
    IMAGE = "https://i.imgur.com/menu50off.jpg"

    def test_greeting_by_time_of_day(self):
        from datetime import datetime as dt
        from loja import views
        self.assertEqual(views.greeting(dt(2026, 1, 1, 8)), ("☀️", "Bom dia"))
        self.assertEqual(views.greeting(dt(2026, 1, 1, 14)), ("🌤", "Boa tarde"))
        self.assertEqual(views.greeting(dt(2026, 1, 1, 20)), ("🌙", "Boa noite"))
        self.assertEqual(views.greeting(dt(2026, 1, 1, 3)), ("🌙", "Boa noite"))
        self.send(USER, "/start")
        text = views.home_text(USER, dt(2026, 1, 1, 20))
        self.assertTrue(text.startswith("🌙 <b>Boa noite, T!</b> 👋"))
        self.assertIn("<b>🏦 Carteira:</b>\n<b> ├ ID:</b> <code>500</code>\n<b> ├💰 Saldo: R$ 0,00</b>", text)

    def test_image_shown_below_text_and_buttons_kept(self):
        storage.update_settings(home_image_url=self.IMAGE)
        self.send(USER, "/start")
        data = [d for m, d in self.tg.calls if m == "sendMessage" and d["chat_id"] == USER][-1]
        preview = json.loads(data["link_preview_options"])
        self.assertEqual(preview, {"url": self.IMAGE, "prefer_large_media": True, "show_above_text": False})
        buttons = [b["callback_data"] for row in json.loads(data["reply_markup"])["inline_keyboard"]
                   for b in row if "callback_data" in b]
        self.assertEqual(buttons[:3], ["catalog", "profile", "wallet:deposit"])
        # voltar ao menu por um botão mantém a imagem; outras telas desligam a prévia
        self.click(USER, "profile")
        self.assertEqual(json.loads(self.tg.calls[-1][1]["link_preview_options"]), {"is_disabled": True})
        self.click(USER, "home")
        self.assertEqual(json.loads(self.tg.calls[-1][1]["link_preview_options"])["url"], self.IMAGE)

    def test_admin_sets_menu_image_and_notices(self):
        self.send(ADMIN, "/admin")
        self.click(ADMIN, "adm:cfg:home_img")
        self.tg.bad_photos.add("https://site.com/pagina")
        self.send(ADMIN, "https://site.com/pagina")
        self.assertIn("não conseguiu usar essa imagem", self.screen(ADMIN)["content"])
        self.assertEqual(storage.get_settings()["home_image_url"], "")
        self.send(ADMIN, self.IMAGE)
        self.assertEqual(storage.get_settings()["home_image_url"], self.IMAGE)
        self.assertIn("Imagem do menu salva", self.screen(ADMIN)["content"])
        self.assertFalse(any(m["kind"] == "photo" for m in self.tg.messages.values()))  # teste foi apagado

        self.click(ADMIN, "adm:cfg:home_text")
        self.send(ADMIN, "🚀 Entrega imediata!\n\n💬 Suporte 24h")
        self.assertEqual(storage.get_settings()["home_notices"], "🚀 Entrega imediata!\n💬 Suporte 24h")
        self.send(USER, "/start")
        content = self.screen(USER)["content"]
        self.assertIn("Entrega imediata!", content)
        self.assertNotIn("navegador 4G", content)

        self.click(ADMIN, "adm:cfg:home_img")
        self.send(ADMIN, "0")
        self.assertEqual(storage.get_settings()["home_image_url"], "")

    def test_no_image_disables_preview(self):
        self.send(USER, "/start")
        data = [d for m, d in self.tg.calls if m == "sendMessage" and d["chat_id"] == USER][-1]
        self.assertEqual(json.loads(data["link_preview_options"]), {"is_disabled": True})


class HardeningTests(BotTestCase):
    def test_group_chats_are_ignored(self):
        handlers.process_update({"update_id": 1, "message": {
            "chat": {"id": -500, "type": "group"}, "from": {"id": USER, "first_name": "T"}, "text": "/start",
        }})
        self.assertIsNone(storage.get_user(USER))
        self.assertNotIn("sendMessage", self.tg.methods())
        handlers.process_update({"update_id": 2, "callback_query": {
            "id": "cb", "from": {"id": USER}, "data": "profile",
            "message": {"chat": {"id": -500, "type": "supergroup"}, "message_id": 9},
        }})
        self.assertEqual(self.tg.calls[-1][0], "answerCallbackQuery")
        self.assertIsNone(storage.get_user(USER))

    def test_inline_search_from_other_chats_points_to_private(self):
        storage.create_product("Algo", "", Decimal("10"))
        handlers.process_update({"update_id": 3, "inline_query": {
            "id": "iq", "from": {"id": USER}, "query": "", "offset": "", "chat_type": "group",
        }})
        method, data = self.tg.calls[-1]
        self.assertEqual(json.loads(data["results"]), [])
        self.assertIn("start_parameter", data["button"])

    def test_admin_credit_notifies_without_replacing_panel(self):
        self.send(USER, "/start")
        self.click(USER, "wallet:deposit")
        self.send(USER, "20")
        qr_panel = panels.active_menu_message_id(USER)
        self.send(ADMIN, f"/saldo {USER} 15")
        self.assertEqual(panels.active_menu_message_id(USER), qr_panel)  # QR do Pix continua na tela
        self.assertEqual(self.screen(USER)["kind"], "photo")
        notes = [d["text"] for m, d in self.tg.calls if m == "sendMessage" and d["chat_id"] == USER]
        self.assertIn("Saldo atual: <b>R$ 15,00</b>", notes[-1])

    def test_repeated_clicks_do_not_rewrite_the_data_file(self):
        self.send(USER, "/start")
        self.click(USER, "profile")
        writes = []
        original = storage._write
        storage._write = lambda data, path=None: (writes.append(path), original(data, path))
        try:
            for _ in range(3):
                self.click(USER, "profile")
        finally:
            storage._write = original
        self.assertEqual(writes, [])

    def test_first_contact_by_button_is_logged(self):
        storage.update_settings(log_channel_id="-1001")
        self.click(USER, "catalog", message_id=1)
        self.assertTrue(any(m == "sendMessage" and "NOVO USUÁRIO" in d["text"] for m, d in self.tg.calls))

    def test_backup_keeps_latest_copies(self):
        storage.create_product("X", "", Decimal("1"))
        self.send(USER, "/start")
        folder = storage.backup_data(keep=2)
        self.assertTrue((folder / "store.json").exists())
        self.assertTrue((folder / "produtos.json").exists())

    def test_instance_lock(self):
        self.assertTrue(storage.acquire_instance_lock())
        script = (
            "import os, sys; sys.path.insert(0, %r); os.environ['DATA_FILE']=%r;"
            "from loja import storage; print(storage.acquire_instance_lock())"
        ) % (str(Path(__file__).resolve().parent.parent), str(storage._PATH))
        import subprocess
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env={
            **os.environ, "DATA_FILE": str(storage._PATH)}, timeout=60)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "False")


class KeepAliveTests(unittest.TestCase):
    """Conexão HTTPS reaproveitada e reaberta quando o Telegram fecha a conexão ociosa."""

    def setUp(self):
        telegram_api._drop_connection()

    def tearDown(self):
        telegram_api._drop_connection()

    def fake_connection(self, script):
        import http.client

        class Response:
            def __init__(self, status, body):
                self.status, self._body, self.will_close = status, body, False

            def read(self):
                return self._body

        created = []

        class Connection:
            def __init__(self, host, timeout=None, context=None):
                self.sock = None
                self.timeout = timeout
                created.append(self)

            def request(self, method, path, body=None, headers=None):
                action = script.pop(0)
                if action == "drop":
                    self.sock = None
                    raise http.client.RemoteDisconnected("fechou")
                self.sock = type("S", (), {"settimeout": lambda *_: None})()
                self._next = action

            def getresponse(self):
                return Response(*self._next)

            def close(self):
                self.sock = None

        return Connection, created

    def test_connection_is_reused(self):
        Connection, created = self.fake_connection([(200, b'{"ok":true,"result":1}')] * 3)
        original = telegram_api.http.client.HTTPSConnection
        telegram_api.http.client.HTTPSConnection = Connection
        try:
            for _ in range(3):
                self.assertEqual(telegram_api.call("getMe"), 1)
        finally:
            telegram_api.http.client.HTTPSConnection = original
        self.assertEqual(len(created), 1)

    def test_reconnects_when_idle_connection_was_closed(self):
        Connection, created = self.fake_connection([
            (200, b'{"ok":true,"result":1}'), "drop", (200, b'{"ok":true,"result":2}'),
        ])
        original = telegram_api.http.client.HTTPSConnection
        telegram_api.http.client.HTTPSConnection = Connection
        try:
            self.assertEqual(telegram_api.call("getMe"), 1)
            self.assertEqual(telegram_api.call("getMe"), 2)
        finally:
            telegram_api.http.client.HTTPSConnection = original
        self.assertEqual(len(created), 2)

    def test_api_error_keeps_code_and_description(self):
        Connection, _ = self.fake_connection([
            (403, b'{"ok":false,"error_code":403,"description":"Forbidden: bot was blocked by the user"}'),
        ])
        original = telegram_api.http.client.HTTPSConnection
        telegram_api.http.client.HTTPSConnection = Connection
        try:
            with self.assertRaises(telegram_api.TelegramAPIError) as ctx:
                telegram_api.call("sendMessage", {"chat_id": 1, "text": "x"})
        finally:
            telegram_api.http.client.HTTPSConnection = original
        self.assertEqual(ctx.exception.code, 403)
        self.assertIn("Telegram HTTP 403", str(ctx.exception))


class PixTests(BotTestCase):
    def test_pix_flow_credits_once(self):
        self.send(USER, "/start")
        self.click(USER, "wallet:deposit")
        self.send(USER, "9,99")
        self.assertIn("mínimo", self.screen(USER)["content"])
        self.send(USER, "25,50")

        self.assertEqual(len(self.roas.created), 1)
        payload = self.roas.created[0]
        self.assertEqual(payload["amount"], 2550)
        self.assertEqual(payload["customer"], {
            "name": "Loja Exemplo Ltda", "email": "financeiro@example.com",
            "document": {"type": "cpf", "number": "52998224725"}, "phone": "5511999998888",
        })
        self.assertEqual(self.screen(USER)["kind"], "photo")

        deposit_id = handlers.ACTIVE_PIX_DEPOSITS[USER]
        self.click(USER, f"pix:check:{deposit_id}")
        self.assertEqual(self.screen(USER)["kind"], "photo")  # QR continua visível
        self.assertIn("ainda não confirmado", self.screen(USER)["content"])
        self.assertEqual(storage.get_user(USER)["balance"], "0.00")

        self.roas.status = "PAID"
        handlers.reconcile_pix_deposits()
        handlers.reconcile_pix_deposits()
        self.assertEqual(storage.get_user(USER)["balance"], "25.50")
        self.assertEqual(self.screen(USER)["kind"], "text")
        self.assertIn("Recarga Pix confirmada", self.screen(USER)["content"])

        self.click(USER, f"pix:check:{deposit_id}")
        self.assertEqual(storage.get_user(USER)["balance"], "25.50")

    def test_cancel_from_qr_returns_to_text_menu(self):
        self.send(USER, "/start")
        self.click(USER, "wallet:deposit")
        self.send(USER, "10")
        qr_id = panels.active_menu_message_id(USER)
        self.click(USER, "home")
        self.assertNotIn((USER, qr_id), self.tg.messages)
        self.assertIn("Carteira", self.screen(USER)["content"])

    def test_wrong_paid_amount_is_not_credited(self):
        self.send(USER, "/start")
        self.click(USER, "wallet:deposit")
        self.send(USER, "50")
        self.roas.status = "PAID"
        self.roas.amount_override = 100
        handlers.reconcile_pix_deposits()
        self.assertEqual(storage.get_user(USER)["balance"], "0.00")

    def test_expired_charge_is_marked_failed(self):
        self.send(USER, "/start")
        self.click(USER, "wallet:deposit")
        self.send(USER, "15")
        self.roas.status = "EXPIRED"
        handlers.reconcile_pix_deposits()
        self.assertEqual(storage.pending_pix_deposits(), [])
        self.assertIn("não foi confirmada", self.screen(USER)["content"])

    def test_pix_unavailable_without_payer_config(self):
        original = config.ROASCLUB_CUSTOMER_CPF
        config.ROASCLUB_CUSTOMER_CPF = "11111111111"
        try:
            self.send(USER, "/start")
            self.click(USER, "wallet:deposit")
            self.assertIn("indisponível", self.screen(USER)["content"])
            self.assertEqual(self.roas.created, [])
        finally:
            config.ROASCLUB_CUSTOMER_CPF = original

    def test_no_fake_payer_generator(self):
        self.assertFalse(hasattr(payments, "generate_customer"))
        self.assertFalse(hasattr(validators, "generate_cpf"))


class ParsingTests(unittest.TestCase):
    def test_parse_amounts(self):
        self.assertEqual(validators.parse_amount_cents("10"), 1000)
        self.assertEqual(validators.parse_amount_cents("10,5"), 1050)
        self.assertEqual(validators.parse_amount_cents("R$ 1234.56"), 123456)
        for bad in ("", "-1", "0", "1.2.3", "1,234", "abc", "NaN", "Infinity", "1e5"):
            with self.assertRaises(ValueError, msg=bad):
                validators.parse_amount_cents(bad)

    def test_parse_id(self):
        self.assertEqual(validators.parse_id(" #12 ", "pedido"), 12)
        with self.assertRaises(ValueError):
            validators.parse_id("0", "pedido")


if __name__ == "__main__":
    unittest.main()
