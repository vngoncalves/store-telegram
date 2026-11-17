"""Conversão e validação de valores digitados (dinheiro, IDs, CPF)."""
import re
from decimal import Decimal

from . import config


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
    if cents < config.MIN_PIX_DEPOSIT_CENTS:
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


def money(value):
    return f"R$ {Decimal(value):.2f}".replace(".", ",")
