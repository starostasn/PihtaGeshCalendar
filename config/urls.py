from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.auth.views import redirect_to_login
from django.urls import path, reverse

from booking import accounts, views

urlpatterns = [
    path("", views.calendar_view, name="calendar"),
    path("bookings/", views.booking_list, name="booking_list"),
    path("bookings/new/", views.booking_create, name="booking_create"),
    path("bookings/<int:pk>/", views.booking_edit, name="booking_edit"),
    path("bookings/<int:pk>/payments/", views.payment_add, name="payment_add"),
    path("payments/<int:pk>/delete/", views.payment_delete, name="payment_delete"),
    path("api/quote/", views.quote_api, name="quote_api"),
    path("prices/", views.prices_view, name="prices"),
    path("prices/rules/new/", views.rule_edit, name="rule_create"),
    path("prices/rules/<int:pk>/", views.rule_edit, name="rule_edit"),
    path("prices/rules/<int:pk>/delete/", views.rule_delete, name="rule_delete"),
    path("stats/", views.stats_view, name="stats"),
    path("channels/", views.channels_view, name="channels"),
    path("channels/new/", views.channel_edit, name="channel_create"),
    path("channels/<int:pk>/", views.channel_edit, name="channel_edit"),
    path("channels/<int:pk>/sync/", views.channel_sync, name="channel_sync"),
    path("channels/<int:pk>/new-token/", views.channel_new_token, name="channel_new_token"),
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
