from django.db import migrations


def exclude_sheet(apps, schema_editor):
    expenses = apps.get_model("booking", "Expense").objects.using(schema_editor.connection.alias)
    expenses.filter(source__startswith="Текущее содержание · ").update(state="skipped")


class Migration(migrations.Migration):
    dependencies = [("booking", "0013_positive_expense_totals")]
    operations = [migrations.RunPython(exclude_sheet, migrations.RunPython.noop)]
