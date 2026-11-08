"""Операции с бронями: сохранение с проверкой пересечений и поиск конфликтов."""

from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from .models import Booking


class OverlapError(Exception):
    def __init__(self, conflicts: list[Booking]):
        self.conflicts = conflicts
        super().__init__("Даты пересекаются с другими бронями")


def find_overlaps(check_in: date, check_out: date, exclude_pk: int | None = None) -> list[Booking]:
    qs = Booking.objects.active().overlapping(check_in, check_out).select_related("channel")
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return list(qs)


def save_booking(booking: Booking) -> Booking:
    """Сохраняет бронь, созданную вручную. Пересечение с действующими бронями запрещено.

    SQLite работает в режиме IMMEDIATE-транзакций, поэтому между проверкой и записью
    никто (например, фоновая синхронизация) не успеет вставить пересекающуюся бронь.
    """
    with transaction.atomic():
        if booking.status != Booking.Status.CANCELLED:
            conflicts = find_overlaps(booking.check_in, booking.check_out, exclude_pk=booking.pk)
            if conflicts:
                raise OverlapError(conflicts)
        booking.save()
    return booking


def cancel_booking(booking: Booking, reason: str = "") -> Booking:
    booking.status = Booking.Status.CANCELLED
    booking.cancel_reason = reason
    booking.save(update_fields=["status", "cancel_reason", "updated_at"])
    return booking


def conflict_pairs(horizon_days: int = 730) -> list[tuple[Booking, Booking]]:
    """Все пары действующих броней, которые пересекаются (то есть двойные брони)."""
    today = timezone.localdate()
    bookings = list(
        Booking.objects.active()
        .filter(check_out__gt=today, check_in__lt=today + timedelta(days=horizon_days))
        .select_related("channel")
        .order_by("check_in", "pk")
    )
    pairs = []
    for i, a in enumerate(bookings):
        for b in bookings[i + 1 :]:
            if b.check_in >= a.check_out:
                break
            pairs.append((a, b))
    return pairs


def is_possible_echo(a: Booking, b: Booking) -> bool:
    """Даты совпадают полностью и одна из броней импортирована — похоже, площадка
    вернула нам нашу же бронь (некоторые площадки отдают в своём iCal и чужие закрытые даты)."""
    return a.check_in == b.check_in and a.check_out == b.check_out and (a.is_imported or b.is_imported)
