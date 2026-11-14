from django.contrib import admin

from .models import Booking, Channel, Guest, House, Payment, PriceRule, SyncLog


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("check_in", "check_out", "kind", "status", "channel", "guest", "total_price")
    inlines = [PaymentInline]
    list_filter = ("status", "kind", "channel")
    search_fields = ("guest__name", "guest__phone", "comment", "external_summary")
    raw_id_fields = ("guest",)
    date_hierarchy = "check_in"


@admin.register(Guest)
class GuestAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "city", "email", "created_at")
    search_fields = ("name", "phone", "email")


@admin.register(PriceRule)
class PriceRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "date_from", "date_to", "weekdays", "price", "min_stay", "priority", "is_active")


@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "last_sync_at", "last_sync_ok", "last_exported_at")
    readonly_fields = ("export_token",)


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = ("started_at", "channel", "ok", "created", "updated", "cancelled", "message")
    list_filter = ("channel", "ok")


admin.site.register(House)
