from django import template

register = template.Library()


@register.filter
def rub(value):
    """10000 -> «10 000» (с неразрывным пробелом)."""
    if value is None or value == "":
        return "—"
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return value


@register.filter
def short_price(value):
    """Цена в ячейке календаря: 12500 -> «12,5к», 9000 -> «9к»."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return ""
    if v >= 1000:
        k = v / 1000
        return (f"{k:.1f}".rstrip("0").rstrip(".").replace(".", ",")) + "к"
    return str(v)


@register.filter
def abs_int(value):
    try:
        return abs(int(value))
    except (TypeError, ValueError):
        return value


@register.filter
def ru_plural(value, forms):
    """3|ru_plural:"бронь,брони,броней" -> «брони»."""
    try:
        n = abs(int(value))
    except (TypeError, ValueError):
        return ""
    one, few, many = forms.split(",")
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


AMENITY_ICONS = [  # сначала более точные слова: «тв» внутри «Средства» или «Твистер» не должно совпадать
    ("алиса", "🔊"), ("колонк", "🔊"), ("игр", "🎲"), ("гигиен", "🧴"), ("сушилк", "🥾"), ("лыж", "⛷"),
    ("сноуборд", "🏂"), ("парков", "🅿️"), ("бан", "🧖"), ("мангал", "🔥"), ("террас", "🌲"), ("веранд", "🌲"),
    ("wi-fi", "📶"), ("wifi", "📶"), ("телевиз", "📺"), ("холодил", "🧊"), ("варочн", "🍳"), ("плит", "🍳"),
    ("духов", "🍳"), ("микровол", "🍳"), ("чайник", "☕"), ("посуд", "🍽"), ("стираль", "🧺"), ("водонагр", "🚿"),
    ("душ", "🚿"), ("фен", "💨"), ("утюг", "👔"), ("постель", "🛏"), ("полотен", "🧻"),
]


@register.filter
def amenity_icon(text):
    low = (text or "").lower()
    for key, icon in AMENITY_ICONS:
        if key in low:
            return icon
    return "✓"


@register.filter
def pair_with(first, second):
    """Для перебора двух объектов в шаблоне: {% for x in a|pair_with:b %}."""
    return [first, second]


@register.filter
def iso_date(value):
    """'2026-12-02' → '02.12.2026'."""
    from datetime import date

    try:
        return date.fromisoformat(str(value)).strftime("%d.%m.%Y")
    except ValueError:
        return value
