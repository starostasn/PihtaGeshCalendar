"""Интеграция с API Авито (краткосрочная аренда).

Что делает:
- загружает брони с Авито: даты, статус, гостя (имя, телефон, e-mail), стоимость проживания,
  предоплату и фактическую комиссию Авито — и дополняет ими брони календаря;
- закрывает на Авито даты прямых броней и броней с других площадок (сразу после изменения);
- по желанию отправляет на Авито цены и минимальный срок из раздела «Цены».

Доступ — через приложение на developers.avito.ru: владелец объявления входит в Авито и разрешает
доступ (OAuth authorization_code), токены обновляются сами. Ключи приложения лежат только
в .env сервера; токены хранятся в базе в зашифрованном виде.

Методы и поля сверены со снимком официального каталога API Авито (октябрь 2026):
GET  /realty/v1/accounts/{user_id}/items/{item_id}/bookings — брони (status: active / pending / canceled);
POST /core/v1/accounts/{user_id}/items/{item_id}/bookings — занятость (конец периода открытый);
POST /realty/v1/accounts/{user_id}/items/{item_id}/prices — цены (date_to включительно).
"""

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from .merge import same_platform
from .models import AvitoAccount, Booking, Channel, Guest, House, Payment, SyncLog
from .notify import send_telegram
from .phones import normalize_phone
from .pricing import PriceCalculator

log = logging.getLogger(__name__)

API = "https://api.avito.ru"
TOKEN_URL = f"{API}/token"
AUTH_URL = "https://avito.ru/oauth"
SCOPES = ["short_term_rent:read", "short_term_rent:write", "user:read", "items:info"]
SOURCE = "PihtaGeshCalendar"
DAYS_AHEAD = 365
CALENDAR_REFRESH = timedelta(hours=6)  # даже без изменений периодически повторяем отправку
PRICES_REFRESH = timedelta(hours=24)
TOKEN_MARGIN = timedelta(minutes=10)

STATUS_MAP = {
    "active": Booking.Status.CONFIRMED,
    "pending": Booking.Status.REQUEST,  # бронь создана, но гость ещё не оплатил
    "canceled": Booking.Status.CANCELLED,
}


def credentials_configured() -> bool:
    return bool(settings.AVITO_CLIENT_ID and settings.AVITO_CLIENT_SECRET)


def redirect_uri() -> str:
    from django.urls import reverse

    return settings.PUBLIC_BASE_URL.rstrip("/") + reverse("avito_callback")


def authorize_url(state: str) -> str:
    query = urlencode(
        {"response_type": "code", "client_id": settings.AVITO_CLIENT_ID, "scope": ",".join(SCOPES), "state": state},
        safe=",:",
    )
    return f"{AUTH_URL}?{query}"


def item_id_from_url(url: str) -> int | None:
    """…/doma_dachi_kottedzhi/2-k._dom_80m_3753751223 → 3753751223."""
    m = re.search(r"_(\d{6,})(?:[/?#]|$)", url or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- шифрование токенов


def _fernet() -> Fernet:
    key = hashlib.sha256(("avito-tokens:" + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:  # сменился SECRET_KEY — нужно подключиться заново
        return ""


# ---------------------------------------------------------------- клиент API


class AvitoError(Exception):
    def __init__(self, message: str, status: int | None = None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class AvitoAuthError(AvitoError):
    """Доступ потерян или ключи неверные — нужно подключиться заново."""


def _payload(resp) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"data": data}


def _error_text(resp, data: dict) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        text = err.get("message") or err.get("code")
    else:
        text = data.get("error_description") or err or data.get("message")
    return f"HTTP {resp.status_code}: {text or (resp.text or '')[:200]}"


class AvitoClient:
    def __init__(self, account: AvitoAccount, http=None):
        self.account = account
        self.http = http or requests.Session()

    # ---- токены

    def _token_request(self, data: dict) -> dict:
        if not credentials_configured():
            raise AvitoAuthError("Не заданы AVITO_CLIENT_ID и AVITO_CLIENT_SECRET в .env сервера")
        resp = self.http.request(
            "POST",
            TOKEN_URL,
            data={**data, "client_id": settings.AVITO_CLIENT_ID, "client_secret": settings.AVITO_CLIENT_SECRET},
            timeout=settings.SYNC_HTTP_TIMEOUT,
        )
        payload = _payload(resp)
        if resp.status_code != 200 or not payload.get("access_token"):
            raise AvitoAuthError("Авито не выдал доступ: " + _error_text(resp, payload), resp.status_code, payload)
        return payload

    @staticmethod
    def _store(account: AvitoAccount, payload: dict) -> None:
        account.access_token_enc = encrypt(payload["access_token"])
        if payload.get("refresh_token"):
            account.refresh_token_enc = encrypt(payload["refresh_token"])
        account.token_expires_at = timezone.now() + timedelta(seconds=int(payload.get("expires_in") or 86400))
        if payload.get("scope"):
            account.scope = str(payload["scope"])[:500]

    def exchange_code(self, code: str) -> None:
        payload = self._token_request({"grant_type": "authorization_code", "code": code})
        self.account.mode = AvitoAccount.Mode.OAUTH
        self.account.refresh_token_enc = ""
        self._store(self.account, payload)
        self.account.save()

    def connect_with_client_credentials(self) -> None:
        payload = self._token_request({"grant_type": "client_credentials"})
        self.account.mode = AvitoAccount.Mode.CLIENT
        self.account.refresh_token_enc = ""
        self.account.scope = ""
        self._store(self.account, payload)
        self.account.save()

    def access_token(self) -> str:
        acc = self.account
        token = decrypt(acc.access_token_enc)
        if token and acc.token_expires_at and acc.token_expires_at > timezone.now() + TOKEN_MARGIN:
            return token
        return self._refresh()

    def _refresh(self, rejected: str = "") -> str:
        # Обновление токена — под блокировкой записи (SQLite в режиме IMMEDIATE): токен обновления
        # одноразовый, и если веб-процесс и фоновая синхронизация обновят его одновременно,
        # один из них останется с недействительным токеном.
        with transaction.atomic():
            fresh = AvitoAccount.objects.get(pk=self.account.pk)
            token = decrypt(fresh.access_token_enc)
            valid = fresh.token_expires_at and fresh.token_expires_at > timezone.now() + TOKEN_MARGIN
            if token and valid and token != rejected:  # уже обновил другой процесс
                self.account = fresh
                return token
            if fresh.mode == AvitoAccount.Mode.CLIENT:
                payload = self._token_request({"grant_type": "client_credentials"})
            else:
                refresh = decrypt(fresh.refresh_token_enc)
                if not refresh:
                    raise AvitoAuthError("Подключение к Авито потеряно — нажмите «Подключить Авито» ещё раз")
                payload = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
            self._store(fresh, payload)
            fresh.save(update_fields=["access_token_enc", "refresh_token_enc", "token_expires_at", "scope"])
            self.account = fresh
            return payload["access_token"]

    # ---- запросы

    def call(self, method: str, path: str, *, params=None, json_body=None, _retry=True) -> dict:
        token = self.access_token()
        resp = self.http.request(
            method,
            API + path,
            params=params,
            json=json_body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=settings.SYNC_HTTP_TIMEOUT,
        )
        data = _payload(resp)
        token_problem = resp.status_code == 401 or (
            resp.status_code == 403 and re.search(r"token|scope|авториз", _error_text(resp, data), re.I)
        )
        if token_problem and _retry:
            self._refresh(rejected=token)  # токен отозван или истёк раньше срока — обновим и повторим
            return self.call(method, path, params=params, json_body=json_body, _retry=False)
        if resp.status_code >= 400:
            error_cls = AvitoAuthError if token_problem else AvitoError
            raise error_cls(_error_text(resp, data), resp.status_code, data)
        return data

    def _item_path(self, prefix: str) -> str:
        acc = self.account
        if not (acc.user_id and acc.item_id):
            raise AvitoError("Не указан номер пользователя или объявления Авито")
        return f"{prefix}/accounts/{acc.user_id}/items/{acc.item_id}"

    def me(self) -> dict:
        return self.call("GET", "/core/v1/accounts/self")

    def items(self) -> list[dict]:
        data = self.call("GET", "/core/v1/items", params={"per_page": 100})
        return [i for i in data.get("resources", []) if isinstance(i, dict)]

    def bookings(self, date_start: date, date_end: date) -> list[dict]:
        data = self.call(
            "GET",
            self._item_path("/realty/v1") + "/bookings",
            params={"date_start": date_start.isoformat(), "date_end": date_end.isoformat(), "with_unpaid": "true"},
        )
        return [b for b in data.get("bookings", []) if isinstance(b, dict)]

    def set_calendar(self, intervals: list[dict]) -> dict:
        return self.call(
            "POST", self._item_path("/core/v1") + "/bookings", json_body={"bookings": intervals, "source": SOURCE}
        )

    def set_prices(self, prices: list[dict]) -> dict:
        return self.call("POST", self._item_path("/realty/v1") + "/prices", json_body={"prices": prices})


# ---------------------------------------------------------------- брони с Авито


def avito_channel(account: AvitoAccount) -> Channel:
    """Площадка, с которой записываются брони из API: выбранная или «Авито» (созданная для iCal)."""
    if account.channel_id:
        return account.channel
    candidates = [c for c in Channel.objects.order_by("pk") if c.name.strip().lower() == "авито"]
    channel = next((c for c in candidates if c.import_url), candidates[0] if candidates else None)
    if channel is None:
        channel = Channel.objects.create(name="Авито", color="#00AAFF")
    account.channel = channel
    account.save(update_fields=["channel"])
    return channel


def _guest_from_contact(contact: dict) -> Guest | None:
    phone = normalize_phone(str(contact.get("phone") or ""))
    name = " ".join(str(contact.get("name") or "").split())[:200]
    email = str(contact.get("email") or "").strip()[:254]
    try:
        validate_email(email)
    except ValidationError:
        email = ""
    if phone:
        guest = Guest.objects.filter(phone=phone).first()
        if guest:
            if email and not guest.email:
                guest.email = email
                guest.save()
            return guest
        return Guest.objects.create(name=name or "Гость с Авито", phone=phone, email=email, notes="Добавлен из Авито")
    if email:
        guest = Guest.objects.filter(email__iexact=email).first()
        if guest:
            return guest
    if name or email:
        return Guest.objects.create(
            name=name or "Гость с Авито", email=email, notes="Добавлен из Авито — телефон Авито не передал"
        )
    return None


def _ensure_prepayment(booking: Booking, amount: int, today: date) -> bool:
    ext = f"avito:{booking.avito_booking_id}:prepay"
    payment = booking.payments.filter(external_id=ext).first()
    if payment is None:
        # Предоплата могла быть внесена вручную раньше — не задваиваем, а связываем
        payment = booking.payments.filter(
            external_id="", method=Payment.Method.PLATFORM, kind=Payment.Kind.PAYMENT, amount=amount
        ).first()
        if payment is not None:
            payment.external_id = ext
            payment.save(update_fields=["external_id"])
            return False
        Payment.objects.create(
            booking=booking, amount=amount, date=today, method=Payment.Method.PLATFORM,
            comment="Предоплата через Авито", external_id=ext,
        )
        return True
    if payment.amount != amount:
        payment.amount = amount
        payment.save(update_fields=["amount"])
        return True
    return False


@dataclass
class AvitoEvent:
    kind: str  # created / pending / paid / updated / cancelled / attached
    booking: Booking
    details: list[str] = field(default_factory=list)


@dataclass
class AvitoResult:
    ok: bool = True
    error: str = ""
    auth_lost: bool = False
    events: list[AvitoEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pushed: list[str] = field(default_factory=list)
    pulled: int = 0

    def of(self, *kinds: str) -> list[Booking]:
        return [e.booking for e in self.events if e.kind in kinds]


def _parse_date(value) -> date:
    return date.fromisoformat(str(value)[:10])


def apply_bookings(account: AvitoAccount, items: list[dict], today: date | None = None) -> AvitoResult:
    """Приводит брони календаря в соответствие с бронями Авито из API."""
    today = today or timezone.localdate()
    result = AvitoResult(pulled=len(items))
    channel = avito_channel(account)
    seen: set[int] = set()

    with transaction.atomic():
        known = {
            b.avito_booking_id: b
            for b in Booking.objects.filter(avito_booking_id__isnull=False).select_related("channel", "guest")
        }
        for raw in items:
            try:
                aid = int(raw["avito_booking_id"])
                check_in, check_out = _parse_date(raw["check_in"]), _parse_date(raw["check_out"])
                status = STATUS_MAP[str(raw.get("status", ""))]
            except (KeyError, TypeError, ValueError):
                result.warnings.append(f"Не удалось разобрать бронь Авито: {str(raw)[:200]}")
                continue
            if check_out <= check_in:
                result.warnings.append(f"Бронь Авито №{aid}: неверные даты")
                continue
            seen.add(aid)
            booking = known.get(aid)
            kind = "updated"
            if booking is None:
                if status == Booking.Status.CANCELLED:
                    continue  # отменённую бронь, которой у нас не было, не заводим
                booking = _find_twin(channel, check_in, check_out)
                if booking is not None:
                    kind = "attached"
                else:
                    booking = Booking(kind=Booking.Kind.BOOKING, channel=channel, check_in=check_in, check_out=check_out)
                    kind = "created" if status == Booking.Status.CONFIRMED else "pending"
                booking.avito_booking_id = aid
                known[aid] = booking

            details = _apply_fields(booking, raw, status, check_in, check_out, today)
            if kind in ("created", "pending", "attached"):
                result.events.append(AvitoEvent(kind, booking, details))
            elif "status:cancelled" in details:
                result.events.append(AvitoEvent("cancelled", booking, details))
            elif "status:confirmed" in details:
                result.events.append(AvitoEvent("paid", booking, details))
            elif any(d.startswith(("даты", "status")) for d in details):
                result.events.append(AvitoEvent("updated", booking, details))

        # Неоплаченная заявка пропала из ответа — Авито её снял.
        for aid, booking in known.items():
            if aid in seen or booking.pk is None:
                continue
            if booking.status == Booking.Status.REQUEST and booking.avito_status == "pending" and booking.check_out > today:
                booking.status = Booking.Status.CANCELLED
                booking.avito_status = "canceled"
                booking.cancel_reason = "Заявка на Авито не была оплачена"
                booking.save(update_fields=["status", "avito_status", "cancel_reason", "updated_at"])
                result.events.append(AvitoEvent("cancelled", booking, ["status:cancelled"]))
    return result


def _find_twin(channel: Channel, check_in: date, check_out: date) -> Booking | None:
    """Та же бронь Авито, уже внесённая у нас (вручную, из загрузки или по iCal)."""
    for b in (
        Booking.objects.active()
        .filter(check_in=check_in, check_out=check_out, avito_booking_id__isnull=True, kind=Booking.Kind.BOOKING)
        .select_related("channel", "guest")
        .order_by("pk")
    ):
        if same_platform(b.channel, channel):
            return b
    return None


def _apply_fields(booking: Booking, raw: dict, status: str, check_in: date, check_out: date, today: date) -> list[str]:
    prev = booking.avito_data or {}
    details = []
    if (booking.check_in, booking.check_out) != (check_in, check_out):
        if booking.pk:
            details.append("даты")
        booking.check_in, booking.check_out = check_in, check_out
    if booking.status != status:
        if booking.pk:
            details.append(f"status:{status}")
        booking.status = status
        booking.cancel_reason = "Отменена на Авито" if status == Booking.Status.CANCELLED else ""
    booking.avito_status = str(raw.get("status", ""))

    if booking.guest_id is None and booking.kind == Booking.Kind.BOOKING:
        guest = _guest_from_contact(raw.get("contact") or {})
        if guest is not None:
            booking.guest = guest
            details.append("гость")
    guest_count = raw.get("guest_count")
    if isinstance(guest_count, int) and guest_count > 0 and booking.guests in (None, prev.get("guest_count")):
        if booking.guests != guest_count:
            details.append("число гостей")
        booking.guests = min(guest_count, 32767)
    base_price = raw.get("base_price")
    if isinstance(base_price, int) and base_price >= 0 and booking.total_price in (None, prev.get("base_price")):
        if booking.total_price != base_price:
            details.append("сумма")
        booking.total_price = base_price
    deposit = raw.get("safe_deposit") or {}
    if isinstance(deposit.get("tax"), int) and deposit["tax"] >= 0:
        booking.commission_amount = deposit["tax"]
    booking.avito_data = raw
    booking.save()

    amount = deposit.get("total_amount")
    if status == Booking.Status.CONFIRMED and isinstance(amount, int) and amount > 0:
        if _ensure_prepayment(booking, amount, today):
            details.append("предоплата")
    return details


# ---------------------------------------------------------------- что отправляем на Авито


def calendar_intervals(account: AvitoAccount, today: date | None = None) -> list[dict]:
    """Занятые даты для Авито: подтверждённые брони, кроме броней самого Авито.
    Конец периода — дата выезда (на Авито она остаётся свободной для заезда)."""
    today = today or timezone.localdate()
    channel = account.channel
    spans = []
    for b in Booking.objects.confirmed().filter(check_out__gt=today).select_related("channel").order_by("check_in"):
        if b.avito_booking_id is not None or (channel is not None and same_platform(b.channel, channel)):
            continue
        spans.append([max(b.check_in, today), b.check_out, "manual" if b.kind == Booking.Kind.BLOCK else "booking"])
    merged: list[list] = []
    for start, end, kind in spans:  # пересекающиеся периоды Авито не принимает — склеиваем
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            if kind == "booking":
                merged[-1][2] = "booking"
        else:
            merged.append([start, end, kind])
    return [{"date_start": s.isoformat(), "date_end": e.isoformat(), "type": k} for s, e, k in merged]


def price_ranges(today: date | None = None, days: int = DAYS_AHEAD, skip: set[date] | None = None) -> list[dict]:
    """Цены и минимальный срок на год вперёд, одинаковые ночи подряд — одним периодом (date_to включительно)."""
    today = today or timezone.localdate()
    skip = skip or set()
    calc = PriceCalculator(House.get())
    ranges: list[dict] = []
    current = None
    for i in range(days):
        night = today + timedelta(days=i)
        if night in skip:
            current = None
            continue
        price = calc.night_price(night).price
        min_stay = max(1, min(30, calc.min_stay(night)))
        if current and current["night_price"] == price and current["minimal_duration"] == min_stay:
            current["date_to"] = night
            continue
        current = {"date_from": night, "date_to": night, "night_price": price, "minimal_duration": min_stay}
        ranges.append(current)
    return [{**r, "date_from": r["date_from"].isoformat(), "date_to": r["date_to"].isoformat()} for r in ranges]


def _pending_nights(today: date) -> set[date]:
    """Ночи неоплаченных броней Авито: если обновить на них цены, Авито переводит такие брони в overbook."""
    nights = set()
    for b in Booking.objects.filter(avito_status="pending", check_out__gt=today):
        d = max(b.check_in, today)
        while d < b.check_out:
            nights.add(d)
            d += timedelta(days=1)
    return nights


def _digest(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def push_changes(account: AvitoAccount, client: AvitoClient, force: bool = False) -> list[str]:
    """Отправляет на Авито занятость и цены, если они изменились (или давно не отправлялись)."""
    now = timezone.now()
    today = timezone.localdate()
    done = []
    if account.push_calendar:
        intervals = calendar_intervals(account, today)
        digest = _digest(intervals)
        stale = account.calendar_pushed_at is None or now - account.calendar_pushed_at > CALENDAR_REFRESH
        if force or stale or digest != account.calendar_hash:
            try:
                client.set_calendar(intervals)
                account.calendar_message = f"Отправлено периодов: {len(intervals)}"
            except AvitoError as exc:
                if exc.status != 409:
                    raise
                account.calendar_message = "Часть дат не закрыта — на них уже есть оплаченные брони Авито: " + _conflicts_text(exc.payload)
            account.calendar_hash, account.calendar_pushed_at = digest, now
            account.save(update_fields=["calendar_hash", "calendar_pushed_at", "calendar_message"])
            if account.channel_id:  # площадка получила наш календарь — для распознавания «эха» в iCal
                Channel.objects.filter(pk=account.channel_id).update(last_exported_at=now)
            done.append("занятость: " + account.calendar_message)
    if account.push_prices:
        prices = price_ranges(today, skip=_pending_nights(today))
        digest = _digest(prices)
        stale = account.prices_pushed_at is None or now - account.prices_pushed_at > PRICES_REFRESH
        if prices and (force or stale or digest != account.prices_hash):
            client.set_prices(prices)
            account.prices_hash, account.prices_pushed_at = digest, now
            account.prices_message = f"Отправлено периодов цен: {len(prices)}"
            account.save(update_fields=["prices_hash", "prices_pushed_at", "prices_message"])
            done.append("цены: " + account.prices_message)
    return done


def _conflicts_text(payload) -> str:
    try:
        conflicts = payload["error"]["conflicts"]
        return ", ".join(f"{c['interval']['date_start']}–{c['interval']['date_end']}" for c in conflicts) or "—"
    except (KeyError, TypeError):
        return "подробности не переданы"


# ---------------------------------------------------------------- синхронизация целиком


def _fmt(b: Booking) -> str:
    return f"{b.check_in:%d.%m}–{b.check_out:%d.%m.%Y} ({b.nights} н.)"


def _rub(v) -> str:
    return f"{v:,} ₽".replace(",", " ")


def _describe(b: Booking) -> str:
    parts = [_fmt(b)]
    if b.guest_id:
        parts.append(b.guest.name)
    if b.guests:
        parts.append(f"гостей: {b.guests}")
    if b.total_price is not None:
        parts.append(_rub(b.total_price))
    prepay = (b.avito_data.get("safe_deposit") or {}).get("total_amount") if b.avito_data else None
    if prepay:
        parts.append(f"предоплата {_rub(prepay)}")
    return ", ".join(parts)


def _messages(result: AvitoResult) -> list[str]:
    lines = []
    for e in result.events:
        b = e.booking
        if e.kind == "created":
            lines.append(f"🆕 Авито: новая бронь {_describe(b)}")
        elif e.kind == "pending":
            lines.append(f"📝 Авито: бронь ждёт оплаты гостем — {_describe(b)}")
        elif e.kind == "paid":
            lines.append(f"✅ Авито: гость оплатил бронь — {_describe(b)}")
        elif e.kind == "cancelled":
            lines.append(f"❌ Авито: отменена бронь {_fmt(b)}{', ' + b.guest.name if b.guest_id else ''}")
        elif e.kind == "updated":
            lines.append(f"✏️ Авито: изменилась бронь {_describe(b)}")
        elif e.kind == "attached" and e.details:
            lines.append(f"🔗 Авито: бронь {_fmt(b)} дополнена — {', '.join(d for d in e.details if ':' not in d)}")
    return lines


def sync_avito(http=None, notify=send_telegram, pull: bool = True, force_push: bool = False) -> AvitoResult | None:
    """Загружает брони с Авито и отправляет занятость и цены. None — интеграция не настроена."""
    account = AvitoAccount.get()
    if not (credentials_configured() and account.is_ready):
        return None
    from .ical import _conflict_warnings  # noqa: PLC0415 — общий текст о двойных бронях

    client = AvitoClient(account, http)
    first_sync = account.last_sync_at is None
    was_ok = account.last_sync_ok
    today = timezone.localdate()
    result = AvitoResult()
    try:
        if pull and account.sync_bookings:
            items = client.bookings(today, today + timedelta(days=DAYS_AHEAD))
            result = apply_bookings(client.account, items, today)
        result.pushed = push_changes(client.account, client, force=force_push)
    except AvitoAuthError as exc:
        result.ok, result.error, result.auth_lost = False, str(exc)[:1000], True
    except (AvitoError, requests.RequestException) as exc:
        result.ok, result.error = False, str(exc)[:1000] or exc.__class__.__name__
    except Exception as exc:  # noqa: BLE001 — сбой не должен ронять фоновый цикл
        log.exception("Сбой синхронизации с Авито")
        result.ok, result.error = False, f"{exc.__class__.__name__}: {exc}"[:1000]

    account = client.account
    if not pull and result.ok:
        return result

    created, updated = result.of("created", "pending"), result.of("paid", "updated", "attached")
    cancelled = result.of("cancelled")
    summary = (
        f"API Авито: броней на Авито {result.pulled}; новых {len(created)}, обновлено {len(updated)}, отменено {len(cancelled)}"
        if result.ok
        else f"API Авито — ошибка: {result.error}"
    )
    if result.pushed:
        summary += "; " + "; ".join(result.pushed)
    if result.warnings:
        summary += ". " + " ".join(result.warnings[:3])
    account.last_sync_at = timezone.now()
    account.last_sync_ok = result.ok
    account.last_sync_message = summary
    account.save(update_fields=["last_sync_at", "last_sync_ok", "last_sync_message"])
    if account.channel_id:
        SyncLog.objects.create(
            channel_id=account.channel_id, ok=result.ok, message=summary,
            created=len(created), updated=len(updated), cancelled=len(cancelled),
        )

    lines = []
    if result.ok and first_sync:
        lines.append(
            f"✅ Авито подключено: на Авито {result.pulled} {'бронь' if result.pulled == 1 else 'броней'}, "
            f"новых в календаре {len(created)}, дополнено {len(result.of('attached'))}."
        )
    elif result.ok:
        lines += _messages(result)
    if result.ok:
        lines += _conflict_warnings(result.of("created", "pending", "paid", "updated", "attached"))
    if not result.ok and was_ok is not False:
        if result.auth_lost:
            lines.append("🔑 Авито: подключение потеряно — откройте «Площадки → Авито» и нажмите «Подключить Авито».")
        else:
            lines.append(f"🔌 Не удалось синхронизироваться с Авито: {result.error}")
    elif result.ok and was_ok is False:
        lines.append("✅ Синхронизация с Авито снова работает")
    if lines:
        notify("\n".join(lines))
    return result


def push_avito_changes(http=None) -> list[str]:
    """Быстрая проверка (каждые ~30 с): если занятость или цены изменились — сразу отправить на Авито."""
    account = AvitoAccount.get()
    if not (credentials_configured() and account.is_ready) or account.last_sync_ok is False:
        return []
    try:
        return push_changes(account, AvitoClient(account, http))
    except (AvitoError, requests.RequestException) as exc:
        log.warning("Не удалось отправить изменения на Авито: %s", exc)
        return []

