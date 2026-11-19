"""Configuração lida do arquivo .env."""
import os
import re
from pathlib import Path


# Raiz do projeto (pasta acima de loja/), onde ficam o .env e a pasta data/.
BASE_DIR = Path(__file__).resolve().parent.parent


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


# Catálogo editável (produtos + estoque de acessos). O bot relê este arquivo a cada ação.
PRODUCTS_FILE = os.getenv("PRODUCTS_FILE", "produtos.json").strip() or "produtos.json"


# Canal de logs (ex.: -1001234567890). Também pode ser definido no painel admin → ⚙️ Configurações.
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "").strip()


POLLING_TIMEOUT = _int_env("POLLING_TIMEOUT", 30, minimum=1, maximum=50)


ROASCLUB_PUBLIC_KEY = os.getenv("ROASCLUB_PUBLIC_KEY", "").strip()


ROASCLUB_SECRET_KEY = os.getenv("ROASCLUB_SECRET_KEY", "").strip()


ROASCLUB_CUSTOMER_NAME = os.getenv("ROASCLUB_CUSTOMER_NAME", "").strip()


ROASCLUB_CUSTOMER_EMAIL = os.getenv("ROASCLUB_CUSTOMER_EMAIL", "").strip()


ROASCLUB_CUSTOMER_CPF = re.sub(r"\D", "", os.getenv("ROASCLUB_CUSTOMER_CPF", ""))


ROASCLUB_CUSTOMER_PHONE = re.sub(r"\D", "", os.getenv("ROASCLUB_CUSTOMER_PHONE", ""))


ROASCLUB_API = "https://api2.roasclub.xyz"


PAYMENT_CHECK_INTERVAL = 30


PIX_DEPOSIT_EXPIRY_DAYS = 1


MIN_PIX_DEPOSIT_CENTS = 1000


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
