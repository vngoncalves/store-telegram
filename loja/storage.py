"""Armazenamento em JSON: usuários, produtos, licenças, pedidos e cobranças Pix."""
import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from . import config


_LOCK = threading.RLock()


_PATH = Path(config.DATA_FILE)


if not _PATH.is_absolute():
    _PATH = config.BASE_DIR / _PATH


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
    if type(amount_cents) is not int or amount_cents < config.MIN_PIX_DEPOSIT_CENTS:
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
            expires_at = (created_at + timedelta(days=config.PIX_DEPOSIT_EXPIRY_DAYS)).isoformat(timespec="seconds")
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
