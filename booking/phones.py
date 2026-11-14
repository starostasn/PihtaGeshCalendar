"""Телефоны: приведение к единому виду, чтобы один номер в любом написании находил одного гостя."""

import re

_DIGITS = re.compile(r"\D")


def normalize_phone(raw: str) -> str:
    """'8 (912) 345-67-89', '+7 912 3456789', '9123456789' -> '+79123456789'.

    Пустая строка — если номера нет. Иностранные номера (с '+', не 7) сохраняются как '+цифры'.
    Возвращает '' и для явно неполных номеров (меньше 10 цифр).
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    digits = _DIGITS.sub("", raw)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    if raw.startswith("+") and 10 <= len(digits) <= 15:
        return "+" + digits
    return ""


def format_phone(phone: str) -> str:
    """'+79123456789' -> '+7 912 345-67-89'."""
    if phone.startswith("+7") and len(phone) == 12:
        d = phone[2:]
        return f"+7 {d[:3]} {d[3:6]}-{d[6:8]}-{d[8:]}"
    return phone


def phone_search_variants(query: str) -> list[str]:
    """Варианты цифр для поиска по части номера.

    '912 34' -> ['91234']; '8 912 34' -> ['891234', '91234'] (8 или 7 в начале может быть кодом страны).
    """
    digits = _DIGITS.sub("", query or "")
    if len(digits) < 3:
        return []
    variants = [digits]
    if digits[0] in "78" and len(digits) >= 4:
        variants.append(digits[1:])
    return variants
