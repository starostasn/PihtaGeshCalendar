from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.auth.views import redirect_to_login
from django.urls import path, reverse

from booking import accounts, avito_views, expenses, guests, house, import_views, merge, views

urlpatterns = [
    path("expenses/", expenses.overview, name="expenses"),
    path("expenses/new/", expenses.edit, name="expense_new"),
    path("expenses/<int:pk>/", expenses.edit, name="expense_edit"),
    path("expenses/import/", expenses.review, name="expense_import"),
    path("", views.calendar_view, name="calendar"),
    path("bookings/", views.booking_list, name="booking_list"),
    path("bookings/new/", views.booking_create, name="booking_create"),
    path("bookings/<int:pk>/", views.booking_edit, name="booking_edit"),
    path("bookings/import/", import_views.import_view, name="import"),
    path("bookings/import/<int:pk>/", import_views.import_preview, name="import_preview"),
    path("bookings/import/<int:pk>/revert/", import_views.import_revert, name="import_revert"),
    path("bookings/<int:pk>/payments/", views.payment_add, name="payment_add"),
    path("bookings/merge/<int:a>/<int:b>/", merge.merge_view, name="booking_merge"),
    path("bookings/duplicates/", merge.duplicates_view, name="duplicates"),
    path("linked-events/<int:pk>/delete/", views.linked_event_delete, name="linked_event_delete"),
    path("payments/<int:pk>/delete/", views.payment_delete, name="payment_delete"),
    path("api/quote/", views.quote_api, name="quote_api"),
    path("api/guests/", guests.guest_search_api, name="guest_search"),
    path("guests/", guests.guests_view, name="guests"),
    path("guests/new/", guests.guest_create, name="guest_create"),
    path("guests/<int:pk>/", guests.guest_edit, name="guest_edit"),
    path("guests/<int:pk>/delete/", guests.guest_delete, name="guest_delete"),
    path("guests/<int:pk>/merge/", guests.guest_merge, name="guest_merge"),
    path("house/", house.house_view, name="house"),
    path("house/edit/", house.house_edit, name="house_edit"),
    path("house/cover.jpg", house.house_cover, name="house_cover"),
    path("manifest.webmanifest", house.manifest, name="manifest"),
    path("house/photos/upload/", house.photo_upload, name="photo_upload"),
    path("house/photos/<int:pk>/", house.photo_update, name="photo_update"),
    path("house/photos/<int:pk>/<str:size>.jpg", house.photo_file, name="photo_file"),
    path("prices/", views.prices_view, name="prices"),
    path("prices/rules/new/", views.rule_edit, name="rule_create"),
    path("prices/rules/<int:pk>/", views.rule_edit, name="rule_edit"),
    path("prices/rules/<int:pk>/delete/", views.rule_delete, name="rule_delete"),
    path("stats/", views.stats_view, name="stats"),
    path("channels/", views.channels_view, name="channels"),
    path("channels/new/", views.channel_edit, name="channel_create"),
    path("channels/<int:pk>/", views.channel_edit, name="channel_edit"),
    path("channels/<int:pk>/sync/", views.channel_sync, name="channel_sync"),
    path("channels/<int:pk>/preview/", views.channel_preview, name="channel_preview"),
    path("channels/<int:pk>/new-token/", views.channel_new_token, name="channel_new_token"),
    path("channels/avito/", avito_views.avito_view, name="avito"),
    path("channels/avito/connect/", avito_views.avito_connect, name="avito_connect"),
    path("channels/avito/connect-keys/", avito_views.avito_connect_client, name="avito_connect_client"),
    path("channels/avito/check/", avito_views.avito_check, name="avito_check"),
    path("channels/avito/sync/", avito_views.avito_sync, name="avito_sync"),
    path("channels/avito/disconnect/", avito_views.avito_disconnect, name="avito_disconnect"),
    path("avito/callback/", avito_views.avito_callback, name="avito_callback"),
    path("ical/<str:token>.ics", views.ical_export, name="ical_export"),
    path("users/", accounts.users_view, name="users"),
    path("users/new/", accounts.user_create, name="user_create"),
    path("users/<int:pk>/", accounts.user_edit, name="user_edit"),
    path("users/invites/", accounts.invite_create, name="invite_create"),
    path("users/invites/<int:pk>/revoke/", accounts.invite_revoke, name="invite_revoke"),
    path("invite/<str:token>/", accounts.invite_accept, name="invite_accept"),
    path("account/", accounts.account_view, name="account"),
    path("login/", accounts.ThrottledLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
]

if settings.ENABLE_DJANGO_ADMIN:
    # Вход в админку идёт через общую страницу входа календаря с защитой от перебора паролей.
    admin.site.login = lambda request, extra_context=None: redirect_to_login(
        request.get_full_path(), reverse("login")
    )
    urlpatterns.append(path("admin/", admin.site.urls))
