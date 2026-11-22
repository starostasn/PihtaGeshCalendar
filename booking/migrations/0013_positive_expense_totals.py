import re
from decimal import Decimal

from django.db import migrations


def recalculate(apps, schema_editor):
    expenses = apps.get_model("booking", "Expense").objects.using(schema_editor.connection.alias)
    for entry in expenses.filter(source__isnull=False):
        prefix, sep, raw_total = entry.original.rpartition("Сумма строки: ")
        if not sep or not re.fullmatch(r"-?\d+(?:\.\d+)?", raw_total):
            continue
        old_total = Decimal(raw_total)
        amounts = [Decimal(v) for v in re.findall(r": (-?\d+\.\d{2})(?=, |$)", entry.funding)]
        if amounts and sum(amounts) == old_total and any(v < 0 for v in amounts):
            total = sum((v for v in amounts if v > 0), Decimal(0))
        elif not entry.funding and old_total < 0:
            total = Decimal(0)
        else:
            continue
        prefix = prefix.replace("Есть взаиморасчёты с участниками. ", "Отрицательные значения исключены из суммы. ")
        if total:
            prefix = prefix.replace("Проверить наличие оплаты. ", "")
        entry.original = f"{prefix}{sep}{total}"
        entry.amount = total
        entry.save(update_fields=["original", "amount"])


class Migration(migrations.Migration):
    dependencies = [("booking", "0012_expense")]
    operations = [migrations.RunPython(recalculate, migrations.RunPython.noop)]
