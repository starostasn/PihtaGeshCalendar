"""Повторное заполнение «О доме» из объявления.

В первом выпуске файл booking/data/house_pihta.json не попал в репозиторий из-за правила .gitignore,
и миграция 0007 могла отработать впустую. Заполнение безопасно повторять: меняются только пустые поля.
"""

import importlib

from django.db import migrations

_fill = importlib.import_module("booking.migrations.0007_fill_house_from_listing").fill


class Migration(migrations.Migration):
    dependencies = [("booking", "0007_fill_house_from_listing")]
    operations = [migrations.RunPython(_fill, migrations.RunPython.noop)]
