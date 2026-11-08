from django import forms

from .models import WEEKDAY_NAMES, Booking, Channel, House, PriceRule


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class BookingForm(forms.ModelForm):
    class Meta:
        model = Booking
        fields = [
            "kind",
            "check_in",
            "check_out",
            "guest_name",
            "phone",
            "guests",
            "channel",
            "total_price",
            "prepaid",
            "comment",
        ]
        widgets = {
            "check_in": DateInput(),
            "check_out": DateInput(),
            "comment": forms.Textarea(attrs={"rows": 3}),
            "kind": forms.RadioSelect(),
            "phone": forms.TextInput(attrs={"type": "tel"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["channel"].empty_label = "Напрямую"
        self.fields["channel"].queryset = Channel.objects.all()
        if self.instance.pk and self.instance.is_imported:
            # Даты и источник импортированной брони задаёт площадка.
            for name in ("check_in", "check_out", "channel", "kind"):
                self.fields[name].disabled = True

    def clean(self):
        data = super().clean()
        check_in, check_out = data.get("check_in"), data.get("check_out")
        if check_in and check_out and check_out <= check_in:
            self.add_error("check_out", "Дата выезда должна быть позже даты заезда.")
        prepaid, total = data.get("prepaid") or 0, data.get("total_price")
        if total is not None and prepaid > total:
            self.add_error("prepaid", "Предоплата больше суммы брони.")
        return data


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
        fields = ["name", "color", "import_url", "is_active"]
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}
