"""Testes de ponta a ponta do store_bot com Telegram e ROASClub simulados.

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
    "ROASCLUB_PUBLIC_KEY": "pk_test",
    "ROASCLUB_SECRET_KEY": "sk_test",
    "ROASCLUB_CUSTOMER_NAME": "Loja Exemplo Ltda",
    "ROASCLUB_CUSTOMER_EMAIL": "financeiro@example.com",
    "ROASCLUB_CUSTOMER_CPF": "529.982.247-25",
    "ROASCLUB_CUSTOMER_PHONE": "(11) 99999-8888",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import store_bot as bot  # noqa: E402

ADMIN = 1
USER = 500


class FakeTelegram:
    """Simula a Bot API: guarda as mensagens existentes e as chamadas feitas."""

    def __init__(self):
        self.calls = []
        self.messages = {}  # (chat_id, message_id) -> {"kind": "text"|"photo", "content": str}
        self.next_id = 100
        self.fail_next = {}

    def _new(self, chat_id, kind, content):
        self.next_id += 1
        self.messages[(int(chat_id), self.next_id)] = {"kind": kind, "content": content}
        return {"message_id": self.next_id, "chat": {"id": int(chat_id)}}

    def call(self, method, data=None, timeout=45):
        data = dict(data or {})
        self.calls.append((method, data))
        if method in self.fail_next:
            raise RuntimeError(self.fail_next.pop(method))
        if method == "getMe":
            return {"id": 42, "username": "loja_teste_bot"}
        if method in {"deleteWebhook", "answerCallbackQuery"}:
            return True
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
        if bot._PATH.exists():
            bot._PATH.unlink()
        bot.ACTIVE_MENUS.clear()
        bot.ACTIVE_PIX_DEPOSITS.clear()
        bot.STATES.clear()
        self.tg = FakeTelegram()
        self.roas = FakeRoasClub()
        self._originals = {
            name: getattr(bot, name)
            for name in ("call", "send_photo", "roas_api_call", "encrypt_text", "decrypt_text")
        }
        bot.call = self.tg.call
        bot.send_photo = self.tg.send_photo
        bot.roas_api_call = self.roas
        bot.encrypt_text = fake_encrypt
        bot.decrypt_text = fake_decrypt

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(bot, name, value)

    # Auxiliares que imitam o que o Telegram envia ao bot
    def send(self, user_id, text):
        bot.process_update({"update_id": 1, "message": {
            "chat": {"id": user_id}, "from": {"id": user_id, "first_name": "T"}, "text": text,
        }})

    def click(self, user_id, data, message_id=None):
        message_id = message_id or bot.active_menu_message_id(user_id)
        message = {"chat": {"id": user_id}, "message_id": message_id}
        if self.tg.messages.get((user_id, message_id), {}).get("kind") == "photo":
            message["photo"] = [{"file_id": "x"}]
        bot.process_update({"update_id": 2, "callback_query": {
            "id": "cb", "from": {"id": user_id, "first_name": "T"}, "message": message, "data": data,
        }})

    def screen(self, user_id):
        message_id = bot.active_menu_message_id(user_id)
        return self.tg.messages[(user_id, message_id)]

    def create_product_with_license(self, price="29,90"):
        self.send(ADMIN, "/start")
        self.click(ADMIN, "adm:add_product")
        self.send(ADMIN, "Curso Teste")
        self.send(ADMIN, "Descrição do curso")
        self.send(ADMIN, price)
        self.assertIn("Produto #1 cadastrado", self.screen(ADMIN)["content"])
        self.click(ADMIN, "adm:add_license")
        self.send(ADMIN, "1")
        self.send(ADMIN, "https://exemplo.com/login")
        self.send(ADMIN, "aluno@exemplo.com")
        self.send(ADMIN, "s3nh@<forte>")
        self.assertIn("Licença adicionada", self.screen(ADMIN)["content"])


class StartupTests(BotTestCase):
    def test_startup_checks_pass_with_valid_config(self):
        self.assertTrue(bot.startup_checks())
        self.assertEqual(self.tg.methods()[:2], ["getMe", "deleteWebhook"])

    def test_startup_fails_cleanly_with_bad_token(self):
        self.tg.fail_next["getMe"] = "Telegram HTTP 401: Unauthorized"
        self.assertFalse(bot.startup_checks())

    def test_config_errors_detect_placeholders(self):
        original = (bot.BOT_TOKEN, bot.ADMIN_IDS, bot._RAW_ADMIN_IDS)
        try:
            bot.BOT_TOKEN, bot.ADMIN_IDS, bot._RAW_ADMIN_IDS = "TOKEN_DO_BOTFATHER", set(), ["SEU_ID"]
            errors = bot.config_errors()
            self.assertEqual(len(errors), 3)
        finally:
            bot.BOT_TOKEN, bot.ADMIN_IDS, bot._RAW_ADMIN_IDS = original


class MenuTests(BotTestCase):
    def test_start_creates_user_and_panel(self):
        self.send(USER, "/start")
        self.assertIsNotNone(bot.get_user(USER))
        content = self.screen(USER)["content"]
        self.assertIn("Saldo: R$ 0,00", content)
        self.assertIn("@suporte_teste", content)
        self.assertIn("@canal_teste", content)

    def test_admin_command_without_panel_sends_new_message(self):
        self.send(ADMIN, "/admin")
        self.assertIn("Painel administrativo", self.screen(ADMIN)["content"])

    def test_deleted_panel_is_recreated(self):
        self.send(USER, "/start")
        old_id = bot.active_menu_message_id(USER)
        del self.tg.messages[(USER, old_id)]
        self.send(USER, "/cancel")
        self.assertNotEqual(bot.active_menu_message_id(USER), old_id)
        self.assertIn("Carteira", self.screen(USER)["content"])

    def test_clicking_same_button_twice_is_harmless(self):
        self.send(USER, "/start")
        self.click(USER, "profile")
        self.click(USER, "profile")
        self.assertIn("Meu perfil", self.screen(USER)["content"])

    def test_non_admin_cannot_open_admin_panel(self):
        self.send(USER, "/start")
        self.click(USER, "admin")
        self.assertIn("não tem permissão", self.screen(USER)["content"])


class PurchaseTests(BotTestCase):
    def test_full_purchase_flow(self):
        self.create_product_with_license("29,90")
        self.send(USER, "/start")
        self.send(ADMIN, f"/saldo {USER} 50,00")
        self.assertEqual(bot.get_user(USER)["balance"], "50.00")

        self.click(USER, "catalog")
        self.assertIn("Curso Teste", self.screen(USER)["content"])
        self.click(USER, "product:1")
        self.click(USER, "buy:1")
        delivered = self.screen(USER)["content"]
        self.assertIn("COMPRA CONFIRMADA", delivered)
        self.assertIn("s3nh@&lt;forte&gt;", delivered)  # HTML escapado
        self.assertEqual(bot.get_user(USER)["balance"], "20.10")
        self.assertEqual(bot.user_orders(USER)[0]["status"], "completed")
        self.assertEqual(bot.list_products()[0]["stock"], 0)

        self.click(USER, "history")
        self.assertIn("Compra 1 de 1", self.screen(USER)["content"])

        bot.add_balance(USER, Decimal("100"))
        self.click(USER, "buy:1")  # sem estoque
        self.assertIn("Estoque esgotado", self.screen(USER)["content"])
        self.assertEqual(bot.get_user(USER)["balance"], "120.10")

    def test_insufficient_balance_does_not_create_order(self):
        self.create_product_with_license("29,90")
        self.send(USER, "/start")
        self.click(USER, "buy:1")
        self.assertIn("Saldo insuficiente", self.screen(USER)["content"])
        self.assertEqual(bot.user_orders(USER), [])
        self.assertEqual(bot.list_products()[0]["stock"], 1)

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
        self.assertEqual(bot.get_product(1)["price"], Decimal("10.50"))
        self.click(ADMIN, "adm:add_license")
        self.send(ADMIN, "xyz")
        self.assertIn("número do produto", self.screen(ADMIN)["content"])

    def test_saldo_rejects_unknown_user_and_bad_amount(self):
        self.send(ADMIN, "/start")
        self.send(ADMIN, "/saldo 999 10")
        self.assertIn("Usuário não encontrado", self.screen(ADMIN)["content"])
        self.send(USER, "/start")
        self.send(ADMIN, f"/saldo {USER} -5")
        self.assertEqual(bot.get_user(USER)["balance"], "0.00")

    def test_resend_delivery_to_user(self):
        self.create_product_with_license()
        self.send(USER, "/start")
        bot.add_balance(USER, Decimal("100"))
        original_edit = bot.edit_message

        def failing_edit(chat_id, message_id, text, *args, **kwargs):
            if "COMPRA CONFIRMADA" in text:
                raise RuntimeError("falha simulada")
            return original_edit(chat_id, message_id, text, *args, **kwargs)

        bot.edit_message = failing_edit
        try:
            self.click(USER, "buy:1")
        finally:
            bot.edit_message = original_edit
        self.assertEqual(bot.user_orders(USER)[0]["status"], "delivery_failed")
        self.assertIn("t.me/suporte_teste", json.dumps(self.tg.calls[-1][1], ensure_ascii=False))

        self.click(ADMIN, "adm:resend")
        self.send(ADMIN, "#1")
        self.assertIn("Entrega reenviada", self.screen(ADMIN)["content"])
        self.assertIn("COMPRA CONFIRMADA", self.screen(USER)["content"])
        self.assertEqual(bot.user_orders(USER)[0]["status"], "completed")


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

        deposit_id = bot.ACTIVE_PIX_DEPOSITS[USER]
        self.click(USER, f"pix:check:{deposit_id}")
        self.assertEqual(self.screen(USER)["kind"], "photo")  # QR continua visível
        self.assertIn("ainda não confirmado", self.screen(USER)["content"])
        self.assertEqual(bot.get_user(USER)["balance"], "0.00")

        self.roas.status = "PAID"
        bot.reconcile_pix_deposits()
        bot.reconcile_pix_deposits()
        self.assertEqual(bot.get_user(USER)["balance"], "25.50")
        self.assertEqual(self.screen(USER)["kind"], "text")
        self.assertIn("Recarga Pix confirmada", self.screen(USER)["content"])

        self.click(USER, f"pix:check:{deposit_id}")
        self.assertEqual(bot.get_user(USER)["balance"], "25.50")

    def test_cancel_from_qr_returns_to_text_menu(self):
        self.send(USER, "/start")
        self.click(USER, "wallet:deposit")
        self.send(USER, "10")
        qr_id = bot.active_menu_message_id(USER)
        self.click(USER, "home")
        self.assertNotIn((USER, qr_id), self.tg.messages)
        self.assertIn("Carteira", self.screen(USER)["content"])

    def test_wrong_paid_amount_is_not_credited(self):
        self.send(USER, "/start")
        self.click(USER, "wallet:deposit")
        self.send(USER, "50")
        self.roas.status = "PAID"
        self.roas.amount_override = 100
        bot.reconcile_pix_deposits()
        self.assertEqual(bot.get_user(USER)["balance"], "0.00")

    def test_expired_charge_is_marked_failed(self):
        self.send(USER, "/start")
        self.click(USER, "wallet:deposit")
        self.send(USER, "15")
        self.roas.status = "EXPIRED"
        bot.reconcile_pix_deposits()
        self.assertEqual(bot.pending_pix_deposits(), [])
        self.assertIn("não foi confirmada", self.screen(USER)["content"])

    def test_pix_unavailable_without_payer_config(self):
        original = bot.ROASCLUB_CUSTOMER_CPF
        bot.ROASCLUB_CUSTOMER_CPF = "11111111111"
        try:
            self.send(USER, "/start")
            self.click(USER, "wallet:deposit")
            self.assertIn("indisponível", self.screen(USER)["content"])
            self.assertEqual(self.roas.created, [])
        finally:
            bot.ROASCLUB_CUSTOMER_CPF = original

    def test_no_fake_payer_generator(self):
        self.assertFalse(hasattr(bot, "generate_customer"))
        self.assertFalse(hasattr(bot, "generate_cpf"))


class ParsingTests(unittest.TestCase):
    def test_parse_amounts(self):
        self.assertEqual(bot.parse_amount_cents("10"), 1000)
        self.assertEqual(bot.parse_amount_cents("10,5"), 1050)
        self.assertEqual(bot.parse_amount_cents("R$ 1234.56"), 123456)
        for bad in ("", "-1", "0", "1.2.3", "1,234", "abc", "NaN", "Infinity", "1e5"):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_amount_cents(bad)

    def test_parse_id(self):
        self.assertEqual(bot.parse_id(" #12 ", "pedido"), 12)
        with self.assertRaises(ValueError):
            bot.parse_id("0", "pedido")


if __name__ == "__main__":
    unittest.main()
