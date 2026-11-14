"""Справочник гостей: список с поиском, карточка с историей броней, объединение дублей, поиск для формы брони."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Max, Min, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import GuestForm
from .models import Booking, Guest
from .permissions import admin_required
from .phones import phone_search_variants

SORTS = {
    "name": ("Имя", ["search_text", "pk"]),
    "last": ("Последний заезд", ["-last_stay", "search_text"]),
    "count": ("Броней", ["-bookings_count", "search_text"]),
    "total": ("Сумма", ["-total", "search_text"]),
}


def search_guests(qs, q: str):
    q = (q or "").strip()
    if not q:
        return qs
    words = Guest.search_key(q).split()
    cond = Q()
    for word in words:  # каждое слово запроса должно встретиться в имени, городе или e-mail
        cond &= Q(search_text__contains=word)
    for digits in phone_search_variants(q):
        cond |= Q(phone__contains=digits)
    return qs.filter(cond)


def with_stats(qs):
    today = timezone.localdate()
    confirmed = Q(bookings__status=Booking.Status.CONFIRMED, bookings__kind=Booking.Kind.BOOKING)
    return qs.annotate(
        bookings_count=Count("bookings", filter=confirmed, distinct=True),
        total=Sum("bookings__total_price", filter=confirmed),
        last_stay=Max("bookings__check_in", filter=confirmed & Q(bookings__check_in__lte=today)),
        next_stay=Min("bookings__check_in", filter=confirmed & Q(bookings__check_in__gt=today)),
    )


@login_required
def guests_view(request):
    q = request.GET.get("q", "")
    sort = request.GET.get("sort", "name")
    if sort not in SORTS:
        sort = "name"
    qs = with_stats(search_guests(Guest.objects.all(), q)).order_by(*SORTS[sort][1])
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "booking/guests.html",
        {
            "page": page,
            "q": q,
            "sort": sort,
            "sorts": [(k, v[0]) for k, v in SORTS.items()],
            "total_count": Guest.objects.count(),
            "no_phone_count": Guest.objects.filter(phone="").count(),
        },
    )


@login_required
def guest_create(request):
    form = GuestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        guest = form.save(commit=False)
        guest.created_by = request.user
        guest.save()
        messages.success(request, f"Гость «{guest.name}» добавлен")
        return redirect("guest_edit", pk=guest.pk)
    return render(request, "booking/guest_form.html", {"form": form, "guest": None})


@login_required
def guest_edit(request, pk: int):
    guest = get_object_or_404(Guest, pk=pk)
    form = GuestForm(request.POST or None, instance=guest)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Карточка гостя сохранена")
        return redirect("guest_edit", pk=guest.pk)

    bookings = list(guest.bookings.select_related("channel").prefetch_related("payments").order_by("-check_in"))
    confirmed = [b for b in bookings if b.status == Booking.Status.CONFIRMED and b.kind == Booking.Kind.BOOKING]
    today = timezone.localdate()
    summary = {
        "count": len(confirmed),
        "nights": sum(b.nights for b in confirmed),
        "total": sum(b.total_price or 0 for b in confirmed),
        "paid": sum(b.paid for b in confirmed),
        "debt": sum(max(b.balance_due or 0, 0) for b in confirmed),
        "upcoming": [b for b in confirmed if b.check_out > today],
    }
    return render(
        request,
        "booking/guest_form.html",
        {
            "form": form,
            "guest": guest,
            "bookings": bookings,
            "summary": summary,
            "duplicate_of": getattr(form, "duplicate_of", None),
        },
    )


@admin_required
@require_POST
def guest_delete(request, pk: int):
    guest = get_object_or_404(Guest, pk=pk)
    if guest.bookings.exists():
        messages.error(request, "У гостя есть брони — удалить нельзя. Можно объединить его с другим гостем.")
        return redirect("guest_edit", pk=pk)
    name = guest.name
    guest.delete()
    messages.success(request, f"Гость «{name}» удалён")
    return redirect("guests")


@admin_required
@require_POST
def guest_merge(request, pk: int):
    """Объединить другого гостя (other) в этого: брони переезжают, пустые поля дополняются, другой удаляется."""
    target = get_object_or_404(Guest, pk=pk)
    other_id = request.POST.get("other")
    if not other_id or not other_id.isdigit() or int(other_id) == target.pk:
        messages.error(request, "Выберите другого гостя для объединения")
        return redirect("guest_edit", pk=pk)
    other = get_object_or_404(Guest, pk=int(other_id))
    with transaction.atomic():
        moved = other.bookings.update(guest=target)
        phone = other.phone
        for field in ("city", "address", "email"):
            if not getattr(target, field) and getattr(other, field):
                setattr(target, field, getattr(other, field))
        if other.notes:
            target.notes = (target.notes + "\n" if target.notes else "") + other.notes
        other.delete()  # сначала удаляем — телефон должен быть уникальным
        if not target.phone and phone:
            target.phone = phone
        target.save()
    messages.success(request, f"Гость «{other.name}» объединён с «{target.name}», перенесено броней: {moved}")
    return redirect("guest_edit", pk=pk)


@login_required
def guest_search_api(request):
    """Поиск гостя для формы брони и объединения: /api/guests/?q=ан"""
    q = request.GET.get("q", "")
    if len(q.strip()) < 2:
        return JsonResponse({"results": []})
    exclude = request.GET.get("exclude")
    qs = with_stats(search_guests(Guest.objects.all(), q))
    if exclude and exclude.isdigit():
        qs = qs.exclude(pk=int(exclude))
    results = [
        {
            "id": g.pk,
            "name": g.name,
            "phone": g.phone_display,
            "city": g.city,
            "bookings": g.bookings_count,
            "url": reverse("guest_edit", args=[g.pk]),
        }
        for g in qs.order_by("-last_stay", "search_text")[:10]
    ]
    return JsonResponse({"results": results})


def guest_choice_payload(guest: Guest | None) -> dict | None:
    if guest is None:
        return None
    return {"id": guest.pk, "name": guest.name, "phone": guest.phone_display, "url": reverse("guest_edit", args=[guest.pk])}

