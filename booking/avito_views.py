"""Страница «Площадки → Авито»: подключение к API Авито, проверка и настройки."""

import secrets
import time
from datetime import timedelta

import requests
from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .avito import (
    AvitoClient,
    AvitoError,
    authorize_url,
    avito_channel,
    calendar_intervals,
    credentials_configured,
    item_id_from_url,
    price_ranges,
    redirect_uri,
    sync_avito,
)
from .models import AvitoAccount, Booking, Channel, House
from .permissions import admin_required

STATE_TTL = 15 * 60  # сколько ждём возвращения с Авито после нажатия «Подключить»


class AvitoSettingsForm(forms.ModelForm):
    class Meta:
        model = AvitoAccount
        fields = ["item_id", "channel", "sync_bookings", "push_calendar", "push_prices"]
        help_texts = {"item_id": "Число в конце ссылки на объявление"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["channel"].queryset = Channel.objects.all()
        self.fields["channel"].empty_label = "— выбрать —"


def _finish_connect(request, client: AvitoClient) -> None:
    """После получения токена: узнаём, чей аккаунт, и подставляем объявление."""
    account = client.account
    me = client.me()
    account.user_id = int(me.get("id") or 0) or None
    account.user_name = str(me.get("name") or "")[:200]
    if not account.item_id:
        account.item_id = item_id_from_url(House.get().avito_url)
    account.connected_at = timezone.now()
    account.connected_by = request.user
    account.last_sync_ok = None
    account.last_sync_at = None
    account.calendar_hash = account.prices_hash = ""
    account.save()
    avito_channel(account)


@admin_required
def avito_view(request):
    account = AvitoAccount.get()
    if not account.item_id and House.get().avito_url:
        account.item_id = item_id_from_url(House.get().avito_url)
    form = AvitoSettingsForm(request.POST or None, instance=account)
    if request.method == "POST" and form.is_valid():
        before = (account.push_prices, account.push_calendar)
        saved = form.save()
        if (saved.push_prices, saved.push_calendar) != before:
            AvitoAccount.objects.filter(pk=saved.pk).update(prices_hash="", calendar_hash="")
        messages.success(request, "Настройки Авито сохранены")
        return redirect("avito")

    today = timezone.localdate()
    intervals = calendar_intervals(account, today) if account.channel_id else []
    prices = price_ranges(today)
    upcoming = (
        Booking.objects.filter(avito_booking_id__isnull=False, check_out__gte=today)
        .select_related("guest", "channel")
        .prefetch_related("payments")
        .order_by("check_in")[:30]
    )
    return render(
        request,
        "booking/avito.html",
        {
            "account": account,
            "form": form,
            "configured": credentials_configured(),
            "redirect_uri": redirect_uri(),
            "intervals": intervals,
            "prices": prices[:12],
            "prices_total": len(prices),
            "upcoming": upcoming,
        },
    )


@admin_required
def avito_connect(request):
    if not credentials_configured():
        messages.error(request, "Сначала впишите AVITO_CLIENT_ID и AVITO_CLIENT_SECRET в .env на сервере и перезапустите календарь")
        return redirect("avito")
    state = secrets.token_urlsafe(24)
    request.session["avito_oauth_state"] = state
    request.session["avito_oauth_started"] = int(time.time())
    return redirect(authorize_url(state))


@admin_required
def avito_callback(request):
    """Сюда Авито возвращает после «Разрешить» (Redirect URI приложения)."""
    expected = request.session.pop("avito_oauth_state", "")
    started = request.session.pop("avito_oauth_started", 0)
    if request.GET.get("error"):
        messages.error(request, f"Авито не дал доступ: {request.GET.get('error_description') or request.GET['error']}")
        return redirect("avito")
    code = request.GET.get("code", "")
    state = request.GET.get("state")
    fresh = expected and time.time() - started < STATE_TTL
    # state защищает от подделки запроса; если Авито его не вернул, принимаем код только
    # сразу после нажатия «Подключить» этим же администратором.
    if not code or not fresh or (state is not None and not secrets.compare_digest(state, expected)):
        messages.error(request, "Ссылка подключения устарела или неверна — нажмите «Подключить Авито» ещё раз")
        return redirect("avito")
    client = AvitoClient(AvitoAccount.get())
    try:
        client.exchange_code(code)
        _finish_connect(request, client)
    except (AvitoError, requests.RequestException) as exc:
        messages.error(request, f"Не удалось подключить Авито: {exc}")
        return redirect("avito")
    messages.success(request, f"Авито подключено: {client.account.user_name or client.account.user_id}. Нажмите «Проверить», затем «Синхронизировать».")
    return redirect("avito")


@admin_required
@require_POST
def avito_connect_client(request):
    """Запасной способ: ключи приложения дают доступ к собственному аккаунту без входа (client_credentials)."""
    client = AvitoClient(AvitoAccount.get())
    try:
        client.connect_with_client_credentials()
        _finish_connect(request, client)
    except (AvitoError, requests.RequestException) as exc:
        messages.error(request, f"По ключам приложения подключиться не удалось: {exc}. Используйте «Подключить Авито».")
        return redirect("avito")
    messages.success(request, f"Подключено по ключам приложения: {client.account.user_name or client.account.user_id}")
    return redirect("avito")


@admin_required
@require_POST
def avito_check(request):
    account = AvitoAccount.get()
    if not account.is_connected:
        messages.error(request, "Авито ещё не подключено")
        return redirect("avito")
    client = AvitoClient(account)
    today = timezone.localdate()
    try:
        me = client.me()
        messages.success(request, f"Доступ есть: аккаунт «{me.get('name', '')}» (№{me.get('id')})")
        try:
            items = client.items()
            if items:
                listing = "; ".join(f"№{i.get('id')} — {i.get('title', '')}" for i in items[:5])
                messages.info(request, f"Объявления аккаунта: {listing}")
        except AvitoError:
            pass  # список объявлений — только для подсказки
        if client.account.item_id:
            bookings = client.bookings(today, today + timedelta(days=365))
            messages.success(request, f"Объявление №{client.account.item_id}: на Авито броней на год вперёд — {len(bookings)}")
        else:
            messages.warning(request, "Укажите номер объявления в настройках ниже")
    except (AvitoError, requests.RequestException) as exc:
        messages.error(request, f"Проверка не прошла: {exc}")
    return redirect("avito")


@admin_required
@require_POST
def avito_sync(request):
    result = sync_avito(force_push=True)
    if result is None:
        messages.error(request, "Сначала подключите Авито и укажите номер объявления")
    elif result.ok:
        messages.success(request, AvitoAccount.get().last_sync_message)
        for w in result.warnings:
            messages.warning(request, w)
    else:
        messages.error(request, f"Авито: {result.error}")
    return redirect("avito")


@admin_required
@require_POST
def avito_disconnect(request):
    account = AvitoAccount.get()
    account.access_token_enc = account.refresh_token_enc = ""
    account.token_expires_at = None
    account.last_sync_ok = None
    account.save()
    messages.info(request, "Авито отключено. Брони в календаре остались; iCal продолжает работать.")
    return redirect("avito")
