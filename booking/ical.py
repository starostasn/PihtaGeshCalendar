"""Синхронизация с площадками по iCal: экспорт наших занятых дат и импорт календарей площадок."""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import icalendar
import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Booking, Channel, SyncLog
from .notify import send_telegram
from .services import conflict_pairs, is_possible_echo

log = logging.getLogger(__name__)

MAX_FEED_BYTES = 5 * 1024 * 1024
EXPORT_PAST_DAYS = 30


# ---------------------------------------------------------------- экспорт


def build_export(channel: Channel | None = None) -> bytes:
    """iCal со всеми занятыми датами. Брони, импортированные с самой площадки `channel`,
    в её календарь не попадают: иначе отмена брони на площадке «застревала» бы у нас и
    возвращалась туда же закрытыми датами."""
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Guesthouse Calendar//RU")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")

    today = timezone.localdate()
    qs = Booking.objects.active().filter(check_out__gte=today - timedelta(days=EXPORT_PAST_DAYS))
    if channel is not None:
        qs = qs.exclude(channel=channel, external_uid__gt="")

    host = settings.DOMAIN or "localhost"
    stamp = timezone.now()
    for booking in qs.order_by("check_in"):
        event = icalendar.Event()
        event.add("uid", f"booking-{booking.pk}@{host}")
        event.add("dtstamp", stamp)
        event.add("dtstart", booking.check_in)  # VALUE=DATE
        event.add("dtend", booking.check_out)  # дата выезда, не включительно — так и нужно по RFC 5545
        event.add("summary", "Занято")  # без персональных данных гостя
        cal.add_component(event)
    return cal.to_ical()


# ---------------------------------------------------------------- импорт


@dataclass(frozen=True)
class FeedEvent:
    uid: str
    start: date
    end: date
    summary: str = ""


def _to_local_date(value) -> date:
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.date()
    return value


def parse_feed(data: bytes) -> list[FeedEvent]:
    cal = icalendar.Calendar.from_ical(data)
    events, seen_uids = [], {}
    for comp in cal.walk("VEVENT"):
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if "DTSTART" not in comp:
            continue
        start_raw = comp.decoded("DTSTART")
        if "DTEND" in comp:
            end_raw = comp.decoded("DTEND")
        elif "DURATION" in comp:
            end_raw = start_raw + comp.decoded("DURATION")
        else:
            end_raw = None
        start = _to_local_date(start_raw)
        end = _to_local_date(end_raw) if end_raw is not None else start + timedelta(days=1)
        if end <= start:
            end = start + timedelta(days=1)
        summary = str(comp.get("SUMMARY", "")).strip()
        uid = str(comp.get("UID", "")).strip()
        if not uid:  # без UID отслеживаем событие по датам
            uid = "nouid-" + hashlib.sha1(f"{start}|{end}|{summary}".encode()).hexdigest()[:16]
        if uid in seen_uids:  # битые календари иногда повторяют UID
            seen_uids[uid] += 1
            uid = f"{uid}#{seen_uids[uid]}"
        else:
            seen_uids[uid] = 0
        events.append(FeedEvent(uid=uid, start=start, end=end, summary=summary))
    return events


def fetch_feed(url: str) -> bytes:
    resp = requests.get(
        url,
        timeout=settings.SYNC_HTTP_TIMEOUT,
        headers={"User-Agent": "GuesthouseCalendar/1.0 (+ical sync)"},
        stream=True,
    )
    resp.raise_for_status()
    chunks, size = [], 0
    for chunk in resp.iter_content(64 * 1024):
        size += len(chunk)
        if size > MAX_FEED_BYTES:
            raise ValueError("Календарь площадки слишком большой (больше 5 МБ)")
        chunks.append(chunk)
    data = b"".join(chunks)
    if b"BEGIN:VCALENDAR" not in data[:2000]:
        raise ValueError("По ссылке не iCal-календарь (возможно, ссылка устарела или требует входа)")
    return data


@dataclass
class SyncResult:
    ok: bool = True
    created: list[Booking] = field(default_factory=list)
    updated: list[Booking] = field(default_factory=list)
    cancelled: list[Booking] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def touched(self) -> list[Booking]:
        return self.created + self.updated


def _fmt(b: Booking) -> str:
    return f"{b.check_in:%d.%m}–{b.check_out:%d.%m.%Y} ({b.nights} н.)"


def apply_feed(channel: Channel, events: list[FeedEvent]) -> SyncResult:
    """Приводит импортированные брони площадки в соответствие с её календарём."""
    result = SyncResult()
    today = timezone.localdate()
    relevant = [e for e in events if e.end > today]  # прошедшие брони не трогаем
    feed_uids = {e.uid for e in relevant}

    with transaction.atomic():
        imported = {b.external_uid: b for b in Booking.objects.filter(channel=channel).exclude(external_uid="")}
        current = [
            b for b in imported.values() if b.status == Booking.Status.ACTIVE and b.check_out > today
        ]

        if not relevant and len(current) >= 2:
            # Площадка отдала пустой календарь, хотя у нас несколько её будущих броней.
            # Скорее всего, это сбой площадки — массово отменять брони опасно.
            result.warnings.append(
                f"Календарь «{channel.name}» пришёл пустым, хотя у нас {len(current)} будущих брони "
                "с этой площадки. Отмены не применены — проверьте календарь на площадке."
            )
            return result

        # Брони, чей UID пропал из календаря, — кандидаты на «смену UID» (сопоставляем по датам).
        orphans_by_dates = {
            (b.check_in, b.check_out): b for b in current if b.external_uid not in feed_uids
        }

        for ev in relevant:
            booking = imported.get(ev.uid)
            if booking is None:
                booking = orphans_by_dates.pop((ev.start, ev.end), None)
                if booking is not None:
                    del imported[booking.external_uid]
                    booking.external_uid = ev.uid
                    imported[ev.uid] = booking
                    booking.save(update_fields=["external_uid", "updated_at"])

            if booking is None:
                booking = Booking.objects.create(
                    kind=Booking.Kind.BOOKING,
                    channel=channel,
                    check_in=ev.start,
                    check_out=ev.end,
                    external_uid=ev.uid,
                    external_summary=ev.summary[:500],
                )
                imported[ev.uid] = booking
                result.created.append(booking)
                continue

            changed = (
                booking.check_in != ev.start
                or booking.check_out != ev.end
                or booking.status != Booking.Status.ACTIVE
            )
            if changed:
                booking.check_in, booking.check_out = ev.start, ev.end
                booking.status = Booking.Status.ACTIVE
                booking.cancel_reason = ""
                booking.external_summary = ev.summary[:500]
                booking.save()
                result.updated.append(booking)

        for booking in current:
            if booking.external_uid not in feed_uids:
                booking.status = Booking.Status.CANCELLED
                booking.cancel_reason = f"Пропала из календаря «{channel.name}»"
                booking.save(update_fields=["status", "cancel_reason", "updated_at"])
                result.cancelled.append(booking)

    return result


def _conflict_warnings(touched: list[Booking]) -> list[str]:
    touched_ids = {b.pk for b in touched}
    lines = []
    for a, b in conflict_pairs():
        if a.pk in touched_ids or b.pk in touched_ids:
            line = f"⚠️ ДВОЙНАЯ БРОНЬ: {a.source_name} {_fmt(a)} и {b.source_name} {_fmt(b)}"
            if is_possible_echo(a, b):
                line += " — даты совпадают полностью, возможно, это площадка вернула вашу же бронь"
            lines.append(line)
    return lines


def sync_channel(channel: Channel, fetch=fetch_feed, notify=send_telegram) -> SyncResult:
    """Забирает календарь площадки, применяет изменения, пишет журнал и шлёт уведомления."""
    was_ok = channel.last_sync_ok
    try:
        events = parse_feed(fetch(channel.import_url))
        result = apply_feed(channel, events)
    except Exception as exc:  # noqa: BLE001 — любой сбой площадки фиксируем, а не падаем
        log.warning("Синхронизация %s не удалась: %s", channel, exc, exc_info=settings.DEBUG)
        result = SyncResult(ok=False, error=str(exc)[:1000] or exc.__class__.__name__)

    messages = []
    for b in result.created:
        messages.append(f"🆕 Новая бронь с «{channel.name}»: {_fmt(b)}")
    for b in result.updated:
        messages.append(f"✏️ Изменилась бронь с «{channel.name}»: {_fmt(b)}")
    for b in result.cancelled:
        messages.append(f"❌ Отменена бронь с «{channel.name}»: {_fmt(b)}")
    messages += result.warnings
    if result.ok:
        messages += _conflict_warnings(result.touched)

    summary = (
        f"Новых: {len(result.created)}, изменено: {len(result.updated)}, отменено: {len(result.cancelled)}"
        if result.ok
        else f"Ошибка: {result.error}"
    )
    if result.warnings:
        summary += ". " + " ".join(result.warnings)

    SyncLog.objects.create(
        channel=channel,
        ok=result.ok,
        message=summary,
        created=len(result.created),
        updated=len(result.updated),
        cancelled=len(result.cancelled),
    )
    channel.last_sync_at = timezone.now()
    channel.last_sync_ok = result.ok
    channel.last_sync_message = summary
    channel.save(update_fields=["last_sync_at", "last_sync_ok", "last_sync_message"])

    if not result.ok and was_ok is not False:
        messages.append(f"🔌 Не удалось синхронизировать «{channel.name}»: {result.error}")
    elif result.ok and was_ok is False:
        messages.append(f"✅ Синхронизация с «{channel.name}» снова работает")

    if messages:
        notify("\n".join(messages))
    return result


def sync_all(**kwargs) -> dict[str, SyncResult]:
    results = {}
    for channel in Channel.objects.filter(is_active=True).exclude(import_url=""):
        results[channel.name] = sync_channel(channel, **kwargs)
    return results
