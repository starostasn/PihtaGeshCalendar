from django.contrib import admin

from .models import Booking, Channel, House, PriceRule, SyncLog


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("check_in", "check_out", "kind", "status", "channel", "guest_name", "total_price", "prepaid")
    list_filter = ("status", "kind", "channel")
    search_fields = ("guest_name", "phone", "comment", "external_summary")
    date_hierarchy = "check_in"


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
