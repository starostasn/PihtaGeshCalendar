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
