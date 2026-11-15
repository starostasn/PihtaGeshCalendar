import secrets
from datetime import date, time, timedelta

from django.conf import settings
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

    # ---- «О доме»: справочник для команды. Поля «по строке на пункт» показываются списками.
    tagline = models.CharField("Коротко о доме", max_length=200, blank=True)
    description = models.TextField("Описание", blank=True)
    address = models.CharField("Адрес", max_length=300, blank=True)
    area_m2 = models.PositiveSmallIntegerField("Площадь дома, м²", null=True, blank=True)
    plot_sotok = models.PositiveSmallIntegerField("Участок, соток", null=True, blank=True)
    floors = models.CharField("Этажность", max_length=100, blank=True)
    rooms = models.PositiveSmallIntegerField("Комнат", null=True, blank=True)
    layout = models.TextField("Планировка и спальные места", blank=True, help_text="По строке на пункт")
    amenities = models.TextField("Удобства", blank=True, help_text="По строке на пункт")
    location_info = models.TextField("Расположение", blank=True, help_text="По строке на пункт")
    rules = models.TextField("Правила проживания", blank=True, help_text="По строке на пункт")
    deposit = models.PositiveIntegerField("Залог, ₽", null=True, blank=True)
    prepayment_percent = models.PositiveSmallIntegerField("Предоплата, %", null=True, blank=True)
    cancellation_policy = models.TextField("Оплата и отмена", blank=True)
    extra_services = models.TextField("Дополнительные услуги", blank=True, help_text="По строке на пункт")
    avito_url = models.URLField("Объявление на Авито", max_length=500, blank=True)
    # Для команды: видно всем сотрудникам — менеджерам это нужно отправлять гостям
    checkin_instructions = models.TextField("Как заселить гостя", blank=True, help_text="Ключи, котёл, баня, выезд")
    wifi_name = models.CharField("Wi-Fi: сеть", max_length=100, blank=True)
    wifi_password = models.CharField("Wi-Fi: пароль", max_length=100, blank=True)
    lockbox_code = models.CharField("Код сейфа с ключами", max_length=50, blank=True)
    team_contacts = models.TextField("Контакты команды", blank=True, help_text="Уборка, сантехник, электрик…")
    admin_notes = models.TextField("Заметки только для администратора", blank=True)

    class Meta:
        verbose_name = "Дом"
        verbose_name_plural = "Дом"

    def __str__(self):
        return self.name

    @staticmethod
    def lines(text: str) -> list[str]:
        return [line.strip(" •-–\t") for line in (text or "").splitlines() if line.strip(" •-–\t")]

    @property
    def map_url(self) -> str:
        from urllib.parse import quote

        return f"https://yandex.ru/maps/?text={quote(self.address)}" if self.address else ""

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


class ImportBatch(models.Model):
    """Загрузка старых броней из файла: черновик -> применена -> (отменена)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        APPLIED = "applied", "Загружено"
        REVERTED = "reverted", "Отменено"

    filename = models.CharField("Файл", max_length=255)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    rows = models.JSONField("Строки файла", default=list)
    options = models.JSONField("Настройки", default=dict)
    result = models.JSONField("Итог", default=dict)
    enriched = models.JSONField("Дополненные брони", default=list)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    reverted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Импорт броней"
        verbose_name_plural = "Импорт броней"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.filename} ({self.created_at:%d.%m.%Y %H:%M})"


class Guest(models.Model):
    """Справочник гостей: один гость — много броней."""

    name = models.CharField("Имя", max_length=200)
    phone = models.CharField(
        "Телефон", max_length=20, blank=True, help_text="Хранится в виде +79123456789; один номер — один гость"
    )
    city = models.CharField("Откуда", max_length=120, blank=True, help_text="Город или регион")
    address = models.CharField("Адрес", max_length=300, blank=True)
    email = models.EmailField("E-mail", blank=True)
    notes = models.TextField("Заметки", blank=True)
    import_batch = models.ForeignKey(
        ImportBatch, null=True, blank=True, editable=False, on_delete=models.SET_NULL, related_name="guests"
    )
    # Имя, город и e-mail в нижнем регистре для поиска: LIKE в SQLite не различает регистр только у латиницы
    search_text = models.CharField(max_length=700, blank=True, editable=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, editable=False, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Гость"
        verbose_name_plural = "Гости"
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["phone"], condition=~Q(phone=""), name="guest_unique_phone"),
        ]

    def __str__(self):
        return f"{self.name} ({self.phone_display})" if self.phone else self.name

    @staticmethod
    def search_key(text: str) -> str:
        return " ".join((text or "").lower().replace("ё", "е").split())

    def save(self, *args, **kwargs):
        self.name = " ".join(self.name.split())
        self.search_text = self.search_key(f"{self.name} {self.city} {self.email}")
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"search_text"}
        super().save(*args, **kwargs)

    @property
    def phone_display(self) -> str:
        from .phones import format_phone

        return format_phone(self.phone)

    @property
    def has_phone(self) -> bool:
        return bool(self.phone)


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
    guest = models.ForeignKey(
        Guest, verbose_name="Гость", null=True, blank=True, on_delete=models.PROTECT, related_name="bookings"
    )
    guests = models.PositiveSmallIntegerField("Гостей", null=True, blank=True)
    total_price = models.PositiveIntegerField("Сумма, ₽", null=True, blank=True)
    comment = models.TextField("Комментарий", blank=True)

    # Для броней, импортированных из iCal площадки
    external_uid = models.CharField(max_length=500, blank=True)
    external_summary = models.CharField("Описание с площадки", max_length=500, blank=True)
    cancel_reason = models.CharField("Причина отмены", max_length=300, blank=True)
    import_batch = models.ForeignKey(
        ImportBatch, null=True, blank=True, editable=False, on_delete=models.SET_NULL, related_name="bookings"
    )
    # Бронь из API Авито: номер, статус и последние данные (гость, суммы) как их отдал Авито
    avito_booking_id = models.BigIntegerField("Номер брони на Авито", null=True, blank=True, unique=True, editable=False)
    avito_status = models.CharField(max_length=20, blank=True, editable=False)
    avito_data = models.JSONField(default=dict, blank=True, editable=False)
    commission_amount = models.PositiveIntegerField(
        "Комиссия площадки, ₽", null=True, blank=True, editable=False,
        help_text="Фактическая комиссия (приходит из API Авито). Если пусто — считается по проценту площадки.",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Создал",
        null=True,
        blank=True,
        editable=False,
        on_delete=models.SET_NULL,
        related_name="+",
    )
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
        who = self.guest.name if self.guest_id else self.get_kind_display()
        return f"{who}: {self.check_in:%d.%m}–{self.check_out:%d.%m.%Y}"

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def is_imported(self) -> bool:
        return bool(self.external_uid)

    @property
    def is_from_avito_api(self) -> bool:
        return self.avito_booking_id is not None

    @property
    def is_synced(self) -> bool:
        """Даты и статус задаёт площадка (iCal или API Авито)."""
        return self.is_imported or self.is_from_avito_api

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
        if self.commission_amount is not None:
            return self.commission_amount
        if not self.total_price or not self.channel:
            return 0
        return round(self.total_price * float(self.channel.commission_percent) / 100)

    @property
    def title(self) -> str:
        if self.kind == self.Kind.BLOCK:
            return self.comment.splitlines()[0][:40] if self.comment else "Закрыто"
        if self.guest_id:
            return self.guest.name
        return self.external_summary or "Бронь"


class LinkedEvent(models.Model):
    """Событие из календаря площадки, которое — та же бронь, что уже есть у нас.

    Так бывает, когда площадка показывает в своём календаре наши же закрытые даты
    (прямую бронь или бронь с другой площадки). Синхронизация такие события пропускает,
    поэтому дубль не появляется снова. Связь исчезает сама, когда событие пропадает с площадки.
    """

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="linked_events")
    uid = models.CharField(max_length=500)
    booking = models.ForeignKey("Booking", on_delete=models.CASCADE, related_name="linked_events")
    check_in = models.DateField()
    check_out = models.DateField()
    summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Связанное событие площадки"
        verbose_name_plural = "Связанные события площадок"
        constraints = [models.UniqueConstraint(fields=["channel", "uid"], name="linked_event_unique_uid")]

    def __str__(self):
        return f"{self.channel}: {self.check_in:%d.%m}–{self.check_out:%d.%m.%Y} → бронь {self.booking_id}"


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
    import_batch = models.ForeignKey(
        ImportBatch, null=True, blank=True, editable=False, on_delete=models.SET_NULL, related_name="payments"
    )
    # Платёж, пришедший с площадки (например, «avito:123:prepay») — чтобы не задвоить при повторной загрузке
    external_id = models.CharField(max_length=100, blank=True, db_index=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Внёс",
        null=True,
        blank=True,
        editable=False,
        on_delete=models.SET_NULL,
        related_name="+",
    )
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


def invite_expiry():
    return timezone.now() + timedelta(days=7)


class Invite(models.Model):
    """Одноразовая ссылка-приглашение: по ней человек сам задаёт логин и пароль."""

    ROLE_CHOICES = [("admin", "Администратор"), ("manager", "Менеджер")]

    token = models.CharField(max_length=64, unique=True, default=new_token, editable=False)
    name = models.CharField("Для кого", max_length=150, blank=True, help_text="Например: Ирина, менеджер")
    role = models.CharField("Роль", max_length=10, choices=ROLE_CHOICES, default="manager")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField("Действует до", default=invite_expiry)
    used_at = models.DateTimeField(null=True, blank=True)
    used_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Приглашение"
        verbose_name_plural = "Приглашения"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name or f"Приглашение от {self.created_at:%d.%m.%Y}"

    @property
    def status(self) -> str:
        if self.used_at:
            return "used"
        if self.revoked_at:
            return "revoked"
        if self.expires_at <= timezone.now():
            return "expired"
        return "active"

    @property
    def status_display(self) -> str:
        return {"used": "Использовано", "revoked": "Отозвано", "expired": "Истекло", "active": "Активно"}[self.status]

    @property
    def is_valid(self) -> bool:
        return self.status == "active"


class HousePhoto(models.Model):
    """Фото дома. Хранится уменьшенная копия без метаданных (геометки удаляются) и превью."""

    image = models.CharField(max_length=200, editable=False)  # путь в DATA_DIR/media
    thumb = models.CharField(max_length=200, editable=False)
    width = models.PositiveIntegerField(default=0, editable=False)
    height = models.PositiveIntegerField(default=0, editable=False)
    caption = models.CharField("Подпись", max_length=200, blank=True)
    position = models.PositiveIntegerField(default=0)
    is_cover = models.BooleanField("Главное фото", default=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Фото дома"
        verbose_name_plural = "Фото дома"
        ordering = ["position", "pk"]

    def __str__(self):
        return self.caption or f"Фото {self.pk}"


class AvitoAccount(models.Model):
    """Подключение к API Авито (одна запись). Ключи приложения — в .env сервера, здесь только токены доступа."""

    class Mode(models.TextChoices):
        OAUTH = "oauth", "Вход через Авито"
        CLIENT = "client", "Ключи приложения"

    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.OAUTH)
    channel = models.ForeignKey(
        Channel, verbose_name="Площадка в календаре", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+", help_text="Брони из Авито записываются с этим источником",
    )
    user_id = models.BigIntegerField("Номер пользователя Авито", null=True, blank=True)
    user_name = models.CharField(max_length=200, blank=True)
    item_id = models.BigIntegerField("Номер объявления", null=True, blank=True)
    access_token_enc = models.TextField(blank=True)
    refresh_token_enc = models.TextField(blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    scope = models.CharField(max_length=500, blank=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    connected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    sync_bookings = models.BooleanField("Загружать брони с Авито", default=True)
    push_calendar = models.BooleanField(
        "Закрывать на Авито даты других броней", default=True,
        help_text="Прямые брони и брони с других площадок сразу закрывают даты на Авито",
    )
    push_prices = models.BooleanField(
        "Отправлять цены и минимальный срок на Авито", default=False,
        help_text="Цены из раздела «Цены» на год вперёд. Включайте, когда цены здесь настроены.",
    )

    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_ok = models.BooleanField(null=True, blank=True)
    last_sync_message = models.TextField(blank=True)
    calendar_hash = models.CharField(max_length=64, blank=True)
    calendar_pushed_at = models.DateTimeField(null=True, blank=True)
    calendar_message = models.TextField(blank=True)
    prices_hash = models.CharField(max_length=64, blank=True)
    prices_pushed_at = models.DateTimeField(null=True, blank=True)
    prices_message = models.TextField(blank=True)

    class Meta:
        verbose_name = "Подключение к Авито"
        verbose_name_plural = "Подключение к Авито"

    def __str__(self):
        return f"Авито: {self.user_name or self.user_id or 'не подключено'}"

    @classmethod
    def get(cls) -> "AvitoAccount":
        account = cls.objects.order_by("pk").first()
        if account is None:
            account = cls.objects.create()
        return account

    @property
    def is_connected(self) -> bool:
        return bool(self.access_token_enc or self.refresh_token_enc)

    @property
    def is_ready(self) -> bool:
        return self.is_connected and bool(self.user_id and self.item_id)
