from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from booking import views

urlpatterns = [
    path("", views.calendar_view, name="calendar"),
    path("bookings/", views.booking_list, name="booking_list"),
    path("bookings/new/", views.booking_create, name="booking_create"),
    path("bookings/<int:pk>/", views.booking_edit, name="booking_edit"),
    path("bookings/<int:pk>/cancel/", views.booking_cancel, name="booking_cancel"),
    path("bookings/<int:pk>/restore/", views.booking_restore, name="booking_restore"),
    path("api/quote/", views.quote_api, name="quote_api"),
    path("prices/", views.prices_view, name="prices"),
    path("prices/rules/new/", views.rule_edit, name="rule_create"),
    path("prices/rules/<int:pk>/", views.rule_edit, name="rule_edit"),
    path("prices/rules/<int:pk>/delete/", views.rule_delete, name="rule_delete"),
    path("channels/", views.channels_view, name="channels"),
    path("channels/new/", views.channel_edit, name="channel_create"),
    path("channels/<int:pk>/", views.channel_edit, name="channel_edit"),
    path("channels/<int:pk>/sync/", views.channel_sync, name="channel_sync"),
    path("channels/<int:pk>/new-token/", views.channel_new_token, name="channel_new_token"),
    path("ical/<str:token>.ics", views.ical_export, name="ical_export"),
    path("login/", auth_views.LoginView.as_view(template_name="booking/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("admin/", admin.site.urls),
]
