"""Название дома — «ПихтаГеш».

Меняются только автоматически заданные названия (по умолчанию или из объявления);
если название уже вписано вручную, оно не трогается.
"""

from django.db import migrations

AUTO_NAMES = {"Гостевой дом", "Пихта", "Пихта Гэш", "Пихта Геш", "ПихтаГэш"}


def rename(apps, schema_editor):
    House = apps.get_model("booking", "House")
    House.objects.filter(name__in=AUTO_NAMES).update(name="ПихтаГеш")


class Migration(migrations.Migration):
    dependencies = [("booking", "0008_fill_house_from_listing_again")]
    operations = [migrations.RunPython(rename, migrations.RunPython.noop)]
