from datetime import date
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from openpyxl import Workbook

from .expense_import import expense_month
from .expenses import monthly_results, stage
from .models import Booking, Expense, PayerContribution


class ExpenseTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username="owner", is_superuser=True)
        self.client.force_login(self.user)

    def test_review_and_report(self):
        period = date(2026, 1, 1)
        expense = Expense.objects.create(kind="run", amount=100, period=period, source="Лист · 12")
        Booking.objects.create(check_in=date(2025, 12, 31), check_out=date(2026, 1, 2),
                               total_price=2000, status=Booking.Status.CONFIRMED)
        def january():
            return next(monthly_results(2026, Expense.objects.filter(state="posted")))
        self.assertEqual(january()["costs"], 0)
        response = self.client.post(reverse("expense_edit", args=[expense.pk]), {
            "period": "2026-01", "kind": "run", "category": "Стирка",
            "description": "Прачечная", "amount": "99.50",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(january()["unit"], Decimal("99.50"))
        self.assertEqual(january()["profit"], Decimal("900.50"))
        self.assertIsNone(january()["net"])
        Expense.objects.create(kind="tax", state="posted", amount=0, period=period)
        self.assertEqual(january()["net"], Decimal("900.50"))
        february = list(monthly_results(2026, Expense.objects.filter(state="posted")))[1]
        self.assertIsNone(february["unit"])
        for tab in ("run", "build", "report"):
            self.assertEqual(self.client.get(reverse("expenses"), {"tab": tab, "year": 2026}).status_code, 200)
        for name in ("expense_import", "expense_new"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_default_dashboard_and_new_expense_payer(self):
        self.assertTemplateUsed(self.client.get(reverse("expenses")), "booking/expense_stats.html")
        url = reverse("expense_new")
        self.assertContains(self.client.get(url), '<select name="funding"')
        data = dict(kind="run", period="2026-03", category="Уборка", description="Клининг",
                    amount="500.50", funding="Староста")
        self.assertRedirects(self.client.post(url, data), reverse("expenses") + "?tab=run")
        expense = Expense.objects.get(description="Клининг")
        self.assertEqual(expense.funding, "Староста")
        stats = self.client.get(reverse("expenses"), {"year": 2026}).context["dashboard"]
        self.assertEqual(stats["payers"][0]["paid"], Decimal("500.50"))
        expense.funding = "Староста: 300.00, Копилка: 200.50"
        expense.save()
        edit = self.client.get(reverse("expense_edit", args=[expense.pk]))
        self.assertContains(edit, expense.funding)

    def test_import_period_auto_confirmation_and_dashboard(self):
        for value in ("май 25", "25-27.05.2025", "26,27.05..25"):
            self.assertEqual(expense_month(value), date(2025, 5, 1))
        self.assertIsNone(expense_month("30.04.2025-02.05.2025"))
        book = Workbook()
        sheet = book.active
        sheet.title = "Текущее содержание"
        sheet["A7"] = "декабрь 2023"
        for row, amount in ((7, 100), (8, 50), (9, 0)):
            for column, value in enumerate((row, "Свет", "С копилки Саша", amount, date(2023, 11, 30)), 2):
                sheet.cell(row, column, value)
        sheet.merge_cells("A7:A9")
        annual = book.create_sheet("Строительство дома 2023")
        for row, amount in ((12, 100), (13, 50), (14, 0)):
            annual.cell(row, 1, row)
            annual.cell(row, 2, "декабрь 2023")
            annual.cell(row, 3, "Свет")
            annual.cell(row, 9, amount)
        upload = BytesIO()
        book.save(upload)
        self.assertEqual(stage(upload), (3, 0, 2))
        self.assertEqual(stage(upload), (0, 3, 0))
        self.assertFalse(Expense.objects.filter(source__startswith="Текущее содержание · ").exists())
        posted = Expense.objects.filter(state="posted")
        self.assertEqual(posted.filter(period=date(2023, 12, 1)).count(), 2)
        response = self.client.get(reverse("expenses"), {"tab": "stats", "year": 2023})
        self.assertContains(response, "Кто сколько оплатил")
        stats = response.context["dashboard"]
        self.assertEqual(stats["total"], 150)
        payers = {p["label"]: p for p in stats["payers"]}
        self.assertEqual(payers["Копилка"]["paid"], 150)
        self.assertEqual(stats["trend"][-1]["total"], 150)

    def test_refunds_reduce_only_recipient_balance(self):
        PayerContribution.objects.create(payer="Староста", paid=date(2023, 2, 1), amount=200, source="Тест")
        Expense.objects.create(state="posted", kind="run", period=date(2023, 2, 1), amount=150,
                               funding="Староста: -30.00, Копилка: 150.00")
        response = self.client.get(reverse("expenses"), {"tab": "stats", "year": 2023, "month": 2})
        stats = response.context["dashboard"]
        payers = {p["label"]: p for p in stats["payers"]}
        self.assertEqual(payers["Староста"]["amount"], 200-30)
        self.assertEqual(payers["Копилка"]["amount"], 150)
        self.assertEqual(stats["total"], 150)

    def test_bulk_confirm_and_transfer(self):
        def item(source, **fields):
            return Expense.objects.create(source=source, category="Дом", description="Работа",
                                          amount=100, **fields)

        ready = item("A", kind="build", period=date(2026, 1, 1))
        incomplete = item("B", kind="run")
        skipped = item("C", kind="build", state="skipped")
        url = reverse("expense_import")
        for _ in range(2):
            self.assertEqual(self.client.post(url, {"action": "confirm_all"}).status_code, 302)
        self.assertEqual(list(Expense.objects.filter(state="posted")), [ready])
        self.assertTrue(Expense.objects.filter(pk=skipped.pk, state="skipped").exists())
        self.client.post(url, {"action": "move", "expense_id": incomplete.pk, "target_kind": "build"})
        self.assertTrue(Expense.objects.filter(pk=incomplete.pk, kind="build", state="draft").exists())
        response = self.client.post(reverse("expenses") + "?tab=build", {
            "action": "move", "expense_id": [ready.pk, incomplete.pk], "target_kind": "run",
            "target_category": "Уборка",
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        ready.refresh_from_db()
        self.assertEqual((ready.kind, ready.category, ready.state), ("run", "Уборка", "posted"))
        self.assertEqual(next(monthly_results(2026, Expense.objects.filter(state="posted")))["costs"], 100)

    def test_comparison_uses_same_month_category_and_state(self):
        for year, month, amount, category, state in [
            (2024, 2, 120, "Дом", "posted"), (2023, 2, 80, "Дом", "posted"),
            (2023, 3, 700, "Дом", "posted"), (2024, 2, 900, "Сад", "posted"),
            (2023, 2, 400, "Дом", "draft"),
        ]:
            Expense.objects.create(kind="build", period=date(year, month, 1), amount=amount,
                                   category=category, state=state)
        response = self.client.get(reverse("expenses"), dict(tab="stats", year=2024, compare=2023,
                                                             month=2, category="Дом"))
        self.assertContains(response, 'id="expense-comparison"')
        result = response.context["comparison"]
        self.assertEqual((result["values"], result["base_values"]), ([120], [80]))
        self.assertEqual((result["delta"], result["percent"]), (40, 50))

    def test_review_actions_respect_year_buttons(self):
        for year in (2023, 2024):
            Expense.objects.create(kind="build", category="Дом", description="Работа", amount=12,
                                   source=str(year), period=date(year, 1, 1))
        url = reverse("expense_import") + "?year=2023"
        self.assertContains(self.client.get(url), 'value="2023" checked')
        self.client.post(url, {"action": "confirm_all"})
        self.assertEqual(Expense.objects.get(source="2024").state, "draft")
        self.assertEqual(Expense.objects.get(source="2023").state, "posted")
        for tab in ("build", "run", "report", "stats"):
            response = self.client.get(reverse("expenses"), {"tab": tab, "year": 2023})
            self.assertContains(response, 'name="year" value="2023" checked')

    def test_undated_preliminary_expense_counts_in_year_only(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "Предварительные расходы"
        sheet.append(["Журнал"])
        sheet.append(["№", "Дата", "Назначение", "Плательщик"])
        sheet.append([1, None, "Сваи", 300])
        upload = BytesIO()
        book.save(upload)
        self.assertEqual(stage(upload), (1, 0, 1))
        entry = Expense.objects.get()
        self.assertEqual((entry.period, entry.paid, entry.year_only), (None, None, 2018))
        for filters, total in [({"year":2018}, 300), ({"year":2018, "month":1}, 0)]:
            response = self.client.get(reverse("expenses"), filters)
            self.assertEqual(response.context["dashboard"]["total"], total)

    def test_manager_cannot_view_finances(self):
        self.user.is_superuser = False
        self.user.save()
        for name in ("expenses", "expense_import", "expense_new"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)
