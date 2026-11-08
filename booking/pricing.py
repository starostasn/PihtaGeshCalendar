"""Расчёт цен. Единственное место, где считается стоимость ночи и минимальный срок.

Им пользуются календарь, форма брони и (в будущем) отправка цен на Авито по API.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from .models import House, PriceRule


def daterange(start: date, end: date):
    """Ночи с start по end-1 включительно."""
    d = start
    while d < end:
        yield d
        d += timedelta(days=1)


@dataclass
class NightPrice:
    night: date
    price: int
    rule: PriceRule | None


@dataclass
class Quote:
    check_in: date
    check_out: date
    nights: list[NightPrice] = field(default_factory=list)
    min_stay: int = 1

    @property
    def total(self) -> int:
        return sum(n.price for n in self.nights)

    @property
    def nights_count(self) -> int:
        return len(self.nights)

    @property
    def min_stay_ok(self) -> bool:
        return self.nights_count >= self.min_stay


class PriceCalculator:
    """Загружает правила один раз и считает цены для любого набора дат."""

    def __init__(self, house: House | None = None, rules: list[PriceRule] | None = None):
        self.house = house or House.get()
        if rules is None:
            rules = list(PriceRule.objects.filter(is_active=True))
        # Порядок важен: первое подходящее правило побеждает.
        self.rules = sorted(rules, key=lambda r: (r.priority, r.created_at or 0, r.pk or 0), reverse=True)

    def _first_rule(self, night: date, attr: str) -> PriceRule | None:
        for rule in self.rules:
            if getattr(rule, attr) is not None and rule.applies_to(night):
                return rule
        return None

    def night_price(self, night: date) -> NightPrice:
        rule = self._first_rule(night, "price")
        return NightPrice(night, rule.price if rule else self.house.base_price, rule)

    def min_stay(self, check_in: date) -> int:
        rule = self._first_rule(check_in, "min_stay")
        return rule.min_stay if rule else self.house.min_stay

    def quote(self, check_in: date, check_out: date) -> Quote:
        return Quote(
            check_in=check_in,
            check_out=check_out,
            nights=[self.night_price(n) for n in daterange(check_in, check_out)],
            min_stay=self.min_stay(check_in),
        )
