import re
from collections import Counter, defaultdict
from decimal import Decimal
from django.db.models import Q

from .models import Expense, PayerContribution
from .stats import MONTHS_SHORT


def payer_amounts(entry):
    funding = entry.funding.strip()
    aliases = {"Рубцов Лёха": "Рубцов Алексей", "Рубцов Саня": "Рубцов Александр"}
    shares = []
    positive = Decimal(0)
    for part in funding.split(", "):
        match = re.fullmatch(r"(.+): (-?\d+(?:\.\d+)?)", part)
        if not match:
            label = funding if funding and ":" not in funding else "Без разбивки"
            return [(label, entry.amount)]
        name, value = match.groups()
        payer = "Копилка" if name.lower().startswith("с копилки ") else aliases.get(name, name)
        value = Decimal(value)
        shares.append((payer, value))
        positive += max(value, 0)
    if positive != entry.amount:
        return [(k, v) for k, v in shares if v < 0] + [("Без разбивки", entry.amount)]
    return shares


def ranked(values, total):
    return [{"label": name, "amount": value, "percent": round(100 * value / total, 1) if total else 0}
            for name, value in values.most_common()]


def compare_expenses(rows, year, baseline, month):
    months = [month] if month else list(range(1, 13))
    series = {y: Counter() for y in (year, baseline)}
    groups = {y: Counter() for y in (year, baseline)}
    relevant = Q(period__year__in=series) | Q(period__isnull=True, year_only__in=series)
    for period, fallback, kind, amount in rows.filter(relevant).values_list("period", "year_only", "kind", "amount"):
        y = period.year if period else fallback
        series[y][period.month if period else 0] += amount
        groups[y][kind] += amount
    if not month and any(0 in values for values in series.values()):
        months.append(0)
    current = sum(series[year].values())
    previous = sum(series[baseline].values())
    return dict(year=year, baseline=baseline, current=current, previous=previous,
                delta=current-previous, percent=(current-previous)*100/previous if previous else None,
                labels=[MONTHS_SHORT[m] if m else "Без месяца" for m in months], values=[series[year][m] for m in months],
                base_values=[series[baseline][m] for m in months],
                groups=[dict(label=label, current=groups[year][k], previous=groups[baseline][k],
                             delta=groups[year][k]-groups[baseline][k]) for k,label in Expense.Kind.choices])


def dashboard(rows, year, month=None, category=""):
    groups, categories = Counter(), Counter()
    payments, refunds, deposits = Counter(), Counter(), Counter()
    contributions = PayerContribution.objects.all()
    if year:
        contributions = contributions.filter(paid__year=year)
    if month:
        contributions = contributions.filter(paid__month=month)
    if not category:
        for contribution in contributions:
            deposits[contribution.payer] += contribution.amount
    timeline = defaultdict(Counter)
    if year:
        for month in range(1, 13):
            timeline[month] = Counter()
    count = 0
    for entry in rows:
        count += 1
        groups[entry.get_kind_display()] += entry.amount
        categories[entry.category or "Без категории"] += entry.amount
        for payer, value in payer_amounts(entry):
            if value < 0:
                refunds[payer] -= value
            else:
                payments[payer] += value
        key = (entry.period.month if year else entry.period.year) if entry.period else 0
        if not entry.period and not year:
            key = entry.year_only or 0
        timeline[key][entry.kind] += entry.amount
    total = sum(groups.values())
    peak = max((sum(series.values()) for series in timeline.values()), default=0)
    trend = []
    for key, series in sorted(timeline.items(), key=lambda item: (not item[0], item[0])):
        trend.append(dict(label=(MONTHS_SHORT[key] if year else key) if key else "Без месяца",
                          total=sum(series.values()),
                          parts=[dict(kind=k, label=label, amount=series[k],
                                      width=round(100 * series[k] / peak, 2) if peak else 0)
                                 for k, label in Expense.Kind.choices]))
    payers = [dict(label=name, paid=payments[name], refunds=refunds[name], deposits=deposits[name],
                   amount=payments[name] + deposits[name] - refunds[name])
              for name in sorted(payments.keys() | refunds.keys() | deposits.keys())]
    payers.sort(key=lambda row: row["amount"], reverse=True)
    return dict(total=total, count=count, payers=payers, groups=ranked(groups, total),
                categories=ranked(categories, total), trend=trend)
