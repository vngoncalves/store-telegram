"""Armazenamento em JSON.

- data/store.json: usuários, pedidos, carteira e cobranças Pix.
- produtos.json: catálogo e estoque de acessos (editável pelo painel admin ou à mão).
"""
import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import config, security


_LOCK = threading.RLock()


_PATH = Path(config.DATA_FILE)


if not _PATH.is_absolute():
    _PATH = config.BASE_DIR / _PATH


_LEGACY_PATH = _PATH.with_suffix(".txt") if _PATH.suffix.lower() == ".json" else None


_PRODUCTS_PATH = Path(config.PRODUCTS_FILE)


if not _PRODUCTS_PATH.is_absolute():
    _PRODUCTS_PATH = config.BASE_DIR / _PRODUCTS_PATH


_EMPTY = {
    "meta": {"version": 1, "next_ids": {"product": 1, "license": 1, "order": 1, "transaction": 1}},
    "users": {}, "products": [], "licenses": [], "orders": [], "wallet_transactions": [],
    "pix_deposits": [], "active_menus": {}, "active_menu_media": {}, "settings": {}
}


# Valores iniciais das configurações editáveis pelo painel admin (⚙️ Configurações).
DEFAULT_SETTINGS = {
    "welcome_bonus": "10.00",           # saldo dado a cada usuário novo (0 = desativado)
    "referral_bonus": "5.00",           # pago a quem indicou, uma vez por indicado (0 = desativado)
    "referral_requires_deposit": False,  # False: paga assim que o indicado entra; True: só após o 1º depósito
    "log_channel_id": "",               # canal de logs (vazio = usa LOG_CHANNEL_ID do .env)
    "home_image_url": "",               # imagem do menu inicial (vazio = usa HOME_IMAGE_URL do .env)
    "catalog_image_url": "",            # imagem da vitrine "🛒 Logins" (Comprar Unitário)
    "profile_image_url": "",            # imagem da tela "👤 Meu perfil"
    "deposit_image_url": "",            # imagem da tela "💰 Adicionar saldo"
    "home_notices": (                   # avisos do menu inicial, um por linha
        "🌐 Acesse sempre via navegador 4G!\n"
        "📧 Apenas Logins antigos e com pedidos concluídos.\n"
        "🔑 Altere a senha após o login.\n"
        "⏱️ Prazo de verificação: 10 minutos!"
    ),
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


def _write(data, path=None):
    path = path or _PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="store_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        for attempt in range(5):
            try:
                os.replace(temp_name, path)
                break
            except PermissionError:
                # Antivírus/indexador do Windows pode segurar o arquivo por alguns instantes.
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


_INSTANCE_LOCK = None


def acquire_instance_lock():
    """Impede duas cópias do bot gravando nos mesmos arquivos ao mesmo tempo.

    Retorna True se conseguiu a trava. A trava some sozinha quando o processo termina.
    """
    global _INSTANCE_LOCK
    lock_path = _PATH.parent / ".bot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _INSTANCE_LOCK = handle
    return True


def backup_data(keep=15):
    """Copia store.json e produtos.json para data/backups/AAAAMMDD-HHMMSS/ e mantém os últimos `keep`."""
    import shutil
    folder = _PATH.parent / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    copied = []
    for source in (_PATH, _PRODUCTS_PATH):
        if source.exists():
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, folder / source.name)
            copied.append(source.name)
    backups = sorted(path for path in (_PATH.parent / "backups").glob("*") if path.is_dir())
    for old in backups[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    return folder if copied else None


def _next(data, kind):
    value = int(data["meta"]["next_ids"][kind])
    data["meta"]["next_ids"][kind] = value + 1
    return value


def ensure_user(tg_user):
    with _LOCK:
        data = _read()
        uid = str(tg_user["id"])
        created = uid not in data["users"]
        user = data["users"].setdefault(uid, {"balance": "0.00", "created_at": _now()})
        profile = {
            "telegram_id": int(tg_user["id"]),
            "username": tg_user.get("username"),
            "first_name": tg_user.get("first_name"),
            "last_name": tg_user.get("last_name"),
            "language_code": tg_user.get("language_code"),
            "is_premium": bool(tg_user.get("is_premium")),
            "is_bot": bool(tg_user.get("is_bot")),
        }
        now = datetime.now().astimezone()
        try:
            last_seen = datetime.fromisoformat(user.get("last_seen", ""))
        except ValueError:
            last_seen = None
        seen_recently = last_seen is not None and now - last_seen < timedelta(hours=1)
        unchanged = all(user.get(key) == value for key, value in profile.items())
        if not created and unchanged and seen_recently and not user.get("blocked"):
            return False  # nada mudou: não regrava o arquivo a cada clique
        # Atualiza só os dados do Telegram; saldo, indicação etc. são preservados.
        user.update(profile)
        user["last_seen"] = _now()  # atualizado no máximo 1x por hora
        user["blocked"] = False     # voltou a falar com o bot: pode receber transmissões de novo
        if not unchanged or created:
            user["updated_at"] = _now()
        if created:
            # Bônus só depois da liberação (complete_onboarding), ex.: após entrar no canal obrigatório.
            user["onboarded"] = False
        _write(data)
        return created


def complete_onboarding(user_id):
    """Libera o usuário novo uma única vez: paga as boas-vindas e o bônus de indicação (se for na entrada).

    Retorna None se ele já tinha sido liberado; senão {"welcome", "referral_bonus", "referrer_id"}.
    Usuários antigos (sem o campo "onboarded") contam como já liberados.
    """
    with _LOCK:
        data = _read()
        user = data["users"].get(str(user_id))
        if not user or user.get("onboarded", True):
            return None
        user["onboarded"] = True
        settings = _settings(data)
        welcome = Decimal(settings["welcome_bonus"])
        if welcome > 0:
            _credit(data, user_id, welcome, "Bônus de boas-vindas")
            user["welcome_bonus"] = str(welcome)
        referral = Decimal("0.00")
        if not settings["referral_requires_deposit"]:
            referral = _pay_referral_bonus(data, user_id)
        _write(data)
        return {"welcome": welcome if welcome > 0 else Decimal("0.00"), "referral_bonus": referral,
                "referrer_id": user.get("referred_by")}


def _settings(data):
    return {**DEFAULT_SETTINGS, **data.get("settings", {})}


def get_settings():
    with _LOCK:
        settings = _settings(_read())
        return {
            "welcome_bonus": Decimal(settings["welcome_bonus"]),
            "referral_bonus": Decimal(settings["referral_bonus"]),
            "referral_requires_deposit": bool(settings["referral_requires_deposit"]),
            "log_channel_id": str(settings.get("log_channel_id") or ""),
            "home_image_url": str(settings.get("home_image_url") or ""),
            "catalog_image_url": str(settings.get("catalog_image_url") or ""),
            "profile_image_url": str(settings.get("profile_image_url") or ""),
            "deposit_image_url": str(settings.get("deposit_image_url") or ""),
            "home_notices": str(settings.get("home_notices") or ""),
        }


def update_settings(**changes):
    allowed = set(DEFAULT_SETTINGS)
    with _LOCK:
        data = _read()
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"Configuração desconhecida: {key}")
            if key.endswith("_bonus"):
                value = Decimal(value)
                if not value.is_finite() or value < 0:
                    raise ValueError("O valor não pode ser negativo.")
                value = str(value.quantize(Decimal("0.01")))
            elif key == "log_channel_id" or key.endswith("_image_url"):
                value = str(value or "").strip()
            elif key == "home_notices":
                value = "\n".join(line.strip() for line in str(value or "").splitlines() if line.strip())
            else:
                value = bool(value)
            data["settings"][key] = value
        _write(data)
    return get_settings()


# ---------------------------------------------------------------------------
# Indicação (afiliados)
# ---------------------------------------------------------------------------

def _credit(data, user_id, amount, description):
    user = data["users"][str(user_id)]
    user["balance"] = str((Decimal(user["balance"]) + amount).quantize(Decimal("0.01")))
    user["updated_at"] = _now()
    _add_transaction(data, user_id, amount, description)


def _pay_referral_bonus(data, referred_id):
    """Paga o bônus fixo ao padrinho uma única vez por indicado. Retorna o valor pago."""
    referred = data["users"][str(referred_id)]
    referrer_id = referred.get("referred_by")
    bonus = Decimal(_settings(data)["referral_bonus"])
    if not referrer_id or referred.get("referral_bonus_paid") or bonus <= 0:
        return Decimal("0.00")
    if str(referrer_id) not in data["users"]:
        return Decimal("0.00")
    referred["referral_bonus_paid"] = True
    _credit(data, referrer_id, bonus, f"Bônus de indicação ({referred_id})")
    _add_referral_earning(data, referrer_id, bonus)
    return bonus


def _add_referral_earning(data, referrer_id, amount):
    referrer = data["users"][str(referrer_id)]
    total = Decimal(referrer.get("referral_earnings", "0.00")) + amount
    referrer["referral_earnings"] = str(total.quantize(Decimal("0.01")))


def set_referrer(user_id, referrer_id):
    """Liga um usuário novo a quem o indicou. Retorna True se ligou.

    O bônus é pago depois: na liberação do indicado (complete_onboarding) ou no 1º depósito dele.
    """
    with _LOCK:
        data = _read()
        user = data["users"].get(str(user_id))
        if (
            not user or int(referrer_id) == int(user_id) or user.get("referred_by")
            or str(referrer_id) not in data["users"]
        ):
            return None
        user["referred_by"] = int(referrer_id)
        user["referred_at"] = _now()
        user["referral_bonus_paid"] = False
        _write(data)
        return True


def referral_stats(user_id):
    with _LOCK:
        data = _read()
        user = data["users"].get(str(user_id)) or {}
        referred = [item for item in data["users"].values() if item.get("referred_by") == int(user_id)]
        depositors = {
            deposit["telegram_id"] for deposit in data["pix_deposits"] if deposit["status"] == "confirmed"
        }
        return {
            "count": len(referred),
            "active": sum(1 for item in referred if item["telegram_id"] in depositors),
            "earnings": Decimal(user.get("referral_earnings", "0.00")),
        }


def count_users():
    with _LOCK:
        return len(_read()["users"])


def broadcast_targets():
    """IDs de quem pode receber transmissões (exclui quem bloqueou o bot e outros bots)."""
    with _LOCK:
        return [
            int(uid) for uid, user in _read()["users"].items()
            if not user.get("blocked") and not user.get("is_bot")
        ]


def user_stats():
    with _LOCK:
        users = list(_read()["users"].values())
    blocked = sum(1 for user in users if user.get("blocked"))
    return {"total": len(users), "blocked": blocked, "reachable": len(users) - blocked}


def mark_blocked(user_ids):
    """Marca quem bloqueou o bot (ou apagou a conta); deixa de receber transmissões até voltar."""
    user_ids = {str(uid) for uid in user_ids}
    if not user_ids:
        return
    with _LOCK:
        data = _read()
        for uid in user_ids:
            if uid in data["users"]:
                data["users"][uid]["blocked"] = True
                data["users"][uid]["blocked_at"] = _now()
        _write(data)


def deposit_count(user_id):
    with _LOCK:
        return sum(
            1 for deposit in _read()["pix_deposits"]
            if deposit["telegram_id"] == int(user_id) and deposit["status"] == "confirmed"
        )


def purchase_stats(user_id):
    with _LOCK:
        orders = [order for order in _read()["orders"] if order["telegram_id"] == int(user_id)]
        return {"count": len(orders), "spent": sum((Decimal(order["amount"]) for order in orders), Decimal("0.00"))}


def get_user(user_id):
    with _LOCK:
        return _read()["users"].get(str(user_id))


# ---------------------------------------------------------------------------
# Catálogo (produtos.json)
#
# Formato:
# {
#   "proximo_id": {"produto": 3, "acesso": 10},
#   "produtos": [
#     {"id": 1, "nome": "...", "descricao": "...", "preco": "29.90", "ativo": true,
#      "acessos": [{"id": 1, "dados": "<criptografado>", "criado_em": "..."}]}
#   ]
# }
# "acessos" guarda só o estoque disponível: cada venda retira um item da lista.
# Para cadastrar à mão, um acesso pode ser escrito em texto puro
# ({"url": "...", "login": "...", "senha": "..."}); o bot o criptografa na próxima leitura.
# ---------------------------------------------------------------------------

_EMPTY_PRODUCTS = {"proximo_id": {"produto": 1, "acesso": 1}, "produtos": []}


def _money_str(value, label):
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"Preço inválido em {label}: {value!r}.") from exc
    if not amount.is_finite() or amount <= 0:
        raise RuntimeError(f"Preço inválido em {label}: {value!r}.")
    return str(amount.quantize(Decimal("0.01")))


def _normalize_products(data):
    """Valida o catálogo e completa o que foi escrito à mão. Retorna True se algo mudou."""
    path = _PRODUCTS_PATH
    if not isinstance(data, dict) or not isinstance(data.get("produtos", []), list):
        raise RuntimeError(f"O arquivo {path} deve ter o formato {{\"produtos\": [...]}}.")
    changed = False
    if "produtos" not in data:
        data["produtos"] = []
        changed = True
    counters = data.get("proximo_id")
    if not isinstance(counters, dict):
        counters = {}
        data["proximo_id"] = counters
        changed = True
    products = data["produtos"]
    used_products = {p.get("id") for p in products if isinstance(p, dict) and type(p.get("id")) is int}
    used_access = {
        a.get("id") for p in products if isinstance(p, dict)
        for a in (p.get("acessos") or []) if isinstance(a, dict) and type(a.get("id")) is int
    }
    for kind, used in (("produto", used_products), ("acesso", used_access)):
        minimum = max(used, default=0) + 1
        value = counters.get(kind)
        if type(value) is not int or value < minimum:
            counters[kind] = minimum
            changed = True

    seen_ids = set()
    for index, product in enumerate(products, start=1):
        label = f"produto nº {index} de {path.name}"
        if not isinstance(product, dict):
            raise RuntimeError(f"O {label} deve ser um objeto JSON.")
        if type(product.get("id")) is not int or product["id"] < 1:
            product["id"] = counters["produto"]
            counters["produto"] += 1
            changed = True
        if product["id"] in seen_ids:
            raise RuntimeError(f"ID de produto repetido em {path.name}: {product['id']}.")
        seen_ids.add(product["id"])
        name = product.get("nome")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError(f"O {label} precisa de um \"nome\".")
        if not isinstance(product.get("descricao"), str):
            product["descricao"] = "" if product.get("descricao") is None else str(product["descricao"])
            changed = True
        price = _money_str(product.get("preco"), f"\"{name}\"")
        if product.get("preco") != price:
            product["preco"] = price
            changed = True
        if not isinstance(product.get("ativo"), bool):
            product["ativo"] = True
            changed = True
        image = product.get("imagem")
        if image is None:
            product["imagem"] = ""
            changed = True
        elif not isinstance(image, str):
            raise RuntimeError(f"\"imagem\" de \"{name}\" deve ser um link (texto).")
        elif image != image.strip():
            product["imagem"] = image.strip()
            changed = True
        accesses = product.get("acessos")
        if accesses is None:
            product["acessos"] = accesses = []
            changed = True
        if not isinstance(accesses, list):
            raise RuntimeError(f"\"acessos\" de \"{name}\" deve ser uma lista.")
        for position, access in enumerate(accesses, start=1):
            if not isinstance(access, dict):
                raise RuntimeError(f"O acesso nº {position} de \"{name}\" deve ser um objeto JSON.")
            if not isinstance(access.get("dados"), str) or not access["dados"]:
                login, password = access.get("login"), access.get("senha", access.get("password"))
                if not login or not password:
                    raise RuntimeError(
                        f"O acesso nº {position} de \"{name}\" precisa de \"login\" e \"senha\"."
                    )
                plain = {"url": str(access.get("url") or ""), "login": str(login), "password": str(password)}
                info = {key: access[key] for key in ("pedidos", "valor_total") if key in access}
                accesses[position - 1] = access = {
                    "dados": security.encrypt_text(json.dumps(plain, ensure_ascii=False)), **info
                }
                changed = True
            if "pedidos" in access and (type(access["pedidos"]) is not int or access["pedidos"] < 0):
                if isinstance(access["pedidos"], str) and access["pedidos"].strip().isdigit():
                    access["pedidos"] = int(access["pedidos"])
                    changed = True
                else:
                    raise RuntimeError(f"\"pedidos\" do acesso nº {position} de \"{name}\" deve ser um número inteiro.")
            if "valor_total" in access:
                try:
                    total = Decimal(str(access["valor_total"]).replace(",", "."))
                    if not total.is_finite() or total < 0:
                        raise InvalidOperation
                except (InvalidOperation, ValueError) as exc:
                    raise RuntimeError(
                        f"\"valor_total\" do acesso nº {position} de \"{name}\" é inválido."
                    ) from exc
                normalized = str(total.quantize(Decimal("0.01")))
                if access["valor_total"] != normalized:
                    access["valor_total"] = normalized
                    changed = True
            if type(access.get("id")) is not int or access["id"] < 1:
                access["id"] = counters["acesso"]
                counters["acesso"] += 1
                changed = True
            if "criado_em" not in access:
                access["criado_em"] = _now()
                changed = True
    return changed


def _migrate_products():
    """Primeira execução: move produtos/licenças antigos do store.json para produtos.json."""
    store = _read()
    catalog = json.loads(json.dumps(_EMPTY_PRODUCTS))
    licenses = store.get("licenses", [])
    for product in store.get("products", []):
        catalog["produtos"].append({
            "id": product["id"], "nome": product["name"],
            "descricao": product.get("description") or "", "preco": product["price"],
            "ativo": product.get("active", True),
            "acessos": [
                {"id": item["id"], "dados": item["encrypted_payload"], "criado_em": item.get("created_at", _now())}
                for item in licenses
                if item["product_id"] == product["id"] and item["status"] == "available"
            ],
        })
    if store.get("products"):
        names = {product["id"]: product["name"] for product in store["products"]}
        by_id = {item["id"]: item for item in licenses}
        for order in store["orders"]:
            order.setdefault("product_name", names.get(order["product_id"], "Produto removido"))
            if not order.get("encrypted_payload") and order.get("license_id") in by_id:
                order["encrypted_payload"] = by_id[order["license_id"]]["encrypted_payload"]
        _normalize_products(catalog)
        _write(catalog, _PRODUCTS_PATH)
        store["products"] = []
        store["licenses"] = [item for item in licenses if item["status"] != "available"]
        _write(store)
    else:
        _write(catalog, _PRODUCTS_PATH)
    return catalog


def _read_products():
    if not _PRODUCTS_PATH.exists():
        _migrate_products()
    try:
        data = json.loads(_PRODUCTS_PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"O arquivo {_PRODUCTS_PATH.name} está com JSON inválido (linha {getattr(exc, 'lineno', '?')}). "
            "Corrija o arquivo; o bot volta a ler sozinho."
        ) from exc
    if _normalize_products(data):
        _write(data, _PRODUCTS_PATH)
    return data


def _write_products(data):
    _write(data, _PRODUCTS_PATH)


def _find_product(catalog, product_id):
    return next((item for item in catalog["produtos"] if item["id"] == int(product_id)), None)


def _public_product(product):
    return {
        "id": product["id"], "name": product["nome"], "description": product["descricao"],
        "price": Decimal(product["preco"]), "active": product["ativo"],
        "stock": len(product["acessos"]), "image": product.get("imagem") or "",
    }


def list_products(include_inactive=False):
    with _LOCK:
        products = [
            _public_product(product) for product in _read_products()["produtos"]
            if include_inactive or product["ativo"]
        ]
        return sorted(products, key=lambda item: item["id"], reverse=True)


def get_product(product_id, include_inactive=False):
    with _LOCK:
        product = _find_product(_read_products(), product_id)
        if not product or not (include_inactive or product["ativo"]):
            return None
        return _public_product(product)


def create_product(name, description, price):
    with _LOCK:
        catalog = _read_products()
        product_id = catalog["proximo_id"]["produto"]
        catalog["proximo_id"]["produto"] += 1
        catalog["produtos"].append({
            "id": product_id, "nome": name, "descricao": description,
            "preco": str(Decimal(price).quantize(Decimal("0.01"))),
            "ativo": True, "imagem": "", "criado_em": _now(), "acessos": []
        })
        _write_products(catalog)
        return product_id


def update_product(product_id, name=None, description=None, price=None, active=None, image=None):
    with _LOCK:
        catalog = _read_products()
        product = _find_product(catalog, product_id)
        if not product:
            raise ValueError("Produto não encontrado.")
        if name is not None:
            product["nome"] = name
        if description is not None:
            product["descricao"] = description
        if price is not None:
            product["preco"] = str(Decimal(price).quantize(Decimal("0.01")))
        if active is not None:
            product["ativo"] = bool(active)
        if image is not None:  # "" remove a imagem
            product["imagem"] = str(image).strip()
        _write_products(catalog)
        return _public_product(product)


def delete_product(product_id):
    """Remove o produto e o estoque restante. Pedidos antigos continuam com seus dados."""
    with _LOCK:
        catalog = _read_products()
        product = _find_product(catalog, product_id)
        if not product:
            raise ValueError("Produto não encontrado.")
        catalog["produtos"].remove(product)
        _write_products(catalog)
        return len(product["acessos"])


def add_licenses(product_id, encrypted_payloads):
    """Adiciona vários acessos ao estoque de uma vez. Retorna o novo estoque.

    Cada item é o texto criptografado ou um dict {"dados", "pedidos", "valor_total"}.
    """
    with _LOCK:
        catalog = _read_products()
        product = _find_product(catalog, product_id)
        if not product:
            raise ValueError("Produto não encontrado.")
        for payload in encrypted_payloads:
            entry = dict(payload) if isinstance(payload, dict) else {"dados": payload}
            entry.update({"id": catalog["proximo_id"]["acesso"], "criado_em": _now()})
            product["acessos"].append(entry)
            catalog["proximo_id"]["acesso"] += 1
        _write_products(catalog)
        return len(product["acessos"])


def add_license(product_id, encrypted_payload):
    return add_licenses(product_id, [encrypted_payload])


def list_accesses(product_id):
    """Estoque do produto na ordem de venda (o primeiro é o próximo a ser entregue)."""
    with _LOCK:
        product = _find_product(_read_products(), product_id)
        if not product:
            raise ValueError("Produto não encontrado.")
        return [dict(access) for access in product["acessos"]]


def public_accesses(product_id, include_inactive=False):
    """Estoque como o cliente vê: só ID e informações do login, nunca as credenciais."""
    with _LOCK:
        product = _find_product(_read_products(), product_id)
        if not product or not (include_inactive or product["ativo"]):
            return []
        return [
            {
                "id": access["id"],
                "orders": access.get("pedidos"),
                "total": Decimal(access["valor_total"]) if access.get("valor_total") is not None else None,
            }
            for access in product["acessos"]
        ]


def remove_access(product_id, access_id):
    with _LOCK:
        catalog = _read_products()
        product = _find_product(catalog, product_id)
        if not product:
            raise ValueError("Produto não encontrado.")
        access = next((item for item in product["acessos"] if item["id"] == int(access_id)), None)
        if not access:
            raise ValueError("Esse acesso não está mais no estoque (pode ter sido vendido).")
        product["acessos"].remove(access)
        _write_products(catalog)
        return len(product["acessos"])


def remove_licenses(product_id, count=None):
    """Retira do estoque os acessos mais recentes (count=None esvazia). Retorna (removidos, estoque)."""
    with _LOCK:
        catalog = _read_products()
        product = _find_product(catalog, product_id)
        if not product:
            raise ValueError("Produto não encontrado.")
        accesses = product["acessos"]
        if not accesses:
            raise ValueError("O estoque deste produto já está vazio.")
        removed = len(accesses) if count is None else min(int(count), len(accesses))
        del accesses[len(accesses) - removed:]
        _write_products(catalog)
        return removed, len(accesses)


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
        referrer_id = user.get("referred_by")
        if referrer_id and str(referrer_id) in data["users"]:
            # Bônus de indicação no 1º depósito, no mesmo salvamento do crédito.
            bonus = _pay_referral_bonus(data, deposit["telegram_id"])
            if bonus > 0:
                deposit["referral_reward"] = {
                    "referrer_id": int(referrer_id), "bonus": str(bonus), "notified": False,
                }
        _write(data)
        return True


def take_referral_notification(deposit_id):
    """Retorna a recompensa de indicação ainda não avisada e a marca como avisada."""
    with _LOCK:
        data = _read()
        deposit = next((item for item in data["pix_deposits"] if item["id"] == deposit_id), None)
        reward = (deposit or {}).get("referral_reward")
        if not reward or reward.get("notified"):
            return None
        reward["notified"] = True
        _write(data)
        return {**reward, "referred_id": deposit["telegram_id"]}


def purchase(user_id, product_id, access_id=None):
    """Compra um login. access_id = o login que o cliente escolheu; None = o mais antigo do estoque."""
    with _LOCK:
        data = _read()
        user = data["users"].get(str(user_id))
        if not user:
            raise ValueError("Usuário não encontrado.")
        catalog = _read_products()
        product = _find_product(catalog, product_id)
        if not product or not product["ativo"]:
            raise ValueError("Produto indisponível.")
        price = Decimal(product["preco"])
        balance = Decimal(user["balance"])
        if balance < price:
            raise ValueError("Saldo insuficiente.")
        if not product["acessos"]:
            raise ValueError("Estoque esgotado.")
        if access_id is None:
            position = 0
        else:
            position = next(
                (index for index, item in enumerate(product["acessos"]) if item["id"] == int(access_id)), None
            )
            if position is None:
                raise ValueError("Esse login acabou de ser vendido. Escolha outro na lista.")
        # Retira o login do estoque antes de cobrar.
        access = product["acessos"].pop(position)
        _write_products(catalog)
        try:
            order_id = _next(data, "order")
            user["balance"] = str((balance - price).quantize(Decimal("0.01")))
            user["updated_at"] = _now()
            order = {
                "id": order_id, "telegram_id": int(user_id), "product_id": product["id"],
                "product_name": product["nome"], "license_id": access["id"],
                "encrypted_payload": access["dados"], "amount": str(price),
                "access_info": {key: access[key] for key in ("pedidos", "valor_total") if key in access},
                "status": "delivery_pending", "created_at": _now(), "delivered_at": None
            }
            data["orders"].append(order)
            _add_transaction(data, user_id, -price, f"Compra #{order_id}")
            _write(data)
        except Exception:
            # Não conseguiu registrar a venda: devolve o acesso ao estoque.
            catalog = _read_products()
            product_now = _find_product(catalog, product["id"])
            if product_now is not None:
                product_now["acessos"].insert(position, access)
                _write_products(catalog)
            raise
        return {
            "order_id": order_id,
            "product": _public_product(product),
            "encrypted_payload": access["dados"],
            "access_id": access["id"],
            "access_info": order["access_info"],
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
        result = []
        for order in data["orders"]:
            if order["telegram_id"] == int(user_id):
                result.append({
                    "id": order["id"], "amount": Decimal(order["amount"]), "status": order["status"],
                    "created_at": order["created_at"],
                    "name": _order_product_name(data, order),
                    "encrypted_payload": _order_payload(data, order)
                })
        return sorted(result, key=lambda item: item["id"], reverse=True)


def _order_product_name(data, order):
    if order.get("product_name"):
        return order["product_name"]
    legacy = next((item for item in data["products"] if item["id"] == order["product_id"]), None)
    return legacy["name"] if legacy else "Produto removido"


def _order_payload(data, order):
    """Pedidos novos guardam o acesso entregue; os antigos apontam para store.json → licenses."""
    if order.get("encrypted_payload"):
        return order["encrypted_payload"]
    legacy = next((item for item in data["licenses"] if item["id"] == order.get("license_id")), None)
    return legacy["encrypted_payload"] if legacy else None


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
        return {
            "id": order["id"], "telegram_id": order["telegram_id"],
            "license_id": order["license_id"], "name": _order_product_name(data, order),
            "amount": Decimal(order["amount"]),
            "encrypted_payload": _order_payload(data, order)
        }
