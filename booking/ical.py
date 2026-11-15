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

from .models import Booking, Channel, LinkedEvent, SyncLog
from .notify import send_telegram
from .merge import same_platform
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
    # Заявки на площадки не отдаём: даты закрываются только подтверждёнными бронями.
    qs = Booking.objects.confirmed().filter(check_out__gte=today - timedelta(days=EXPORT_PAST_DAYS))
    if channel is not None:
        qs = qs.exclude(channel=channel, external_uid__gt="").exclude(channel=channel, avito_booking_id__isnull=False)

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
    attached: list[Booking] = field(default_factory=list)  # бронь уже была внесена вручную — привязали
    echoed: list[Booking] = field(default_factory=list)  # площадка показала нашу же бронь — запомнили
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def touched(self) -> list[Booking]:
        return self.created + self.updated + self.attached


def _fmt(b: Booking) -> str:
    return f"{b.check_in:%d.%m}–{b.check_out:%d.%m.%Y} ({b.nights} н.)"


def _label(b: Booking) -> str:
    return f"{b.source_name}{' (заявка)' if b.is_request else ''} {_fmt(b)}"


def _twin_action(channel: Channel, twin: Booking) -> str:
    """Что делать с новой бронью площадки, если у нас уже есть бронь ровно на эти даты.

    attach — это та же бронь площадки, внесённая вручную (источник — эта площадка): привязываем.
    echo — площадка показывает нашу же бронь, которую получила из нашего календаря:
           бронь подтверждена и создана до того, как площадка последний раз забрала наш календарь.
    """
    if same_platform(twin.channel, channel):
        return "attach"
    if (
        twin.status == Booking.Status.CONFIRMED
        and channel.last_exported_at is not None
        and twin.created_at < channel.last_exported_at
    ):
        return "echo"
    return ""


def apply_feed(channel: Channel, events: list[FeedEvent]) -> SyncResult:
    """Приводит импортированные брони площадки в соответствие с её календарём."""
    result = SyncResult()
    today = timezone.localdate()
    relevant = [e for e in events if e.end > today]  # прошедшие брони не трогаем
    feed_uids = {e.uid for e in relevant}

    with transaction.atomic():
        imported = {b.external_uid: b for b in Booking.objects.filter(channel=channel).exclude(external_uid="")}
        current = [
            b for b in imported.values() if b.status == Booking.Status.CONFIRMED and b.check_out > today
        ]

        future_links = LinkedEvent.objects.filter(channel=channel, check_out__gt=today).count()
        if not relevant and len(current) + future_links >= 2:
            # Площадка отдала пустой календарь, хотя у нас несколько её будущих броней.
            # Скорее всего, это сбой площадки — массово отменять брони опасно.
            result.warnings.append(
                f"Календарь «{channel.name}» пришёл пустым, хотя у нас {len(current) + future_links} будущих брони "
                "с этой площадки. Отмены не применены — проверьте календарь на площадке."
            )
            return result

        # События, которые мы уже связали со своими бронями (дубли после объединения), — пропускаем.
        links = {link.uid: link for link in LinkedEvent.objects.filter(channel=channel)}
        orphan_links = {(l.check_in, l.check_out): l for l in links.values() if l.uid not in feed_uids}

        # Брони, чей UID пропал из календаря, — кандидаты на «смену UID» (сопоставляем по датам).
        orphans_by_dates = {
            (b.check_in, b.check_out): b for b in current if b.external_uid not in feed_uids
        }

        # Наши брони (не с iCal) по датам — чтобы не создавать дубль, если бронь уже внесена.
        twins: dict[tuple, list[Booking]] = {}
        for b in (
            Booking.objects.active()
            .filter(external_uid="", check_out__gt=today)
            .select_related("channel")
            .order_by("pk")
        ):
            twins.setdefault((b.check_in, b.check_out), []).append(b)

        for ev in relevant:
            link = links.get(ev.uid)
            if link is None and ev.uid not in imported:
                link = orphan_links.pop((ev.start, ev.end), None)
                if link is not None:  # площадка сменила UID связанного события
                    del links[link.uid]
                    link.uid = ev.uid
                    links[ev.uid] = link
                    link.save(update_fields=["uid"])
            if link is not None:
                if (link.check_in, link.check_out) != (ev.start, ev.end):
                    link.check_in, link.check_out = ev.start, ev.end
                    link.save(update_fields=["check_in", "check_out"])
                continue

            booking = imported.get(ev.uid)
            if booking is None:
                booking = orphans_by_dates.pop((ev.start, ev.end), None)
                if booking is not None:
                    del imported[booking.external_uid]
                    booking.external_uid = ev.uid
                    imported[ev.uid] = booking
                    booking.save(update_fields=["external_uid", "updated_at"])

            if booking is None:
                same_dates = twins.get((ev.start, ev.end), [])
                action = _twin_action(channel, same_dates[0]) if len(same_dates) == 1 else ""
                if action == "attach":
                    booking = same_dates.pop()
                    booking.channel = channel
                    booking.external_uid = ev.uid
                    booking.external_summary = ev.summary[:500]
                    booking.status = Booking.Status.CONFIRMED
                    booking.save()
                    imported[ev.uid] = booking
                    result.attached.append(booking)
                    continue
                if action == "echo":
                    twin = same_dates.pop()
                    links[ev.uid] = LinkedEvent.objects.create(
                        channel=channel, uid=ev.uid, booking=twin, check_in=ev.start, check_out=ev.end,
                        summary=ev.summary[:500],
                    )
                    result.echoed.append(twin)
                    continue

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

            # Статус брони, которую ведёт API Авито (ждёт оплаты или отменена), задаёт API, а не iCal.
            keep_status = booking.avito_status in ("pending", "canceled")
            changed = (
                booking.check_in != ev.start
                or booking.check_out != ev.end
                or (not keep_status and booking.status != Booking.Status.CONFIRMED)
            )
            if changed:
                booking.check_in, booking.check_out = ev.start, ev.end
                if not keep_status:
                    booking.status = Booking.Status.CONFIRMED
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

        # Связанное событие пропало с площадки — связь больше не нужна.
        LinkedEvent.objects.filter(channel=channel).exclude(uid__in=list(feed_uids)).delete()

    return result


def _conflict_warnings(touched: list[Booking]) -> list[str]:
    touched_ids = {b.pk for b in touched}
    lines = []
    for a, b in conflict_pairs():
        if a.pk in touched_ids or b.pk in touched_ids:
            line = f"⚠️ ДВОЙНАЯ БРОНЬ: {_label(a)} и {_label(b)}"
            if is_possible_echo(a, b):
                line += (
                    " — даты совпадают полностью: возможно, это дубль (площадка вернула вашу же бронь)."
                    " Откройте календарь и нажмите «Объединить»"
                )
            lines.append(line)
    return lines


def preview_channel(channel: Channel, fetch=None) -> dict:
    """Что сделает синхронизация с площадкой — без изменений в базе (всё откатывается)."""
    events = parse_feed((fetch or fetch_feed)(channel.import_url))
    today = timezone.localdate()

    def describe(b: Booking) -> str:
        who = b.guest.name if b.guest_id else (b.comment.splitlines()[0][:40] if b.comment else "")
        return _fmt(b) + (f" — {who}" if who else "")

    with transaction.atomic():
        result = apply_feed(channel, events)
        report = {
            "events": len(events),
            "future": sum(1 for e in events if e.end > today),
            "created": [describe(b) for b in result.created],
            "attached": [describe(b) for b in result.attached],
            "echoed": [describe(b) for b in result.echoed],
            "updated": [describe(b) for b in result.updated],
            "cancelled": [describe(b) for b in result.cancelled],
            "warnings": list(result.warnings),
            "conflicts": [line.replace("⚠️ ", "") for line in _conflict_warnings(result.touched)],
        }
        transaction.set_rollback(True)
    return report


def sync_channel(channel: Channel, fetch=None, notify=send_telegram) -> SyncResult:
    """Забирает календарь площадки, применяет изменения, пишет журнал и шлёт уведомления."""
    was_ok = channel.last_sync_ok
    try:
        events = parse_feed((fetch or fetch_feed)(channel.import_url))
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
    for b in result.attached:
        messages.append(f"🔗 Бронь с «{channel.name}» {_fmt(b)} уже была внесена — привязана к ней, дубля нет")
    messages += result.warnings
    if result.ok:
        messages += _conflict_warnings(result.touched)

    summary = (
        f"Новых: {len(result.created)}, изменено: {len(result.updated)}, отменено: {len(result.cancelled)}"
        if result.ok
        else f"Ошибка: {result.error}"
    )
    linked = len(result.attached) + len(result.echoed)
    if linked:
        summary += f", связано с уже внесёнными: {linked}"
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
