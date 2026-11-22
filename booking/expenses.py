from collections import defaultdict
from datetime import date
from decimal import Decimal
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from openpyxl.utils.exceptions import InvalidFileException

from .expense_import import has_refund, money, parse_workbook
from .expense_stats import compare_expenses, dashboard
from .models import Booking, Expense
from .permissions import admin_required
from .stats import MONTHS_SHORT, _add_months, _core, _load


class ExpenseForm(forms.ModelForm):
    payers = ("Староста", "Рубцов Алексей", "Рубцов Александр", "Кравченко", "Копилка")
    period = forms.DateField(label="Месяц расхода", required=False, input_formats=["%Y-%m"],
                             widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}))

    class Meta:
        model = Expense
        fields = ["kind", "category", "description", "amount", "period", "paid", "funding"]
        widgets = {"paid": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
                   "description": forms.Textarea(attrs={"rows": 2}),
                   "funding": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk or not self.instance.funding or self.instance.funding in self.payers:
            self.fields["funding"] = forms.ChoiceField(
                label="Кто оплатил", required=False,
                choices=[("", "Не указан"), *((name, name) for name in self.payers)],
            )

    def clean(self):
        data = super().clean()
        if data.get("kind") != "build" and not data.get("period"):
            self.add_error("period", "Укажите месяц для отчёта.")
        if data.get("kind") != "tax" and data.get("amount") == 0 and not has_refund(data.get("funding")):
            self.add_error("amount", "Укажите положительную сумму.")
        return data


def monthly_results(year, entries):
    totals = defaultdict(dict)
    for r in entries.filter(period__year=year).values("period", "kind").annotate(value=Sum("amount")):
        totals[r["period"]][r["kind"]] = r["value"]
    bookings = _load(date(year, 1, 1), date(year + 1, 1, 1))
    for month in range(1, 13):
        start = date(year, month, 1)
        core = _core(bookings, start, _add_months(start, 1))
        revenue, commission = money(core.revenue), money(core.commission)
        costs = totals[start].get("run", Decimal(0))
        tax = totals[start].get("tax")
        profit = revenue - costs - commission
        yield dict(label=MONTHS_SHORT[month], revenue=revenue, commission=commission,
                   nights=core.occupied, costs=costs, tax=tax, profit=profit,
                   unit=money(costs / core.occupied) if core.occupied else None,
                   net=profit-tax if tax is not None else None)


def remaining_investment(entries):
    """Окупаемость по закрытым месяцам с известным налогом."""
    cutoff = timezone.localdate().replace(day=1)
    builds = entries.filter(kind="build")
    invested = sum((e.amount for e in builds if not e.period or e.period < cutoff), Decimal(0))
    starts = [
        Booking.objects.filter(kind=Booking.Kind.BOOKING, status=Booking.Status.CONFIRMED,
                               check_in__lt=cutoff).order_by("check_in").values_list("check_in", flat=True).first(),
        entries.exclude(kind="build").filter(period__lt=cutoff).order_by("period").values_list("period", flat=True).first(),
    ]
    first = min((d for d in starts if d), default=cutoff)
    profit = Decimal(0)
    for year in range(first.year, cutoff.year + 1):
        for month, row in enumerate(monthly_results(year, entries), 1):
            if date(year, month, 1) >= cutoff:
                break
            if row["net"] is None:
                if row["nights"] or row["costs"]:
                    return None
            else:
                profit += row["net"]
    return max(Decimal(0), invested - profit)


@transaction.atomic
def bulk_action(request, rows, *, allow_confirm=False):
    action = request.POST.get("action")
    if action == "confirm_all" and allow_confirm:
        rows = rows.filter(state=Expense.State.DRAFT)
        update = {"state": Expense.State.POSTED}
    elif action == "move":
        selected = request.POST.getlist("expense_id")
        if not selected:
            messages.info(request, "Отметьте расходы для переноса.")
            return redirect(request.get_full_path())
        if any(not s.isascii() or not s.isdigit() or len(s) > 18 for s in selected):
            return HttpResponseBadRequest()
        rows = rows.filter(pk__in=selected)
        kind = request.POST.get("target_kind", "")
        category = request.POST.get("target_category", "").strip()
        if (kind and kind not in Expense.Kind.values) or len(category) > 120:
            return HttpResponseBadRequest()
        update = {}
        if kind:
            update["kind"] = kind
        if category:
            update["category"] = category
        if not update:
            messages.info(request, "Выберите группу или укажите статью.")
            return redirect(request.get_full_path())
    else:
        return HttpResponseBadRequest()

    ready = []
    skipped = 0
    for entry in rows:
        if action == "confirm_all" or entry.state == Expense.State.POSTED:
            data = {field: getattr(entry, field) for field in ExpenseForm.Meta.fields}
            data.update(update)
            data["period"] = entry.period.strftime("%Y-%m") if entry.period else ""
            if not ExpenseForm(data, instance=entry).is_valid():
                skipped += 1
                continue
        ready.append(entry.pk)
    changed = rows.filter(pk__in=ready).update(**update)
    label = "Подтверждено" if action == "confirm_all" else "Перенесено"
    messages.success(request, f"{label}: {changed}.")
    if skipped:
        messages.warning(request, f"Требуют исправления: {skipped}. Проверьте месяц и сумму в карточке расхода.")
    return redirect(request.get_full_path())


def selected_year(request):
    raw = request.GET.get("year", "")
    if raw.isascii() and raw.isdigit() and len(raw) == 4 and 1900 <= int(raw) <= 9998:
        return int(raw)


def expense_years(rows, selected=None):
    years = {d.year for d in rows.dates("period", "year")}
    years.update(rows.filter(period__isnull=True, year_only__isnull=False).values_list("year_only", flat=True))
    years.add(timezone.localdate().year)
    if selected:
        years.add(selected)
    return sorted(years, reverse=True)


@admin_required
def overview(request):
    tab = request.GET.get("tab", "stats")
    if tab not in ("build", "run", "report", "stats"):
        tab = "stats"
    entries = Expense.objects.filter(state="posted")
    year = selected_year(request)
    years = expense_years(entries, year)
    if tab == "report":
        for start, end in Booking.objects.values_list("check_in", "check_out"):
            years.extend(range(start.year, end.year + 1))
        years = sorted(set(years), reverse=True)
    category = request.GET.get("category", "")
    rows = entries.filter(kind="build") if tab == "build" else entries.exclude(kind="build")
    if tab == "stats":
        rows = entries
        context_years = {str(y): y for y in years}
        baseline = context_years.get(request.GET.get("compare"))
        if baseline and year is None:
            year = next((y for y in years if y != baseline), baseline)
    categories = rows.order_by("category").values_list("category", flat=True).distinct()
    months = list(enumerate(MONTHS_SHORT))[1:]
    month = request.GET.get("month", "")
    if month in {str(number) for number, _ in months}:
        rows = rows.filter(period__month=int(month))
    if category:
        rows = rows.filter(category=category)
    comparison_rows = rows
    if year:
        rows = rows.filter(Q(period__year=year) | Q(period__isnull=True, year_only=year))
    if request.method == "POST":
        if tab in ("report", "stats"):
            return HttpResponseBadRequest()
        return bulk_action(request, rows)
    year_report = year or timezone.localdate().year
    remaining = None
    if tab == "report" and entries.filter(kind="build").exists() and not Expense.objects.filter(state="draft").exists():
        remaining = remaining_investment(entries)
    context = dict(tab=tab, rows=rows, categories=categories, category=category, months=months, month=month,
                   groups=rows.order_by("category").values("category").annotate(total=Sum("amount")),
                   total=rows.aggregate(s=Sum("amount"))["s"] or 0,
                   invested=entries.filter(kind="build").aggregate(s=Sum("amount"))["s"] or 0,
                   pending=Expense.objects.filter(state="draft").count(), remaining=remaining,
                   years=years, year=year_report if tab=="report" else year,
                   report=list(monthly_results(year_report, entries)) if tab=="report" else [])
    if tab == "stats":
        valid_month = next((n for n, _ in months if str(n) == month), None)
        context["dashboard"] = dashboard(rows, year, valid_month, category)
        context.update(years=years, baseline=baseline)
        if baseline and year != baseline:
            context["comparison"] = compare_expenses(comparison_rows, year, baseline, valid_month)
        return render(request, "booking/expense_stats.html", context)
    return render(request, "booking/expenses.html", context)


@admin_required
def edit(request, pk=None):
    entry = get_object_or_404(Expense, pk=pk) if pk else Expense()
    draft = entry.state == "draft" and bool(pk)
    if request.method == "POST" and request.POST.get("action") == "skip" and draft:
        Expense.objects.filter(pk=pk, state="draft").update(state="skipped")
        return redirect("expense_import")
    form = ExpenseForm(request.POST if request.method == "POST" else None, instance=entry,
                       initial={} if pk else {"kind": request.GET.get("kind", "run")})
    if request.method == "POST" and form.is_valid():
        entry = form.save(commit=False)
        entry.state = "posted"
        entry.save()
        messages.success(request, "Расход учтён.")
        if draft:
            return redirect("expense_import")
        return redirect(reverse("expenses") + ("?tab=build" if entry.kind=="build" else "?tab=run"))
    return render(request, "booking/expense_edit.html", dict(form=form, entry=entry, draft=draft,
                  years=expense_years(Expense.objects.all(), entry.period.year if entry.period else None)))


@transaction.atomic
def stage(upload):
    records = list(parse_workbook(upload))
    if not records:
        raise ValueError("В книге не найдены строки журнала дома.")
    created = confirmed = 0
    for record in records:
        entry, added = Expense.objects.get_or_create(source=record["source"], defaults=record)
        created += added
        fields = []
        if entry.state == Expense.State.SKIPPED:
            continue
        if entry.state == Expense.State.DRAFT and not entry.amount and record["amount"] > 0:
            for field in ("amount", "funding", "original"):
                setattr(entry, field, record[field])
                fields.append(field)
        old_payment_month = entry.paid.replace(day=1) if entry.paid else None
        if record["period"] and entry.period != record["period"] and entry.period in (None, old_payment_month):
            entry.period = record["period"]
            fields.append("period")
        if not entry.period and record["year_only"] and entry.year_only != record["year_only"]:
            entry.year_only = record["year_only"]
            fields.append("year_only")
        if entry.state == Expense.State.DRAFT and (entry.amount > 0 or has_refund(entry.funding)) and (entry.period or entry.year_only):
            entry.state = Expense.State.POSTED
            confirmed += 1
            fields.append("state")
        if fields:
            entry.save(update_fields=fields)
    return created, len(records)-created, confirmed


@admin_required
def review(request):
    state = request.GET.get("state", "draft")
    if state not in Expense.State.values:
        state = "draft"
    rows = Expense.objects.filter(source__isnull=False, state=state).order_by("-period", "pk")
    year = selected_year(request)
    years = expense_years(rows, year)
    if year:
        rows = rows.filter(Q(period__year=year) | Q(period__isnull=True, year_only=year))
    if request.method == "POST":
        if "action" in request.POST:
            return bulk_action(request, rows, allow_confirm=state == "draft")
        upload = request.FILES.get("file")
        if not upload or upload.size > 10_000_000:
            messages.error(request, "Выберите файл XLSX до 10 МБ.")
        else:
            try:
                added, existing, confirmed = stage(upload)
                messages.success(request, f"Новых строк: {added}. Уже загружено ранее: {existing}. Автоматически подтверждено: {confirmed}.")
            except (ValueError, BadZipFile, InvalidFileException, ParseError, KeyError, OSError):
                messages.error(request, "Не удалось прочитать журнал. Проверьте файл и названия листов.")
        return redirect("expense_import")
    return render(request, "booking/expense_review.html", dict(rows=rows, state=state, states=Expense.State.choices,
                                                               year=year, years=years))
