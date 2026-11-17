"""Integração com a API de cobranças Pix da ROASClub."""
import base64
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, storage, validators


_PAYMENT_LAST_REQUEST = 0.0


_PAYMENT_RATE_LOCK = threading.Lock()


_PIX_STATUS_LAST_REQUEST = 0.0


class PaymentAPIError(RuntimeError):
    def __init__(self, code, retryable=False, http_status=None, detail=None):
        super().__init__(str(detail or code))
        self.code = str(code)
        self.retryable = retryable
        self.http_status = http_status
        self.detail = str(detail)[:400] if detail else None


def configured_customer():
    """Pagador enviado à ROASClub, lido do .env. Nunca gera dados fictícios."""
    problems = pix_config_problems()
    if problems:
        raise PaymentAPIError("; ".join(problems))
    phone = config.ROASCLUB_CUSTOMER_PHONE
    if len(phone) in (10, 11):
        phone = "55" + phone
    return {
        "name": config.ROASCLUB_CUSTOMER_NAME,
        "email": config.ROASCLUB_CUSTOMER_EMAIL,
        "document": {"type": "cpf", "number": config.ROASCLUB_CUSTOMER_CPF},
        "phone": phone,
    }


def pix_config_problems():
    """Lista o que falta configurar para a recarga via Pix funcionar."""
    problems = []
    if not config.ROASCLUB_PUBLIC_KEY or not config.ROASCLUB_SECRET_KEY:
        problems.append("configure ROASCLUB_PUBLIC_KEY e ROASCLUB_SECRET_KEY")
    if len(config.ROASCLUB_CUSTOMER_NAME.split()) < 2:
        problems.append("configure ROASCLUB_CUSTOMER_NAME (nome e sobrenome)")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", config.ROASCLUB_CUSTOMER_EMAIL):
        problems.append("configure ROASCLUB_CUSTOMER_EMAIL com um e-mail válido")
    if not validators.valid_cpf(config.ROASCLUB_CUSTOMER_CPF):
        problems.append("configure ROASCLUB_CUSTOMER_CPF com um CPF válido")
    if len(config.ROASCLUB_CUSTOMER_PHONE) not in (10, 11, 12, 13):
        problems.append("configure ROASCLUB_CUSTOMER_PHONE com DDD (ex.: 11999998888)")
    return problems


def roas_api_call(method, path, body=None):
    global _PAYMENT_LAST_REQUEST
    if not config.ROASCLUB_PUBLIC_KEY or not config.ROASCLUB_SECRET_KEY:
        raise PaymentAPIError("Configure ROASCLUB_PUBLIC_KEY e ROASCLUB_SECRET_KEY no .env.")
    credentials = f"{config.ROASCLUB_PUBLIC_KEY}:{config.ROASCLUB_SECRET_KEY}".encode("utf-8")
    headers = {
        "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii"),
        "Accept": "application/json",
    }
    encoded_body = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        config.ROASCLUB_API + path, data=encoded_body, headers=headers, method=method
    )
    is_pix_status_request = (
        method == "GET" and path.startswith("/v1/payment-transaction/info/")
    )
    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        global _PIX_STATUS_LAST_REQUEST
        with _PAYMENT_RATE_LOCK:
            elapsed = time.monotonic() - _PAYMENT_LAST_REQUEST
            minimum_interval = config.PIX_STATUS_MIN_INTERVAL if is_pix_status_request else 0.11
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
                        max(float(retry_after), config.PIX_STATUS_MIN_INTERVAL)
                        if retry_after else
                        config.PIX_STATUS_MIN_INTERVAL if provider_rate_limit else attempt + 1
                    )
                except ValueError:
                    delay = config.PIX_STATUS_MIN_INTERVAL if provider_rate_limit else attempt + 1
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
        storage.fail_pix_deposit(deposit["id"])
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
        "pix": {"expires_in_days": config.PIX_DEPOSIT_EXPIRY_DAYS},
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
        return storage.update_pix_deposit(deposit["id"], response["data"])
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
        storage.fail_pix_deposit(deposit["id"])
        return False
    if status != "PAID":
        return False
    try:
        return storage.confirm_pix_deposit(
            deposit["id"], status, transaction.get("amount")
        )
    except (TypeError, ValueError) as exc:
        raise PaymentAPIError(
            "O valor pago não corresponde à recarga solicitada."
        ) from exc
