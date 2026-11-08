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
