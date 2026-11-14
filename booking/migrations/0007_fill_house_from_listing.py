"""Заполняет «О доме» сведениями из объявления на Авито (booking/data/house_pihta.json).

Меняются только пустые поля и настройки, которые остались по умолчанию
(название «Гостевой дом», заезд 14:00, максимум 10 гостей). То, что уже заполнено вручную, не трогается.
"""

import json
from datetime import time
from pathlib import Path

from django.db import migrations

DATA = Path(__file__).resolve().parent.parent / "data" / "house_pihta.json"
TEXT_FIELDS = [
    "tagline", "description", "address", "floors", "layout", "amenities",
    "location_info", "rules", "cancellation_policy", "extra_services",
]
NUMBER_FIELDS = ["area_m2", "plot_sotok", "rooms", "deposit", "prepayment_percent"]


def fill(apps, schema_editor):
    House = apps.get_model("booking", "House")
    if not DATA.exists():
        return
    data = json.loads(DATA.read_text(encoding="utf-8"))
    if not House.objects.exists():
        House.objects.create()  # запись о доме создаётся при первом открытии цен — может ещё не быть
    for house in House.objects.all():
        for field in TEXT_FIELDS:
            if not getattr(house, field) and data.get(field):
                setattr(house, field, data[field])
        for field in NUMBER_FIELDS:
            if getattr(house, field) is None and data.get(field) is not None:
                setattr(house, field, data[field])
        if house.name == "Гостевой дом" and data.get("name"):
            house.name = data["name"]
        if house.check_in_time == time(14, 0) and data.get("check_in_time"):
            house.check_in_time = time.fromisoformat(data["check_in_time"])
        if house.check_out_time == time(12, 0) and data.get("check_out_time"):
            house.check_out_time = time.fromisoformat(data["check_out_time"])
        if house.max_guests == 10 and data.get("max_guests"):
            house.max_guests = data["max_guests"]
        house.save()


class Migration(migrations.Migration):
    dependencies = [("booking", "0006_house_info_photos")]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
