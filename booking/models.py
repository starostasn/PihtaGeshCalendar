import secrets
from datetime import date, time

from django.core.exceptions import ValidationError
from django.db import models
from django.core.validators import MinValueValidator
from django.db.models import Q
from django.utils import timezone


def new_token() -> str:
    return secrets.token_urlsafe(24)


class House(models.Model):
    """Настройки дома. В системе всегда ровно одна запись (см. House.get())."""

    name = models.CharField("Название", max_length=200, default="Гостевой дом")
    base_price = models.PositiveIntegerField("Базовая цена за ночь, ₽", default=10000)
    min_stay = models.PositiveSmallIntegerField("Минимальный срок, ночей", default=1)
    check_in_time = models.TimeField("Время заезда", default=time(14, 0))
    check_out_time = models.TimeField("Время выезда", default=time(12, 0))
    max_guests = models.PositiveSmallIntegerField("Максимум гостей", default=10)

    class Meta:
        verbose_name = "Дом"
        verbose_name_plural = "Дом"

    def __str__(self):
        return self.name

    @classmethod
    def get(cls) -> "House":
        house = cls.objects.order_by("pk").first()
        if house is None:
            house = cls.objects.create()
        return house


WEEKDAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


class PriceRule(models.Model):
    """Правило цены: сезон, цена выходного дня, цена на конкретные даты.

    Правило действует на ночи, попадающие в период [date_from, date_to] (включительно)
    и в выбранные дни недели. Если подходит несколько правил, побеждает правило
    с бо́льшим приоритетом, при равенстве — созданное позже.
    """

    PRIORITY_WEEKDAYS = 10
    PRIORITY_SEASON = 20
    PRIORITY_DATES = 30

    name = models.CharField("Название", max_length=200)
    date_from = models.DateField("С даты", null=True, blank=True)
    date_to = models.DateField("По дату (включительно)", null=True, blank=True)
    weekdays = models.CharField(
        "Дни недели",
        max_length=13,
        blank=True,
        help_text="Номера дней через запятую, 0 — понедельник … 6 — воскресенье. Пусто — все дни. "
        "День недели считается по ночи: ночь с пятницы на субботу — это пятница.",
    )
    price = models.PositiveIntegerField("Цена за ночь, ₽", null=True, blank=True)
    min_stay = models.PositiveSmallIntegerField(
        "Мин. срок, ночей", null=True, blank=True, help_text="Применяется по дате заезда."
    )
    priority = models.SmallIntegerField("Приоритет", default=PRIORITY_SEASON)
    is_active = models.BooleanField("Включено", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Правило цены"
        verbose_name_plural = "Правила цен"
        ordering = ["-priority", "-created_at"]

    def __str__(self):
        return self.name

    @property
    def weekday_set(self) -> set[int]:
        return {int(x) for x in self.weekdays.split(",") if x.strip() != ""}

    @property
    def weekdays_display(self) -> str:
        days = sorted(self.weekday_set)
        return ", ".join(WEEKDAY_NAMES[d] for d in days) if days else "все дни"

    def applies_to(self, night: date) -> bool:
        if not self.is_active:
            return False
        if self.date_from and night < self.date_from:
            return False
        if self.date_to and night > self.date_to:
            return False
        days = self.weekday_set
        return not days or night.weekday() in days

    def clean(self):
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ValidationError({"date_to": "Дата окончания раньше даты начала."})
        if self.price is None and self.min_stay is None:
            raise ValidationError("Укажите цену и/или минимальный срок.")
        try:
            days = self.weekday_set
        except ValueError:
            raise ValidationError({"weekdays": "Дни недели — числа от 0 до 6 через запятую."})
        if any(d < 0 or d > 6 for d in days):
            raise ValidationError({"weekdays": "Дни недели — числа от 0 до 6 через запятую."})


class Channel(models.Model):
    """Площадка (Авито, Суточно и т.д.) с iCal-синхронизацией.

    import_url — ссылка на календарь площадки (оттуда забираем занятые даты).
    export_token — наша секретная ссылка, которую вставляют на площадке. В неё попадают
    все занятые даты, КРОМЕ броней, пришедших с этой же площадки (чтобы не было «эха»).
    """

    name = models.CharField("Название", max_length=100)
    color = models.CharField("Цвет", max_length=7, default="#00AAFF", help_text="Цвет броней в календаре")
    import_url = models.URLField("Ссылка на календарь площадки (iCal)", max_length=1000, blank=True)
    export_token = models.CharField(max_length=64, unique=True, default=new_token, editable=False)
    is_active = models.BooleanField("Синхронизировать", default=True)
    commission_percent = models.DecimalField(
        "Комиссия площадки, %",
        max_digits=4,
        decimal_places=1,
        default=0,
        help_text="Для статистики: какой процент от суммы брони удерживает площадка.",
    )

    last_sync_at = models.DateTimeField("Последняя синхронизация", null=True, blank=True)
    last_sync_ok = models.BooleanField(null=True, blank=True)
    last_sync_message = models.TextField(blank=True)
    last_exported_at = models.DateTimeField("Площадка забирала наш календарь", null=True, blank=True)

    class Meta:
        verbose_name = "Площадка"
        verbose_name_plural = "Площадки"
        ordering = ["name"]

    def __str__(self):
        return self.name


class BookingQuerySet(models.QuerySet):
    def active(self):
        """Брони и заявки — всё, что занимает даты в календаре (кроме отменённых)."""
        return self.exclude(status=Booking.Status.CANCELLED)

    def confirmed(self):
        """Только подтверждённые брони: они закрывают даты на площадках и идут в выручку."""
        return self.filter(status=Booking.Status.CONFIRMED)

    def overlapping(self, check_in: date, check_out: date):
        """Брони, у которых есть хотя бы одна общая ночь с периодом [check_in, check_out)."""
        return self.filter(check_in__lt=check_out, check_out__gt=check_in)


class Booking(models.Model):
    class Kind(models.TextChoices):
        BOOKING = "booking", "Гости"
        BLOCK = "block", "Закрыто"

    class Status(models.TextChoices):
        CONFIRMED = "confirmed", "Бронь"
        REQUEST = "request", "Заявка"
        CANCELLED = "cancelled", "Отмена"

    kind = models.CharField("Тип", max_length=10, choices=Kind.choices, default=Kind.BOOKING)
    status = models.CharField(
        "Статус",
        max_length=10,
        choices=Status.choices,
        default=Status.CONFIRMED,
        help_text="Заявка занимает даты в календаре, но не закрывает их на площадках.",
    )
    check_in = models.DateField("Заезд")
    check_out = models.DateField("Выезд")

    channel = models.ForeignKey(
        Channel,
        verbose_name="Источник",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bookings",
        help_text="Пусто — прямая бронь (телефон, мессенджер, постоянный гость).",
    )
    guest_name = models.CharField("Гость", max_length=200, blank=True)
    phone = models.CharField("Телефон", max_length=50, blank=True)
    guests = models.PositiveSmallIntegerField("Гостей", null=True, blank=True)
    total_price = models.PositiveIntegerField("Сумма, ₽", null=True, blank=True)
    comment = models.TextField("Комментарий", blank=True)

    # Для броней, импортированных из iCal площадки
    external_uid = models.CharField(max_length=500, blank=True)
    external_summary = models.CharField("Описание с площадки", max_length=500, blank=True)
    cancel_reason = models.CharField("Причина отмены", max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = BookingQuerySet.as_manager()

    class Meta:
        verbose_name = "Бронь"
        verbose_name_plural = "Брони"
        ordering = ["check_in"]
        constraints = [
            models.CheckConstraint(condition=Q(check_out__gt=models.F("check_in")), name="booking_dates_order"),
            models.UniqueConstraint(
                fields=["channel", "external_uid"],
                condition=~Q(external_uid=""),
                name="booking_unique_external_uid",
            ),
        ]

    def __str__(self):
        who = self.guest_name or self.get_kind_display()
        return f"{who}: {self.check_in:%d.%m}–{self.check_out:%d.%m.%Y}"

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def is_imported(self) -> bool:
        return bool(self.external_uid)

    @property
    def source_name(self) -> str:
        return self.channel.name if self.channel else "Напрямую"

    @property
    def color(self) -> str:
        if self.kind == self.Kind.BLOCK and not self.channel:
            return "#9AA0A6"
        return self.channel.color if self.channel else "#2E7D32"

    @property
    def is_request(self) -> bool:
        return self.status == self.Status.REQUEST

    @property
    def is_cancelled(self) -> bool:
        return self.status == self.Status.CANCELLED

    # ---- оплаты (используйте prefetch_related("payments") в списках)

    @property
    def paid(self) -> int:
        return sum(p.signed_amount for p in self.payments.all())

    @property
    def balance_due(self) -> int | None:
        """Остаток к оплате. Отрицательный — переплата (нужно вернуть гостю)."""
        if self.total_price is None:
            return None
        return self.total_price - self.paid

    @property
    def payment_state(self) -> str:
        balance = self.balance_due
        if balance is None:
            return "unknown"
        if balance < 0:
            return "overpaid"
        if balance == 0:
            return "paid"
        return "partial" if self.paid > 0 else "unpaid"

    @property
    def payment_state_display(self) -> str:
        return {
            "unknown": "Сумма не указана",
            "overpaid": "Переплата",
            "paid": "Оплачено",
            "partial": "Частично оплачено",
            "unpaid": "Не оплачено",
        }[self.payment_state]

    @property
    def paid_percent(self) -> int:
        if not self.total_price:
            return 0
        return max(0, min(100, round(self.paid * 100 / self.total_price)))

    @property
    def commission(self) -> int:
        if not self.total_price or not self.channel:
            return 0
        return round(self.total_price * float(self.channel.commission_percent) / 100)

    @property
    def title(self) -> str:
        if self.kind == self.Kind.BLOCK:
            return self.comment.splitlines()[0][:40] if self.comment else "Закрыто"
        return self.guest_name or self.external_summary or "Бронь"


class SyncLog(models.Model):
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="sync_logs")
    started_at = models.DateTimeField(auto_now_add=True)
    ok = models.BooleanField(default=True)
    message = models.TextField(blank=True)
    created = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    cancelled = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Журнал синхронизации"
        verbose_name_plural = "Журнал синхронизации"
        ordering = ["-started_at"]


class Payment(models.Model):
    class Kind(models.TextChoices):
        PAYMENT = "payment", "Оплата"
        REFUND = "refund", "Возврат"

    class Method(models.TextChoices):
        CASH = "cash", "Наличные"
        TRANSFER = "transfer", "Перевод на карту"
        SBP = "sbp", "СБП"
        CARD = "card", "Оплата картой"
        PLATFORM = "platform", "Через площадку"
        OTHER = "other", "Другое"

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="payments")
    date = models.DateField("Дата", default=timezone.localdate)
    kind = models.CharField("Тип", max_length=10, choices=Kind.choices, default=Kind.PAYMENT)
    amount = models.PositiveIntegerField("Сумма, ₽", validators=[MinValueValidator(1)])
    method = models.CharField("Способ", max_length=10, choices=Method.choices, default=Method.TRANSFER)
    comment = models.CharField("Комментарий", max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Платёж"
        verbose_name_plural = "Платежи"
        ordering = ["date", "pk"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.amount} ₽ ({self.date:%d.%m.%Y})"

    @property
    def signed_amount(self) -> int:
        return -self.amount if self.kind == self.Kind.REFUND else self.amount
