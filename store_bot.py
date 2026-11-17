"""Telegram Store: bot, armazenamento JSON, segurança e interface em um único arquivo."""
import base64
import ctypes
from io import BytesIO
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import qrcode
import qrcode.constants
import qrcode.exceptions
from ctypes import wintypes
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path


# Configuração
BASE_DIR = Path(__file__).resolve().parent


def load_env(path=BASE_DIR / ".env"):
    """Carrega variáveis de um .env sem dependências externas."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int_env(name, default, minimum=1, maximum=None):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"Aviso: {name}={raw!r} não é um número inteiro; usando {default}.")
        return default
    if value < minimum or (maximum is not None and value > maximum):
        print(f"Aviso: {name}={value} fora do intervalo permitido; usando {default}.")
        return default
    return value


load_env()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
_RAW_ADMIN_IDS = [value.strip() for value in os.getenv("ADMIN_IDS", "").split(",") if value.strip()]
ADMIN_IDS = {int(value) for value in _RAW_ADMIN_IDS if value.isdigit()}
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
STORE_CHANNEL = os.getenv("STORE_CHANNEL", "").strip().lstrip("@")
DATA_FILE = os.getenv("DATA_FILE", "data/store.json").strip() or "data/store.json"
POLLING_TIMEOUT = _int_env("POLLING_TIMEOUT", 30, minimum=1, maximum=50)
ROASCLUB_PUBLIC_KEY = os.getenv("ROASCLUB_PUBLIC_KEY", "").strip()
ROASCLUB_SECRET_KEY = os.getenv("ROASCLUB_SECRET_KEY", "").strip()
# Pagador informado à ROASClub em todas as cobranças (dados reais, autorizados pela ROASClub).
ROASCLUB_CUSTOMER_NAME = os.getenv("ROASCLUB_CUSTOMER_NAME", "").strip()
ROASCLUB_CUSTOMER_EMAIL = os.getenv("ROASCLUB_CUSTOMER_EMAIL", "").strip()
ROASCLUB_CUSTOMER_CPF = re.sub(r"\D", "", os.getenv("ROASCLUB_CUSTOMER_CPF", ""))
ROASCLUB_CUSTOMER_PHONE = re.sub(r"\D", "", os.getenv("ROASCLUB_CUSTOMER_PHONE", ""))
ROASCLUB_API = "https://api2.roasclub.xyz"
PAYMENT_CHECK_INTERVAL = 30
PIX_DEPOSIT_EXPIRY_DAYS = 1
MIN_PIX_DEPOSIT_CENTS = 1000
_PAYMENT_LAST_REQUEST = 0.0
_PAYMENT_RATE_LOCK = threading.Lock()
_PIX_STATUS_LAST_REQUEST = 0.0
PIX_STATUS_MIN_INTERVAL = 2.1



def config_errors():
    """Problemas que impedem o bot de iniciar."""
    errors = []
    if not BOT_TOKEN or BOT_TOKEN.upper().startswith("TOKEN_DO"):
        errors.append("BOT_TOKEN não configurado. Informe o token do @BotFather no arquivo .env.")
    elif not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", BOT_TOKEN):
        errors.append("BOT_TOKEN tem formato inválido (esperado: 123456789:ABC...).")
    invalid_admins = [value for value in _RAW_ADMIN_IDS if not value.isdigit()]
    if invalid_admins:
        errors.append(
            "ADMIN_IDS contém valores que não são IDs numéricos: " + ", ".join(invalid_admins)
        )
    if not ADMIN_IDS:
        errors.append("ADMIN_IDS não configurado. Informe ao menos um ID numérico de administrador.")
    return errors


class PaymentAPIError(RuntimeError):
    def __init__(self, code, retryable=False, http_status=None, detail=None):
        super().__init__(str(detail or code))
        self.code = str(code)
        self.retryable = retryable
        self.http_status = http_status
        self.detail = str(detail)[:400] if detail else None



# Proteção de credenciais com DPAPI do Windows
class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def encrypt_text(text: str) -> str:
    if os.name != "nt":
        raise RuntimeError("A criptografia DPAPI desta versão requer Windows.")
    raw = text.encode("utf-8")
    in_blob, keepalive = _blob(raw)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob), "TelegramStore", None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return base64.b64encode(
            ctypes.string_at(out_blob.pbData, out_blob.cbData)
        ).decode("ascii")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def decrypt_text(encoded: str) -> str:
    if os.name != "nt":
        raise RuntimeError("A criptografia DPAPI desta versão requer Windows.")
    encrypted = base64.b64decode(encoded.encode("ascii"))
    in_blob, keepalive = _blob(encrypted)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB)
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(out_blob.pbData)


# Armazenamento local em JSON, com migração não destrutiva do antigo TXT.
_LOCK = threading.RLock()
_PATH = Path(DATA_FILE)
if not _PATH.is_absolute():
    _PATH = BASE_DIR / _PATH
_LEGACY_PATH = _PATH.with_suffix(".txt") if _PATH.suffix.lower() == ".json" else None
_EMPTY = {
    "meta": {"version": 1, "next_ids": {"product": 1, "license": 1, "order": 1, "transaction": 1}},
    "users": {}, "products": [], "licenses": [], "orders": [], "wallet_transactions": [],
    "pix_deposits": [], "active_menus": {}, "active_menu_media": {}
}


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read():
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    if not _PATH.exists():
        if _LEGACY_PATH and _LEGACY_PATH.exists():
            _write(_load_store(_LEGACY_PATH))
        else:
            _write(_EMPTY)
    return _load_store(_PATH)


def _load_store(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"O arquivo de dados {path} está inválido. Não apague o arquivo; restaure um backup."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"O arquivo de dados {path} deve conter um objeto JSON.")
    for key, value in _EMPTY.items():
        if key in data and not isinstance(data[key], type(value)):
            raise RuntimeError(f"A seção '{key}' do arquivo {path} tem formato inválido.")
        data.setdefault(key, value.copy() if isinstance(value, dict) else [])
    meta = data["meta"]
    next_ids = meta.get("next_ids")
    if next_ids is None:
        next_ids = _EMPTY["meta"]["next_ids"].copy()
        meta["next_ids"] = next_ids
    if not isinstance(next_ids, dict):
        raise RuntimeError(f"A seção 'meta.next_ids' do arquivo {path} tem formato inválido.")
    for kind, initial_value in _EMPTY["meta"]["next_ids"].items():
        value = next_ids.setdefault(kind, initial_value)
        if type(value) is not int or value < 1:
            raise RuntimeError(f"O contador meta.next_ids.{kind} do arquivo {path} é inválido.")
    return data


def _write(data):
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="store_", suffix=".tmp", dir=str(_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        for attempt in range(5):
            try:
                os.replace(temp_name, _PATH)
                break
            except PermissionError:
                # Antivírus/indexador do Windows pode segurar o arquivo por alguns instantes.
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _next(data, kind):
    value = int(data["meta"]["next_ids"][kind])
    data["meta"]["next_ids"][kind] = value + 1
    return value


def ensure_user(tg_user):
    with _LOCK:
        data = _read()
        uid = str(tg_user["id"])
        old = data["users"].get(uid, {})
        data["users"][uid] = {
            "telegram_id": int(tg_user["id"]),
            "username": tg_user.get("username"),
            "first_name": tg_user.get("first_name"),
            "balance": old.get("balance", "0.00"),
            "created_at": old.get("created_at", _now()),
            "updated_at": _now()
        }
        _write(data)


def get_user(user_id):
    with _LOCK:
        return _read()["users"].get(str(user_id))


def list_products():
    with _LOCK:
        data = _read()
        result = []
        for product in data["products"]:
            if not product.get("active", True):
                continue
            stock = sum(
                1 for license_item in data["licenses"]
                if license_item["product_id"] == product["id"] and license_item["status"] == "available"
            )
            item = dict(product)
            item["stock"] = stock
            item["price"] = Decimal(item["price"])
            result.append(item)
        return sorted(result, key=lambda item: item["id"], reverse=True)


def get_product(product_id):
    with _LOCK:
        for product in _read()["products"]:
            if product["id"] == int(product_id) and product.get("active", True):
                item = dict(product)
                item["price"] = Decimal(item["price"])
                return item
        return None


def create_product(name, description, price):
    with _LOCK:
        data = _read()
        product_id = _next(data, "product")
        data["products"].append({
            "id": product_id, "name": name, "description": description,
            "price": str(Decimal(price).quantize(Decimal("0.01"))),
            "active": True, "created_at": _now()
        })
        _write(data)
        return product_id


def add_license(product_id, encrypted_payload):
    with _LOCK:
        data = _read()
        if not any(
            product["id"] == int(product_id) and product.get("active", True)
            for product in data["products"]
        ):
            raise ValueError("Produto não encontrado ou inativo.")
        license_id = _next(data, "license")
        data["licenses"].append({
            "id": license_id, "product_id": int(product_id),
            "encrypted_payload": encrypted_payload, "status": "available",
            "created_at": _now(), "sold_at": None
        })
        _write(data)
        return license_id


def _add_transaction(data, user_id, amount, description):
    transaction_id = _next(data, "transaction")
    data["wallet_transactions"].append({
        "id": transaction_id, "telegram_id": int(user_id),
        "amount": str(Decimal(amount).quantize(Decimal("0.01"))),
        "description": description, "created_at": _now()
    })


def add_balance(user_id, amount, description="Adição administrativa"):
    amount = Decimal(amount)
    if amount <= 0:
        raise ValueError("O valor deve ser maior que zero.")
    with _LOCK:
        data = _read()
        user = data["users"].get(str(user_id))
        if not user:
            raise ValueError("Usuário não encontrado. Peça para ele iniciar o bot com /start.")
        user["balance"] = str((Decimal(user["balance"]) + amount).quantize(Decimal("0.01")))
        user["updated_at"] = _now()
        _add_transaction(data, user_id, amount, description)
        _write(data)


def start_pix_deposit(user_id, amount_cents):
    if type(amount_cents) is not int or amount_cents < MIN_PIX_DEPOSIT_CENTS:
        raise ValueError("O valor mínimo para recarga via Pix é R$ 10,00.")
    with _LOCK:
        data = _read()
        if str(user_id) not in data["users"]:
            raise ValueError("Usuário não encontrado.")
        deposit = {
            "id": uuid.uuid4().hex,
            "external_ref": uuid.uuid4().hex,
            "provider": "roasclub",
            "telegram_id": int(user_id),
            "amount_cents": int(amount_cents),
            "transaction_id": None,
            "pix_code": None,
            "status": "creating",
            "created_at": _now(),
            "confirmed_at": None,
        }
        data["pix_deposits"].append(deposit)
        _write(data)
        return dict(deposit)


def get_pix_deposit(deposit_id):
    with _LOCK:
        data = _read()
        deposit = next((item for item in data["pix_deposits"] if item["id"] == deposit_id), None)
        return dict(deposit) if deposit else None


def update_pix_deposit(deposit_id, response):
    with _LOCK:
        data = _read()
        deposit = next((item for item in data["pix_deposits"] if item["id"] == deposit_id), None)
        if not deposit:
            raise ValueError("Transação local não encontrada.")
        if not isinstance(response, dict):
            raise ValueError("A resposta da transação está inválida.")
        transaction = response.get("data", response)
        if not isinstance(transaction, dict):
            raise ValueError("A resposta da transação está inválida.")
        if type(transaction.get("amount")) is not int:
            raise ValueError("O valor retornado pela transação é inválido.")
        if transaction["amount"] != deposit["amount_cents"]:
            raise ValueError("O valor retornado pela transação não corresponde ao solicitado.")
        transaction_id = transaction.get("id")
        pix_data = transaction.get("pix")
        pix_code = pix_data.get("qr_code") if isinstance(pix_data, dict) else None
        if not transaction_id or not isinstance(pix_code, str) or not pix_code.strip():
            raise ValueError("A resposta da transação não contém ID e código Pix.")
        if deposit.get("transaction_id") and deposit["transaction_id"] != transaction_id:
            raise ValueError("A API retornou um ID de transação diferente.")
        transaction_status = str(transaction.get("status", "")).upper()
        if transaction_status not in {"PENDING", "PAID"}:
            raise ValueError("A API retornou um status inesperado para a transação.")
        expiration_date = pix_data.get("expiration_date") if isinstance(pix_data, dict) else None
        if expiration_date:
            try:
                expires_at = datetime.fromisoformat(str(expiration_date).replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
            except ValueError as exc:
                raise ValueError("A data de expiração retornada pela transação é inválida.") from exc
        else:
            created_at = datetime.fromisoformat(deposit["created_at"])
            expires_at = (created_at + timedelta(days=PIX_DEPOSIT_EXPIRY_DAYS)).isoformat(timespec="seconds")
        deposit.update({
            "transaction_id": str(transaction_id),
            "pix_code": pix_code,
            "expires_at": expires_at,
            "status": "pending",
        })
        _write(data)
        return dict(deposit)


def fail_pix_deposit(deposit_id):
    with _LOCK:
        data = _read()
        deposit = next((item for item in data["pix_deposits"] if item["id"] == deposit_id), None)
        if deposit and deposit["status"] in {"creating", "pending", "creation_unknown"}:
            deposit["status"] = "failed"
            _write(data)


def mark_pix_deposit_uncertain(deposit_id):
    with _LOCK:
        data = _read()
        deposit = next((item for item in data["pix_deposits"] if item["id"] == deposit_id), None)
        if deposit and deposit["status"] == "creating":
            deposit["status"] = "creation_unknown"
            _write(data)


def pending_pix_deposits():
    with _LOCK:
        return [
            dict(item) for item in _read()["pix_deposits"]
            if item.get("provider") == "roasclub" and item["status"] == "pending"
        ]


def confirm_pix_deposit(deposit_id, api_status, paid_amount_cents):
    if api_status != "PAID":
        return False
    with _LOCK:
        data = _read()
        deposit = next((item for item in data["pix_deposits"] if item["id"] == deposit_id), None)
        if not deposit:
            raise ValueError("Transação local não encontrada.")
        if deposit["status"] == "confirmed":
            return False
        if deposit["status"] != "pending":
            return False
        if type(paid_amount_cents) is not int or paid_amount_cents != deposit["amount_cents"]:
            raise ValueError("O valor pago não corresponde à transação.")
        user = data["users"].get(str(deposit["telegram_id"]))
        if not user:
            raise ValueError("Usuário da cobrança Pix não encontrado.")
        credit = (Decimal(deposit["amount_cents"]) / Decimal(100)).quantize(Decimal("0.01"))
        user["balance"] = str((Decimal(user["balance"]) + credit).quantize(Decimal("0.01")))
        user["updated_at"] = _now()
        _add_transaction(
            data, deposit["telegram_id"], credit,
            f"Recarga Pix ROASClub {deposit['transaction_id']}"
        )
        deposit["status"] = "confirmed"
        deposit["confirmed_at"] = _now()
        _write(data)
        return True


def purchase(user_id, product_id):
    with _LOCK:
        data = _read()
        user = data["users"].get(str(user_id))
        if not user:
            raise ValueError("Usuário não encontrado.")
        product = next(
            (item for item in data["products"]
             if item["id"] == int(product_id) and item.get("active", True)),
            None
        )
        if not product:
            raise ValueError("Produto indisponível.")
        price = Decimal(product["price"])
        balance = Decimal(user["balance"])
        if balance < price:
            raise ValueError("Saldo insuficiente.")
        license_item = next(
            (item for item in data["licenses"]
             if item["product_id"] == int(product_id) and item["status"] == "available"),
            None
        )
        if not license_item:
            raise ValueError("Estoque esgotado.")
        order_id = _next(data, "order")
        user["balance"] = str((balance - price).quantize(Decimal("0.01")))
        user["updated_at"] = _now()
        license_item["status"] = "sold"
        license_item["sold_at"] = _now()
        order = {
            "id": order_id, "telegram_id": int(user_id), "product_id": int(product_id),
            "license_id": license_item["id"], "amount": str(price), "status": "delivery_pending",
            "created_at": _now(), "delivered_at": None
        }
        data["orders"].append(order)
        _add_transaction(data, user_id, -price, f"Compra #{order_id}")
        _write(data)
        return {
            "order_id": order_id,
            "product": {**product, "price": price},
            "encrypted_payload": license_item["encrypted_payload"]
        }


def set_order_status(order_id, status):
    if status not in {"delivery_pending", "completed", "delivery_failed"}:
        raise ValueError("Status de pedido inválido.")
    with _LOCK:
        data = _read()
        order = next((item for item in data["orders"] if item["id"] == int(order_id)), None)
        if not order:
            raise ValueError("Pedido não encontrado.")
        order["status"] = status
        if status == "completed":
            order["delivered_at"] = _now()
        _write(data)


def user_orders(user_id):
    with _LOCK:
        data = _read()
        products = {product["id"]: product["name"] for product in data["products"]}
        licenses = {license_item["id"]: license_item for license_item in data["licenses"]}
        result = []
        for order in data["orders"]:
            if order["telegram_id"] == int(user_id):
                license_item = licenses.get(order["license_id"], {})
                result.append({
                    "id": order["id"], "amount": Decimal(order["amount"]), "status": order["status"],
                    "created_at": order["created_at"],
                    "name": products.get(order["product_id"], "Produto removido"),
                    "encrypted_payload": license_item.get("encrypted_payload")
                })
        return sorted(result, key=lambda item: item["id"], reverse=True)


def get_failed_order(order_id):
    with _LOCK:
        data = _read()
        order = next(
            (item for item in data["orders"]
             if item["id"] == int(order_id) and item["status"] in {"delivery_failed", "delivery_pending"}),
            None
        )
        if not order:
            return None
        product = next((item for item in data["products"] if item["id"] == order["product_id"]), {})
        license_item = next((item for item in data["licenses"] if item["id"] == order["license_id"]), {})
        return {
            "id": order["id"], "telegram_id": order["telegram_id"],
            "license_id": order["license_id"], "name": product.get("name", "Produto"),
            "encrypted_payload": license_item.get("encrypted_payload")
        }


def parse_amount_cents(text):
    """Converte um valor em reais (ex.: 29,90) para centavos, com validação."""
    normalized = re.sub(r"^r\$\s*", "", text.strip(), flags=re.IGNORECASE).replace(",", ".")
    parts = normalized.split(".")
    if (
        len(normalized) > 16
        or len(parts) > 2
        or not parts[0].isdigit()
        or (len(parts) == 2 and (not parts[1].isdigit() or len(parts[1]) > 2))
    ):
        raise ValueError("Informe um valor positivo com até duas casas decimais.")
    amount = Decimal(normalized)
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Informe um valor positivo com até duas casas decimais.")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError("Informe um valor positivo com até duas casas decimais.")
    return int(cents)


def parse_money(text):
    return (Decimal(parse_amount_cents(text)) / Decimal(100)).quantize(Decimal("0.01"))


def parse_id(text, label):
    value = text.strip().lstrip("#")
    if not value.isdigit() or int(value) <= 0:
        raise ValueError(f"Envie apenas o número do {label}.")
    return int(value)


def parse_pix_amount_cents(text):
    cents = parse_amount_cents(text)
    if cents < MIN_PIX_DEPOSIT_CENTS:
        raise ValueError("O valor mínimo para recarga via Pix é R$ 10,00.")
    return int(cents)


def valid_cpf(value):
    digits = re.sub(r"\D", "", value)
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    first = sum(int(digits[index]) * (10 - index) for index in range(9))
    first_digit = (first * 10) % 11
    if first_digit == 10:
        first_digit = 0
    second = sum(int(digits[index]) * (11 - index) for index in range(10))
    second_digit = (second * 10) % 11
    if second_digit == 10:
        second_digit = 0
    return digits[-2:] == f"{first_digit}{second_digit}"


def configured_customer():
    """Pagador enviado à ROASClub, lido do .env. Nunca gera dados fictícios."""
    problems = pix_config_problems()
    if problems:
        raise PaymentAPIError("; ".join(problems))
    phone = ROASCLUB_CUSTOMER_PHONE
    if len(phone) in (10, 11):
        phone = "55" + phone
    return {
        "name": ROASCLUB_CUSTOMER_NAME,
        "email": ROASCLUB_CUSTOMER_EMAIL,
        "document": {"type": "cpf", "number": ROASCLUB_CUSTOMER_CPF},
        "phone": phone,
    }


def pix_config_problems():
    """Lista o que falta configurar para a recarga via Pix funcionar."""
    problems = []
    if not ROASCLUB_PUBLIC_KEY or not ROASCLUB_SECRET_KEY:
        problems.append("configure ROASCLUB_PUBLIC_KEY e ROASCLUB_SECRET_KEY")
    if len(ROASCLUB_CUSTOMER_NAME.split()) < 2:
        problems.append("configure ROASCLUB_CUSTOMER_NAME (nome e sobrenome)")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", ROASCLUB_CUSTOMER_EMAIL):
        problems.append("configure ROASCLUB_CUSTOMER_EMAIL com um e-mail válido")
    if not valid_cpf(ROASCLUB_CUSTOMER_CPF):
        problems.append("configure ROASCLUB_CUSTOMER_CPF com um CPF válido")
    if len(ROASCLUB_CUSTOMER_PHONE) not in (10, 11, 12, 13):
        problems.append("configure ROASCLUB_CUSTOMER_PHONE com DDD (ex.: 11999998888)")
    return problems


def roas_api_call(method, path, body=None):
    global _PAYMENT_LAST_REQUEST
    if not ROASCLUB_PUBLIC_KEY or not ROASCLUB_SECRET_KEY:
        raise PaymentAPIError("Configure ROASCLUB_PUBLIC_KEY e ROASCLUB_SECRET_KEY no .env.")
    credentials = f"{ROASCLUB_PUBLIC_KEY}:{ROASCLUB_SECRET_KEY}".encode("utf-8")
    headers = {
        "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii"),
        "Accept": "application/json",
    }
    encoded_body = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        ROASCLUB_API + path, data=encoded_body, headers=headers, method=method
    )
    is_pix_status_request = (
        method == "GET" and path.startswith("/v1/payment-transaction/info/")
    )
    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        global _PIX_STATUS_LAST_REQUEST
        with _PAYMENT_RATE_LOCK:
            elapsed = time.monotonic() - _PAYMENT_LAST_REQUEST
            minimum_interval = PIX_STATUS_MIN_INTERVAL if is_pix_status_request else 0.11
            if is_pix_status_request:
                elapsed = time.monotonic() - _PIX_STATUS_LAST_REQUEST
                if elapsed < minimum_interval:
                    time.sleep(minimum_interval - elapsed)
            elif elapsed < minimum_interval:
                time.sleep(minimum_interval - elapsed)
            _PAYMENT_LAST_REQUEST = time.monotonic()
            if is_pix_status_request:
                _PIX_STATUS_LAST_REQUEST = _PAYMENT_LAST_REQUEST
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict):
                raise PaymentAPIError("A ROASClub retornou uma resposta inválida.")
            return result
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace").strip()
            try:
                error_data = json.loads(response_body)
            except json.JSONDecodeError:
                error_data = {}
            if not isinstance(error_data, dict):
                error_data = {}
            detail = (
                error_data.get("error")
                or error_data.get("message")
                or error_data.get("error_messages")
            )
            if isinstance(detail, dict):
                detail = (
                    detail.get("message")
                    or detail.get("error")
                    or detail.get("code")
                    or json.dumps(detail, ensure_ascii=False)
                )
            if isinstance(detail, list):
                detail = "; ".join(
                    str(item.get("message") or item.get("error") or item)
                    if isinstance(item, dict) else str(item)
                    for item in detail[:3]
                )
            if not isinstance(detail, str) or not detail.strip():
                detail = response_body[:300] if response_body else f"HTTP {exc.code}"
            provider_rate_limit = "aguarde 2 segundos" in detail.lower()
            retryable = exc.code == 429 or exc.code >= 500 or provider_rate_limit
            if retryable and attempt + 1 < attempts:
                retry_after = exc.headers.get("Retry-After")
                try:
                    delay = (
                        max(float(retry_after), PIX_STATUS_MIN_INTERVAL)
                        if retry_after else
                        PIX_STATUS_MIN_INTERVAL if provider_rate_limit else attempt + 1
                    )
                except ValueError:
                    delay = PIX_STATUS_MIN_INTERVAL if provider_rate_limit else attempt + 1
                time.sleep(delay)
                continue
            raise PaymentAPIError(
                detail[:400], retryable=retryable, http_status=exc.code
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            if attempt + 1 < attempts:
                time.sleep(attempt + 1)
                continue
            raise PaymentAPIError(
                "Falha de conexão com a ROASClub.", retryable=True
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PaymentAPIError("A ROASClub retornou uma resposta inválida.") from exc
    raise PaymentAPIError("Falha de conexão com a ROASClub.", retryable=True)


def create_pix_charge(deposit, customer):
    amount = deposit["amount_cents"]
    if not isinstance(customer, dict) or not all(
        isinstance(customer.get(field), str) and customer[field].strip()
        for field in ("name", "email", "phone")
    ):
        fail_pix_deposit(deposit["id"])
        raise PaymentAPIError("Os dados necessários para criar a cobrança estão incompletos.")

    payload = {
        "payment_method": "pix",
        "amount": amount,
        "customer": customer,
        "items": [{
            "title": "Recarga de saldo Telegram",
            "unit_price": amount,
            "quantity": 1,
            "tangible": False,
            "external_ref": deposit["external_ref"],
        }],
        "pix": {"expires_in_days": PIX_DEPOSIT_EXPIRY_DAYS},
        "metadata": {
            "provider_name": "Telegram Store",
            "deposit_ref": deposit["external_ref"],
        },
    }
    response = roas_api_call(
        "POST",
        "/v1/payment-transaction/create",
        payload,
    )
    if not isinstance(response, dict):
        raise PaymentAPIError(
            "A ROASClub retornou uma resposta inválida. Confira o painel antes de tentar novamente.",
            retryable=True,
        )
    if response.get("success") is False:
        errors = response.get("error_messages")
        if not isinstance(errors, list):
            errors = [errors] if errors else ["A ROASClub recusou a transação."]
        raise PaymentAPIError(
            "; ".join(
                str(error.get("message") or error.get("error") or error)
                if isinstance(error, dict) else str(error)
                for error in errors[:3]
            ),
            http_status=400,
        )
    if not isinstance(response.get("data"), dict):
        raise PaymentAPIError(
            "Resposta sem dados da transação. Confira o painel da ROASClub antes de tentar novamente.",
            retryable=True,
        )
    try:
        return update_pix_deposit(deposit["id"], response["data"])
    except (TypeError, ValueError) as exc:
        raise PaymentAPIError(
            "Resposta sem ID, valor ou código Pix válido. Confira a transação no painel da ROASClub.",
            retryable=True,
        ) from exc


def fetch_pix_deposit_status(deposit):
    transaction_id = deposit.get("transaction_id")
    if not transaction_id:
        raise PaymentAPIError("ID de transação indisponível.")
    encoded_id = urllib.parse.quote(str(transaction_id), safe="")
    response = roas_api_call(
        "GET", f"/v1/payment-transaction/info/{encoded_id}"
    )
    transaction = response.get("data", response)
    if not isinstance(transaction, dict):
        raise PaymentAPIError("Resposta de consulta da ROASClub inválida.", retryable=True)
    if str(transaction.get("id", "")) != str(transaction_id):
        raise PaymentAPIError("A ROASClub retornou outro ID de transação.")
    return transaction


def check_pix_payment(deposit):
    transaction = fetch_pix_deposit_status(deposit)
    status = transaction.get("status")
    if status in {
        "FAILED", "REFUSED", "EXPIRED", "ERROR",
        "REFUNDED", "CHARGEBACK", "PRECHARGEBACK",
    }:
        fail_pix_deposit(deposit["id"])
        return False
    if status != "PAID":
        return False
    try:
        return confirm_pix_deposit(
            deposit["id"], status, transaction.get("amount")
        )
    except (TypeError, ValueError) as exc:
        raise PaymentAPIError(
            "O valor pago não corresponde à recarga solicitada."
        ) from exc


# Comunicação com a API do Telegram
API = f"https://api.telegram.org/bot{BOT_TOKEN}/"


def call(method, data=None, timeout=45):
    payload = urllib.parse.urlencode(data or {}).encode("utf-8")
    request = urllib.request.Request(API + method, data=payload if data is not None else None)
    retryable = method in {"getUpdates", "editMessageText"}
    attempts = 3 if retryable else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
            if attempt + 1 == attempts:
                raise
            time.sleep(attempt + 1)
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Erro na API do Telegram"))
    return result.get("result")


def get_updates(offset=None, timeout=30):
    args = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
    if offset is not None:
        args["offset"] = offset
    return call("getUpdates", args, timeout=timeout + 10)


def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    data = {"chat_id": chat_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return call("sendMessage", data)


def send_photo(chat_id, photo_data, caption, reply_markup=None, parse_mode="HTML"):
    boundary = "----TelegramBot" + uuid.uuid4().hex
    fields = {
        "chat_id": str(chat_id),
        "caption": caption,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    parts = []
    for name, value in fields.items():
        parts.extend([
            f"--{boundary}\r\n".encode("ascii"),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    parts.extend([
        f"--{boundary}\r\n".encode("ascii"),
        b'Content-Disposition: form-data; name="photo"; filename="pix-qr.png"\r\n',
        b"Content-Type: image/png\r\n\r\n",
        photo_data,
        b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ])
    request = urllib.request.Request(
        API + "sendPhoto",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {body}") from exc
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Erro ao enviar o QR Pix pelo Telegram."))
    return result.get("result")


def delete_message(chat_id, message_id):
    return call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def _is_not_modified(exc):
    return "message is not modified" in str(exc).lower()


def _message_unavailable(exc):
    message = str(exc).lower()
    return any(
        fragment in message
        for fragment in ("message to edit not found", "message can't be edited", "message_id_invalid")
    )


def send_new_panel(chat_id, text, reply_markup=None, replace_message_id=None):
    """Envia um novo painel de texto, torna-o o painel ativo e remove o anterior."""
    result = send_message(chat_id, text, reply_markup)
    if not isinstance(result, dict) or result.get("message_id") is None:
        raise RuntimeError("Telegram não retornou o ID do novo painel.")
    new_message_id = result["message_id"]
    remember_active_menu(chat_id, new_message_id, media="text")
    if replace_message_id is not None and replace_message_id != new_message_id:
        try:
            delete_message(chat_id, replace_message_id)
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
        call("editMessageCaption" if is_photo else "editMessageText", data)
    except RuntimeError as exc:
        if _is_not_modified(exc):
            return message_id
        raise
    return message_id


def answer_callback(callback_id, text=None, show_alert=False):
    data = {"callback_query_id": callback_id, "show_alert": str(show_alert).lower()}
    if text:
        data["text"] = text
    try:
        return call("answerCallbackQuery", data)
    except RuntimeError as exc:
        message = str(exc).lower()
        if "query is too old" in message or "query id is invalid" in message:
            return False
        raise


# Teclados
def inline(rows):
    return {"inline_keyboard": rows}


def main_menu(is_admin=False):
    rows = [
        [{"text": "🛍 Produtos", "callback_data": "catalog"}],
        [{"text": "👤 Meu perfil", "callback_data": "profile"},
         {"text": "📦 Minhas compras", "callback_data": "history"}],
        [{"text": "💰 Adicionar saldo", "callback_data": "wallet:deposit"}],
    ]
    if SUPPORT_USERNAME:
        rows.append([{"text": "💬 Suporte", "url": f"https://t.me/{SUPPORT_USERNAME}"}])
    if is_admin:
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


def pix_expiration_label(deposit):
    expires_at = deposit.get("expires_at")
    if not expires_at:
        created_at = datetime.fromisoformat(deposit["created_at"])
        expires_at = (created_at + timedelta(days=PIX_DEPOSIT_EXPIRY_DAYS)).isoformat()
    expiration = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    if expiration.tzinfo is not None:
        expiration = expiration.astimezone()
    return expiration.strftime("%d/%m/%Y às %H:%M")


def pix_deposit_text(deposit):
    amount = Decimal(deposit["amount_cents"]) / 100
    return (
        "💠 <b>Pagamento Pix</b>\n\n"
        f"💳 <b>Valor do Pix:</b> {money(amount)}\n"
        f"💰 <b>Valor que será adicionado ao saldo:</b> {money(amount)}\n\n"
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
        f"💳 <b>Valor:</b> {money(amount)}\n"
        f"💰 <b>Crédito após confirmação:</b> {money(amount)}\n\n"
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


def pix_creation_pending_keyboard():
    rows = []
    if SUPPORT_USERNAME:
        rows.append([{"text": "💬 Chamar suporte", "url": f"https://t.me/{SUPPORT_USERNAME}"}])
    rows.append([{"text": "🏠 Menu principal", "callback_data": "home"}])
    return inline(rows)


def pix_confirmed_text(deposit, balance=None):
    credit = Decimal(deposit["amount_cents"]) / 100
    text = (
        "✅ <b>Recarga Pix confirmada</b>\n\n"
        f"💰 Saldo adicionado: <b>{money(credit)}</b>"
    )
    if balance is not None:
        text += f"\n🏦 Saldo atual: <b>{money(balance)}</b>"
    return text


def edit_active_panel(chat_id, text, reply_markup, keep_photo=False):
    """Atualiza o painel ativo do chat; se ele não existir mais, envia um novo."""
    message_id = active_menu_message_id(chat_id)
    if message_id is None:
        return send_new_panel(chat_id, text, reply_markup)
    try:
        return edit_message(chat_id, message_id, text, reply_markup, keep_photo=keep_photo)
    except RuntimeError as exc:
        if _message_unavailable(exc):
            return send_new_panel(chat_id, text, reply_markup)
        raise


def purchase_actions():
    rows = []
    if SUPPORT_USERNAME:
        rows.append([{"text": "💬 Chamar suporte", "url": f"https://t.me/{SUPPORT_USERNAME}"}])
    rows.append([{"text": "⬅️ Voltar ao menu principal", "callback_data": "home"}])
    return inline(rows)


def purchase_complete_menu():
    return inline([[{"text": "⬅️ Voltar ao menu", "callback_data": "home"}]])


ACTIVE_MENUS = {}
ACTIVE_PIX_DEPOSITS = {}


def active_menu_is_photo(chat_id, message_id=None):
    if message_id is None:
        message_id = active_menu_message_id(chat_id)
    if message_id is None:
        return False
    data = _read()
    return (
        data["active_menus"].get(str(chat_id)) == message_id
        and data["active_menu_media"].get(str(chat_id)) == "photo"
    )


def active_menu_message_id(chat_id):
    message_id = ACTIVE_MENUS.get(chat_id)
    if message_id is None:
        message_id = _read()["active_menus"].get(str(chat_id))
        if type(message_id) is int and message_id > 0:
            ACTIVE_MENUS[chat_id] = message_id
        else:
            return None
    return message_id


def remember_active_menu(chat_id, message_id, media=None):
    ACTIVE_MENUS[chat_id] = message_id
    with _LOCK:
        data = _read()
        data["active_menus"][str(chat_id)] = int(message_id)
        if media is not None:
            data["active_menu_media"][str(chat_id)] = media
        _write(data)


def order_history_view(user_id, index):
    orders = user_orders(user_id)
    if not orders:
        return "Você ainda não realizou compras.", back_menu()
    if index < 0 or index >= len(orders):
        raise ValueError("Pedido do histórico não encontrado.")

    order = orders[index]
    if not order["encrypted_payload"]:
        raise ValueError("Os dados de acesso deste pedido não estão disponíveis.")
    payload = json.loads(decrypt_text(order["encrypted_payload"]))
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
    return text, inline(rows)


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


# Fluxo do bot
STATES = {}


def is_admin(user_id):
    return user_id in ADMIN_IDS


def money(value):
    return f"R$ {Decimal(value):.2f}".replace(".", ",")


def home_text(user_id):
    user = get_user(user_id) or {"balance": "0.00"}
    lines = [
        "📦 <b>Seja bem-vindo(a) a maior loja de logins do telegram!</b>\n",
        "🛍 | <b>Qualidade:</b> Logins",
        "✅ | <b>Garantia:</b> Acesso garantido.\n",
    ]
    if STORE_CHANNEL:
        lines.append(f"🔔 | <b>Canal: @{escape(STORE_CHANNEL)}</b>")
    if SUPPORT_USERNAME:
        lines.append(f"👤 | <b>Suporte: @{escape(SUPPORT_USERNAME)}</b>")
    if STORE_CHANNEL or SUPPORT_USERNAME:
        lines[-1] += "\n"
    lines.extend([
        "<b>🏦 Carteira:</b>",
        f"<b> ├ ID:</b> <code>{user_id}</code>",
        f"<b> ├💰 Saldo: {money(user['balance'])}</b>",
    ])
    return "\n".join(lines)


def home(chat_id, user_id, message_id=None, new_message=False):
    text = home_text(user_id)
    markup = main_menu(is_admin(user_id))
    if new_message:
        send_new_panel(chat_id, text, markup)
        return
    if message_id is None:
        edit_active_panel(chat_id, text, markup)
        return
    try:
        edit_message(chat_id, message_id, text, markup)
    except RuntimeError as exc:
        if not _message_unavailable(exc):
            raise
        send_new_panel(chat_id, text, markup)


def admin_only(user_id):
    if user_id not in ADMIN_IDS:
        raise PermissionError("Acesso restrito ao administrador.")


def send_license(chat_id, order_id, product_name, encrypted_payload):
    text = license_message_text(product_name, encrypted_payload)
    edit_active_panel(chat_id, text, purchase_complete_menu())
    set_order_status(order_id, "completed")


def license_message_text(product_name, encrypted_payload):
    payload = json.loads(decrypt_text(encrypted_payload))
    return (
        "✅ <b>COMPRA CONFIRMADA</b>\n\n"
        f"📦 <b>{escape(product_name)}</b>\n\n"
        "🔐 <b>Dados de acesso</b>\n\n"
        f"🌐 <b>Site:</b>\n<code>{escape(str(payload['url']))}</code>\n\n"
        f"👤 <b>Login:</b>\n<code>{escape(str(payload['login']))}</code>\n\n"
        f"🔑 <b>Senha:</b>\n<code>{escape(str(payload['password']))}</code>"
    )


def show_pix_creation_result(chat_id, deposit, customer):
    try:
        deposit = create_pix_charge(deposit, customer)
    except PaymentAPIError as exc:
        print(
            "Falha na criação da transação ROASClub:",
            f"HTTP {exc.http_status}" if exc.http_status else "sem status HTTP",
            exc.code
        )
        if exc.retryable:
            mark_pix_deposit_uncertain(deposit["id"])
            ACTIVE_PIX_DEPOSITS[chat_id] = deposit["id"]
            edit_active_panel(
                chat_id,
                pix_creation_pending_text()
                + f"\n\n<b>Referência:</b> <code>{escape(deposit['external_ref'])}</code>",
                pix_creation_pending_keyboard()
            )
        else:
            fail_pix_deposit(deposit["id"])
            ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
            detail = escape(exc.detail) if exc.detail else "Verifique as credenciais e tente novamente."
            api_status = f"HTTP {exc.http_status} — " if exc.http_status else ""
            edit_active_panel(
                chat_id,
                f"Não foi possível criar o Pix na ROASClub.\n\n{api_status}{detail}",
                pix_back_menu()
            )
        return
    except Exception as exc:
        print(
            "Falha inesperada durante a criação da cobrança Pix:",
            type(exc).__name__,
            str(exc)[:300],
        )
        mark_pix_deposit_uncertain(deposit["id"])
        ACTIVE_PIX_DEPOSITS[chat_id] = deposit["id"]
        edit_active_panel(
            chat_id,
            pix_creation_pending_text()
            + f"\n\n<b>Referência:</b> <code>{escape(deposit['external_ref'])}</code>",
            pix_creation_pending_keyboard()
        )
        return
    ACTIVE_PIX_DEPOSITS[chat_id] = deposit["id"]
    try:
        show_pix_qr_panel(chat_id, deposit)
    except (RuntimeError, ValueError, OSError, qrcode.exceptions.DataOverflowError) as exc:
        print("Não foi possível exibir a imagem do QR Pix; usando o código copia e cola:", exc)
        edit_active_panel(chat_id, pix_deposit_text(deposit), pix_deposit_keyboard(deposit))


def show_pix_qr_panel(chat_id, deposit):
    photo_data = pix_qr_png(deposit["pix_code"])
    old_message_id = active_menu_message_id(chat_id)
    result = send_photo(
        chat_id,
        photo_data,
        pix_deposit_caption(deposit),
        pix_deposit_keyboard(deposit),
    )
    if not isinstance(result, dict) or result.get("message_id") is None:
        raise RuntimeError("Telegram não retornou o ID da mensagem com o QR Pix.")
    photo_message_id = result["message_id"]
    remember_active_menu(chat_id, photo_message_id, media="photo")
    if old_message_id is not None and old_message_id != photo_message_id:
        try:
            delete_message(chat_id, old_message_id)
        except Exception as exc:
            print("Não foi possível remover o painel anterior após exibir o QR Pix:", exc)


def show_pix_payment_confirmation(chat_id, deposit):
    user = get_user(deposit["telegram_id"])
    balance = user["balance"] if user else None
    ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
    edit_active_panel(chat_id, pix_confirmed_text(deposit, balance), pix_back_menu())


def notify_pix_payment_confirmation(deposit):
    chat_id = deposit["telegram_id"]
    if ACTIVE_PIX_DEPOSITS.get(chat_id) == deposit["id"]:
        try:
            show_pix_payment_confirmation(chat_id, deposit)
            return
        except Exception as exc:
            print("Não foi possível atualizar o painel da recarga Pix:", exc)
    try:
        edit_active_panel(
            chat_id,
            pix_confirmed_text(deposit, (get_user(chat_id) or {}).get("balance")),
            pix_back_menu()
        )
    except Exception as exc:
        print("Não foi possível notificar o usuário sobre a recarga Pix:", exc)


def notify_pix_deposit_failure(deposit):
    chat_id = deposit["telegram_id"]
    text = "⚠️ Esta cobrança Pix não foi confirmada e nenhum saldo foi adicionado. Você pode iniciar uma nova recarga."
    if ACTIVE_PIX_DEPOSITS.get(chat_id) == deposit["id"]:
        try:
            ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
            edit_active_panel(chat_id, text, pix_back_menu())
            return
        except Exception as exc:
            print("Não foi possível atualizar o painel da cobrança Pix:", exc)
    try:
        edit_active_panel(chat_id, text, pix_back_menu())
    except Exception as exc:
        print("Não foi possível notificar falha na cobrança Pix:", exc)


def show_pix_status(chat_id, message_id, deposit, note):
    """Mostra um aviso sobre a cobrança sem remover o QR Code da tela."""
    if active_menu_is_photo(chat_id, message_id):
        edit_message(
            chat_id, message_id, pix_deposit_caption(deposit) + "\n\n" + note,
            pix_deposit_keyboard(deposit), keep_photo=True
        )
    else:
        edit_message(
            chat_id, message_id, note + "\n\n" + pix_deposit_text(deposit), pix_deposit_keyboard(deposit)
        )


def request_pix_amount(chat_id, user_id):
    problems = pix_config_problems()
    if problems:
        print("Recarga Pix indisponível:", "; ".join(problems))
        edit_active_panel(
            chat_id,
            "A recarga via Pix está indisponível no momento. Fale com o suporte.",
            back_menu()
        )
        return
    ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
    STATES[user_id] = {"state": "pix_amount", "data": {}}
    edit_active_panel(
        chat_id,
        "💰 <b>Adicionar saldo via Pix</b>\n\n"
        "Envie apenas o valor que deseja depositar — nenhum dado pessoal é solicitado, "
        "a cobrança é gerada automaticamente.\n\n"
        "O valor integral será adicionado ao saldo após a confirmação do pagamento.\n\n"
        "Valor mínimo: <b>R$ 10,00</b>.\n\nUse /cancel para cancelar.",
        pix_back_menu()
    )


def reconcile_pix_deposits():
    for deposit in pending_pix_deposits():
        try:
            if check_pix_payment(deposit):
                confirmed = get_pix_deposit(deposit["id"])
                if confirmed:
                    notify_pix_payment_confirmation(confirmed)
            else:
                updated = get_pix_deposit(deposit["id"])
                if updated and updated["status"] == "failed":
                    notify_pix_deposit_failure(updated)
        except PaymentAPIError as exc:
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
    ensure_user(user)
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
            edit_active_panel(chat_id, "Você não tem permissão para acessar essa função.", back_menu())
            return
        edit_active_panel(chat_id, "<b>Painel administrativo</b>", admin_menu())
        return
    if command == "/saldo":
        if not is_admin(user_id):
            edit_active_panel(chat_id, "Você não tem permissão para acessar essa função.", back_menu())
            return
        parts = text.split()
        if len(parts) != 3:
            edit_active_panel(chat_id, "Use: <code>/saldo ID_TELEGRAM VALOR</code>", back_menu())
            return
        try:
            target_id = parse_id(parts[1], "ID Telegram")
            amount = parse_money(parts[2])
            add_balance(target_id, amount)
            edit_active_panel(chat_id, f"Saldo de {target_id} atualizado em {money(amount)}.", admin_menu())
            try:
                edit_active_panel(
                    target_id, f"💰 Seu saldo recebeu um crédito de <b>{money(amount)}</b>.", back_menu()
                )
            except Exception as notify_exc:
                print("Não foi possível notificar o usuário sobre o crédito:", notify_exc)
        except Exception as exc:
            print("Falha ao adicionar saldo pelo comando:", exc)
            detail = escape(str(exc)) if isinstance(exc, ValueError) else (
                "Não foi possível concluir a operação."
            )
            try:
                edit_active_panel(
                    chat_id,
                    f"⚠️ {detail}\nConfira o ID e o valor e tente novamente.",
                    admin_menu()
                )
            except Exception as notify_exc:
                print("Não foi possível avisar o administrador:", notify_exc)
        return

    state = STATES.get(user_id)
    if not state:
        if command:
            edit_active_panel(chat_id, "Use /start para abrir o menu principal.", back_menu())
        return
    step = state["state"]
    if not text:
        edit_active_panel(chat_id, "Envie a resposta como texto ou use /cancel.", back_menu())
        return
    try:
        if step == "product_name":
            state["data"]["name"] = text[:180]
            state["state"] = "product_description"
            edit_active_panel(chat_id, "Envie a descrição do produto:", back_menu())
        elif step == "product_description":
            state["data"]["description"] = text
            state["state"] = "product_price"
            edit_active_panel(chat_id, "Envie o preço em reais. Exemplo: 29,90", back_menu())
        elif step == "product_price":
            price = parse_money(text)
            product_id = create_product(state["data"]["name"], state["data"]["description"], price)
            STATES.pop(user_id, None)
            edit_active_panel(
                chat_id, f"Produto #{product_id} cadastrado. Agora adicione licenças pelo painel.", admin_menu()
            )
        elif step == "license_product":
            product_id = parse_id(text, "produto")
            product = get_product(product_id)
            if not product:
                raise ValueError("Produto não encontrado ou inativo.")
            state["data"]["product_id"] = product_id
            state["state"] = "license_url"
            edit_active_panel(chat_id, "Envie a URL de acesso:", back_menu())
        elif step == "license_url":
            state["data"]["url"] = text
            state["state"] = "license_login"
            edit_active_panel(chat_id, "Envie o login:", back_menu())
        elif step == "license_login":
            state["data"]["login"] = text
            state["state"] = "license_password"
            edit_active_panel(chat_id, "Envie a senha:", back_menu())
        elif step == "license_password":
            state["data"]["password"] = text
            encrypted = encrypt_text(json.dumps(state["data"], ensure_ascii=False))
            add_license(state["data"]["product_id"], encrypted)
            STATES.pop(user_id, None)
            edit_active_panel(chat_id, "✅ Licença adicionada ao estoque.", admin_menu())
        elif step == "balance_user":
            target_id = parse_id(text, "ID Telegram")
            if not get_user(target_id):
                raise ValueError("Usuário não encontrado. Peça para ele iniciar o bot com /start.")
            state["data"]["user_id"] = target_id
            state["state"] = "balance_amount"
            edit_active_panel(chat_id, "Envie o valor do crédito em reais:", back_menu())
        elif step == "balance_amount":
            amount = parse_money(text)
            add_balance(state["data"]["user_id"], amount)
            STATES.pop(user_id, None)
            edit_active_panel(chat_id, f"Saldo adicionado: {money(amount)}.", admin_menu())
        elif step == "pix_amount":
            try:
                amount_cents = parse_pix_amount_cents(text)
                customer = configured_customer()
            except PaymentAPIError as exc:
                print("Recarga Pix indisponível:", exc.code)
                STATES.pop(user_id, None)
                edit_active_panel(
                    chat_id, "A recarga via Pix está indisponível no momento. Fale com o suporte.", back_menu()
                )
                return
            except (InvalidOperation, ValueError) as exc:
                edit_active_panel(
                    chat_id,
                    f"Valor inválido: {escape(str(exc))}\nEnvie o valor que deseja pagar via Pix.",
                    back_menu()
                )
                return
            deposit = start_pix_deposit(user_id, amount_cents)
            STATES.pop(user_id, None)
            show_pix_creation_result(chat_id, deposit, customer)
        elif step == "resend_order":
            order_id = parse_id(text, "pedido")
            order = get_failed_order(order_id)
            if not order or not order["encrypted_payload"]:
                raise ValueError("Pedido pendente/falho não encontrado.")
            send_license(order["telegram_id"], order["id"], order["name"], order["encrypted_payload"])
            STATES.pop(user_id, None)
            edit_active_panel(chat_id, "Entrega reenviada.", admin_menu())
    except (ValueError, InvalidOperation) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else "valor inválido."
        edit_active_panel(
            chat_id,
            f"⚠️ {escape(detail)}\nTente novamente ou use /cancel.",
            back_menu()
        )
    except Exception as exc:
        print("Falha ao processar mensagem:", exc)
        try:
            edit_active_panel(
                chat_id,
                "Não foi possível concluir agora. Tente novamente em instantes ou use /cancel.",
                back_menu()
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
    ensure_user(user)
    if message_id is not None:
        remember_active_menu(chat_id, message_id, media="photo" if message.get("photo") else "text")
    if data != "wallet:deposit" and not data.startswith("pix:"):
        ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
    try:
        try:
            answer_callback(
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
            products = list_products()
            if not products:
                edit_message(chat_id, message_id, "Ainda não há produtos disponíveis.", back_menu())
            else:
                lines = ["<b>Produtos disponíveis</b>\n"]
                for product in products:
                    lines.append(
                        f"<b>{escape(product['name'])}</b> — {money(product['price'])} | Estoque: {product['stock']}"
                    )
                    if product["description"]:
                        lines.append(escape(product["description"]))
                    lines.append("")
                catalog_text = "\n".join(lines)
                if len(catalog_text) > 4000:
                    catalog_text = "<b>Produtos disponíveis</b>\n\nEscolha um produto abaixo para ver os detalhes."
                edit_message(chat_id, message_id, catalog_text, product_buttons(products))
        elif data.startswith("product:"):
            product_id = int(data.split(":")[1])
            product = get_product(product_id)
            if not product:
                raise ValueError("Produto indisponível.")
            edit_message(
                chat_id, message_id,
                f"<b>{escape(product['name'])}</b>\n\n{escape(product['description'] or '')}\n\n"
                f"Preço: <b>{money(product['price'])}</b>\n\nConfirmar compra?",
                confirm_purchase(product_id)
            )
        elif data.startswith("buy:"):
            product_id = int(data.split(":")[1])
            result = purchase(user_id, product_id)
            try:
                delivery_text = license_message_text(
                    result["product"]["name"], result["encrypted_payload"]
                )
                edit_message(chat_id, message_id, delivery_text, purchase_complete_menu())
                set_order_status(result["order_id"], "completed")
            except Exception as exc:
                set_order_status(result["order_id"], "delivery_failed")
                print("Falha na entrega:", exc)
                try:
                    edit_message(
                        chat_id, message_id,
                        f"⚠️ O pagamento do pedido #{result['order_id']} foi registrado, mas a entrega está pendente. "
                        "Entre em contato com o suporte para receber sua compra.",
                        purchase_actions()
                    )
                except Exception as notify_exc:
                    print("Não foi possível exibir as opções de suporte:", notify_exc)
                    return
        elif data == "profile":
            user_record = get_user(user_id)
            edit_message(
                chat_id, message_id,
                f"<b>Meu perfil</b>\nID: <code>{user_id}</code>\nSaldo: <b>{money(user_record['balance'])}</b>",
                back_menu()
            )
        elif data == "history":
            text, keyboard = order_history_view(user_id, 0)
            edit_message(chat_id, message_id, text, keyboard)
        elif data == "history:page":
            return
        elif data.startswith("history:"):
            index = int(data.split(":", 1)[1])
            text, keyboard = order_history_view(user_id, index)
            edit_message(chat_id, message_id, text, keyboard)
        elif data.startswith("pix:retry:"):
            deposit_id = data.split(":", 2)[2]
            deposit = get_pix_deposit(deposit_id)
            if not deposit or deposit["telegram_id"] != int(user_id):
                raise ValueError("Cobrança Pix não encontrada.")
            if deposit["status"] == "pending":
                ACTIVE_PIX_DEPOSITS[chat_id] = deposit_id
                edit_message(chat_id, message_id, pix_deposit_text(deposit), pix_deposit_keyboard(deposit))
            elif deposit["status"] == "creation_unknown":
                edit_message(
                    chat_id, message_id,
                    pix_creation_pending_text()
                    + f"\n\n<b>Referência:</b> <code>{escape(deposit['external_ref'])}</code>",
                    pix_creation_pending_keyboard()
                )
            elif deposit["status"] == "confirmed":
                edit_message(chat_id, message_id, pix_confirmed_text(deposit), pix_back_menu())
            else:
                edit_message(
                    chat_id, message_id,
                    "Esta cobrança Pix não está mais disponível. Inicie uma nova recarga pelo menu.",
                    pix_back_menu()
                )
        elif data.startswith("pix:check:"):
            deposit_id = data.split(":", 2)[2]
            deposit = get_pix_deposit(deposit_id)
            if not deposit or deposit["telegram_id"] != int(user_id):
                raise ValueError("Cobrança Pix não encontrada.")
            if deposit["status"] == "confirmed":
                edit_message(chat_id, message_id, pix_confirmed_text(deposit), pix_back_menu())
            elif deposit["status"] == "pending":
                ACTIVE_PIX_DEPOSITS[chat_id] = deposit_id
                try:
                    is_confirmed = check_pix_payment(deposit)
                except PaymentAPIError as exc:
                    print("Falha na verificação manual do Pix:", exc.code)
                    show_pix_status(
                        chat_id, message_id, deposit,
                        "⚠️ <b>Não foi possível consultar o pagamento agora.</b> "
                        "Seu Pix continua válido; tente verificar novamente em alguns segundos."
                    )
                else:
                    if is_confirmed:
                        confirmed = get_pix_deposit(deposit_id)
                        edit_message(
                            chat_id, message_id,
                            pix_confirmed_text(confirmed, get_user(user_id)["balance"]),
                            pix_back_menu()
                        )
                        ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
                    else:
                        updated = get_pix_deposit(deposit_id)
                        if updated and updated["status"] == "failed":
                            ACTIVE_PIX_DEPOSITS.pop(chat_id, None)
                            edit_message(
                                chat_id, message_id,
                                "⚠️ Esta cobrança Pix não foi confirmada e nenhum saldo foi adicionado. "
                                "Você pode iniciar uma nova recarga.",
                                pix_back_menu()
                            )
                        else:
                            show_pix_status(
                                chat_id, message_id, deposit,
                                f"⏳ <b>Pagamento ainda não confirmado</b> ({datetime.now():%H:%M:%S}). "
                                "Se você já pagou, aguarde o processamento e verifique novamente."
                            )
            elif deposit["status"] == "creating":
                ACTIVE_PIX_DEPOSITS[chat_id] = deposit_id
                edit_message(
                    chat_id, message_id,
                    pix_creation_pending_text(),
                    pix_creation_pending_keyboard()
                )
            else:
                edit_message(
                    chat_id, message_id,
                    "Esta cobrança Pix não está mais disponível. Inicie uma nova recarga pelo menu.",
                    pix_back_menu()
                )
        elif data == "admin":
            admin_only(user_id)
            edit_message(chat_id, message_id, "<b>Painel administrativo</b>", admin_menu())
        elif data == "adm:add_product":
            admin_only(user_id)
            STATES[user_id] = {"state": "product_name", "data": {}}
            edit_active_panel(chat_id, "Envie o nome do produto. Use /cancel para cancelar.", back_menu())
        elif data == "adm:add_license":
            admin_only(user_id)
            STATES[user_id] = {"state": "license_product", "data": {}}
            edit_active_panel(chat_id, "Envie o ID do produto que receberá a licença:", back_menu())
        elif data == "adm:add_balance":
            admin_only(user_id)
            STATES[user_id] = {"state": "balance_user", "data": {}}
            edit_active_panel(chat_id, "Envie o ID Telegram do usuário:", back_menu())
        elif data == "adm:resend":
            admin_only(user_id)
            STATES[user_id] = {"state": "resend_order", "data": {}}
            edit_active_panel(chat_id, "Envie o número do pedido que deseja reenviar:", back_menu())
    except PermissionError:
        edit_message(chat_id, message_id, "Você não tem permissão para acessar essa função.", back_menu())
    except ValueError as exc:
        # Mensagens de regra de negócio (saldo insuficiente, estoque esgotado...) vão para o usuário.
        print("Operação recusada:", exc)
        try:
            edit_message(chat_id, message_id, f"⚠️ {escape(str(exc))}", back_menu())
        except Exception as notify_exc:
            print("Não foi possível atualizar a mensagem do menu:", notify_exc)
    except Exception as exc:
        print("Falha ao processar botão:", exc)
        try:
            edit_message(
                chat_id, message_id,
                "Não foi possível concluir agora. Tente novamente em instantes.",
                back_menu()
            )
        except Exception as notify_exc:
            print("Não foi possível atualizar a mensagem do menu:", notify_exc)


def startup_checks():
    errors = config_errors()
    if errors:
        print("Configuração incompleta no arquivo .env:")
        for error in errors:
            print(" -", error)
        return False
    try:
        _read()
    except (RuntimeError, OSError) as exc:
        print("Não foi possível abrir o arquivo de dados:", exc)
        return False
    try:
        me = call("getMe")
    except Exception as exc:
        print("Não foi possível conectar ao Telegram. Confira o BOT_TOKEN e a internet.")
        print("Detalhe:", exc)
        return False
    print(f"Conectado como @{me.get('username')} (id {me.get('id')}).")
    try:
        # getUpdates não funciona enquanto houver webhook configurado.
        call("deleteWebhook", {"drop_pending_updates": "false"})
    except Exception as exc:
        print("Aviso: não foi possível remover um webhook existente:", exc)
    problems = pix_config_problems()
    if problems:
        print("Aviso: recarga via Pix desativada —", "; ".join(problems) + ".")
    if os.name != "nt":
        print("Aviso: o cadastro e a exibição de licenças usam DPAPI e só funcionam no Windows.")
    return True


def process_update(update):
    if "message" in update:
        handle_message(update["message"])
    elif "callback_query" in update:
        handle_callback(update["callback_query"])


def main():
    if not startup_checks():
        raise SystemExit(1)
    print("Telegram Store iniciado. Dados em:", _PATH)
    offset = None
    last_payment_check = 0
    while True:
        try:
            updates = get_updates(offset=offset, timeout=POLLING_TIMEOUT)
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    process_update(update)
                except Exception as exc:
                    print(f"Erro ao processar a atualização {update['update_id']}:", exc)
        except KeyboardInterrupt:
            print("\nBot encerrado.")
            break
        except Exception as exc:
            print("Erro no loop:", exc)
            if "terminated by other getupdates request" in str(exc).lower():
                print("Outra cópia do bot está rodando com o mesmo token. Feche-a antes de continuar.")
            time.sleep(3)
        if time.monotonic() - last_payment_check >= PAYMENT_CHECK_INTERVAL:
            try:
                reconcile_pix_deposits()
            except KeyboardInterrupt:
                print("\nBot encerrado.")
                break
            except Exception as exc:
                print("Erro ao verificar cobranças Pix:", exc)
            last_payment_check = time.monotonic()


if __name__ == "__main__":
    main()