from django import forms

from django.utils import timezone

from .models import WEEKDAY_NAMES, Booking, Channel, Guest, House, Payment, PriceRule
from .phones import normalize_phone


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class BookingForm(forms.ModelForm):
    # Гость выбирается из справочника (скрытое поле guest) или создаётся по имени и телефону
    new_guest_name = forms.CharField(label="Имя гостя", max_length=200, required=False)
    new_guest_phone = forms.CharField(
        label="Телефон гостя", max_length=40, required=False, widget=forms.TextInput(attrs={"type": "tel"})
    )
    # Только при создании брони: сразу записать первый платёж (предоплату)
    initial_payment = forms.IntegerField(label="Предоплата, ₽", min_value=0, required=False)
    initial_payment_method = forms.ChoiceField(
        label="Способ оплаты", choices=Payment.Method.choices, initial=Payment.Method.TRANSFER, required=False
    )

    class Meta:
        model = Booking
        fields = [
            "kind",
            "status",
            "check_in",
            "check_out",
            "guest",
            "guests",
            "channel",
            "total_price",
            "comment",
        ]
        widgets = {
            "check_in": DateInput(),
            "check_out": DateInput(),
            "comment": forms.Textarea(attrs={"rows": 3}),
            "kind": forms.RadioSelect(),
            "status": forms.RadioSelect(),
            "guest": forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.new_guest = None  # будет создан при сохранении брони
        self.matched_guest = None  # найден в справочнике по телефону
        self.fields["guest"].queryset = Guest.objects.all()
        self.fields["channel"].empty_label = "Напрямую"
        self.fields["channel"].queryset = Channel.objects.all()
        if self.instance.pk:
            del self.fields["initial_payment"]
            del self.fields["initial_payment_method"]
            if self.instance.is_synced:
                # Даты, статус и источник брони с площадки (iCal или API Авито) задаёт площадка.
                for name in ("check_in", "check_out", "channel", "kind", "status"):
                    self.fields[name].disabled = True

    def clean(self):
        data = super().clean()
        check_in, check_out = data.get("check_in"), data.get("check_out")
        if check_in and check_out and check_out <= check_in:
            self.add_error("check_out", "Дата выезда должна быть позже даты заезда.")
        self._clean_guest(data)
        if data.get("kind") == Booking.Kind.BLOCK and data.get("status") == Booking.Status.REQUEST:
            self.add_error("status", "Закрытые даты не могут быть заявкой — выберите «Бронь» или «Отмена».")
        return data

    def _clean_guest(self, data):
        if data.get("kind") == Booking.Kind.BLOCK:
            data["guest"] = None
            return
        if data.get("guest"):
            return
        name = " ".join((data.get("new_guest_name") or "").split())
        raw_phone = (data.get("new_guest_phone") or "").strip()
        if not name and not raw_phone:
            return  # бронь без гостя (например, быстро закрыть даты с Авито)
        phone = normalize_phone(raw_phone)
        if raw_phone and not phone:
            self.add_error("new_guest_phone", "Не похоже на номер телефона.")
            return
        if not phone:
            self.add_error("new_guest_phone", "Укажите телефон гостя.")
            return
        existing = Guest.objects.filter(phone=phone).first()
        if existing:
            data["guest"] = self.matched_guest = existing
            return
        if not name:
            self.add_error("new_guest_name", "Укажите имя гостя.")
            return
        self.new_guest = Guest(name=name, phone=phone)

    def save_initial_payment(self, booking: Booking, user=None) -> Payment | None:
        amount = self.cleaned_data.get("initial_payment")
        if not amount or booking.kind != Booking.Kind.BOOKING:
            return None
        return Payment.objects.create(
            booking=booking,
            amount=amount,
            method=self.cleaned_data.get("initial_payment_method") or Payment.Method.TRANSFER,
            comment="Предоплата",
            created_by=user,
        )


class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ["date", "kind", "amount", "method", "comment"]
        widgets = {"date": DateInput(), "amount": forms.NumberInput(attrs={"min": 1})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.initial.get("date"):
            self.initial["date"] = timezone.localdate()


class HouseForm(forms.ModelForm):
    class Meta:
        model = House
        fields = ["name", "base_price", "min_stay", "check_in_time", "check_out_time", "max_guests"]
        widgets = {
            "check_in_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "check_out_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        }


WEEKDAY_CHOICES = [(str(i), name) for i, name in enumerate(WEEKDAY_NAMES)]


class PriceRuleForm(forms.ModelForm):
    weekdays = forms.MultipleChoiceField(
        label="Дни недели (ночь)",
        choices=WEEKDAY_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Ничего не выбрано — все дни. Ночь с пятницы на субботу — это «Пт».",
    )

    class Meta:
        model = PriceRule
        fields = ["name", "date_from", "date_to", "weekdays", "price", "min_stay", "priority", "is_active"]
        widgets = {"date_from": DateInput(), "date_to": DateInput()}
        help_texts = {
            "priority": "Если на дату подходит несколько правил, действует правило с бо́льшим приоритетом. "
            "Обычно: 10 — дни недели, 20 — сезоны, 30 — отдельные даты.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["weekdays"] = sorted(str(d) for d in self.instance.weekday_set)

    def clean_weekdays(self):
        return ",".join(sorted(self.cleaned_data["weekdays"]))


class PeriodPriceForm(forms.Form):
    """Быстрое изменение цены и/или мин. срока на период — создаёт правило с высоким приоритетом."""

    date_from = forms.DateField(label="С", widget=DateInput())
    date_to = forms.DateField(label="По (включительно)", widget=DateInput())
    price = forms.IntegerField(label="Цена за ночь, ₽", min_value=0, required=False)
    min_stay = forms.IntegerField(label="Мин. срок, ночей", min_value=1, required=False)

    def clean(self):
        data = super().clean()
        if data.get("date_from") and data.get("date_to") and data["date_to"] < data["date_from"]:
            self.add_error("date_to", "Дата окончания раньше даты начала.")
        if data.get("price") is None and data.get("min_stay") is None:
            raise forms.ValidationError("Укажите цену и/или минимальный срок.")
        return data

    def create_rule(self) -> PriceRule:
        d = self.cleaned_data
        return PriceRule.objects.create(
            name=f"Даты {d['date_from']:%d.%m}–{d['date_to']:%d.%m.%Y}",
            date_from=d["date_from"],
            date_to=d["date_to"],
            price=d.get("price"),
            min_stay=d.get("min_stay"),
            priority=PriceRule.PRIORITY_DATES,
        )


class ChannelForm(forms.ModelForm):
    class Meta:
        model = Channel
        fields = ["name", "color", "import_url", "commission_percent", "is_active"]
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}


# ---------------------------------------------------------------- пользователи

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.auth.forms import UserCreationForm  # noqa: E402

from .models import Invite  # noqa: E402
from .permissions import ROLE_CHOICES, ROLE_MANAGER, role_of, set_role  # noqa: E402

User = get_user_model()


class RoleField(forms.ChoiceField):
    def __init__(self, **kwargs):
        super().__init__(label="Роль", choices=ROLE_CHOICES, widget=forms.RadioSelect, initial=ROLE_MANAGER, **kwargs)


class UserCreateForm(UserCreationForm):
    """Администратор создаёт пользователя сам и задаёт ему пароль."""

    first_name = forms.CharField(label="Имя", max_length=150, required=False, help_text="Как показывать в календаре")
    role = RoleField()

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name")
        labels = {"username": "Логин"}
        help_texts = {"username": "Латинские буквы, цифры и символы @ . + - _"}

    def save(self, commit=True):
        user = super().save(commit=False)
        set_role(user, self.cleaned_data["role"])
        if commit:
            user.save()
        return user


class UserEditForm(forms.ModelForm):
    role = RoleField()

    class Meta:
        model = User
        fields = ("first_name", "is_active")
        labels = {"first_name": "Имя", "is_active": "Может входить в календарь"}
        help_texts = {"is_active": "Снимите галочку, чтобы заблокировать доступ (история броней сохранится)."}

    def __init__(self, *args, editing_self: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["role"] = role_of(self.instance)
        if editing_self:
            # Нельзя лишить прав или заблокировать самого себя — так в календаре всегда остаётся администратор.
            self.fields["role"].disabled = True
            self.fields["is_active"].disabled = True
            self.fields["role"].help_text = "Свою роль может изменить только другой администратор."

    def save(self, commit=True):
        user = super().save(commit=False)
        set_role(user, self.cleaned_data["role"])
        if commit:
            user.save()
        return user


class InviteForm(forms.ModelForm):
    class Meta:
        model = Invite
        fields = ("name", "role")
        widgets = {"role": forms.RadioSelect}


class InviteAcceptForm(UserCreationForm):
    first_name = forms.CharField(label="Ваше имя", max_length=150, help_text="Так вас будут видеть другие в календаре")

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("first_name", "username")
        labels = {"username": "Придумайте логин"}
        help_texts = {"username": "Латинские буквы, цифры и символы @ . + - _"}


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("first_name",)
        labels = {"first_name": "Имя"}



# ---------------------------------------------------------------- гости


class GuestForm(forms.ModelForm):
    phone = forms.CharField(label="Телефон", max_length=40, required=False, widget=forms.TextInput(attrs={"type": "tel"}))

    class Meta:
        model = Guest
        fields = ("name", "phone", "city", "address", "email", "notes")
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Телефон обязателен у новых гостей; у старых без телефона (например, из импорта) можно оставить пустым
        self.require_phone = self.instance.pk is None or bool(self.instance.phone)
        if self.instance.pk and self.instance.phone:
            self.initial["phone"] = self.instance.phone_display

    def clean_phone(self):
        raw = self.cleaned_data.get("phone", "").strip()
        phone = normalize_phone(raw)
        if raw and not phone:
            raise forms.ValidationError("Не похоже на номер телефона. Пример: +7 912 345-67-89")
        if not phone and self.require_phone:
            raise forms.ValidationError("Укажите телефон гостя.")
        if phone:
            other = Guest.objects.filter(phone=phone).exclude(pk=self.instance.pk).first()
            if other:
                self.duplicate_of = other
                raise forms.ValidationError(f"Этот номер уже есть у гостя «{other.name}».")
        return phone
