"""Чтение журнала расходов дома. Сводные значения и поступления не импортируются."""
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from zipfile import ZipFile

from openpyxl import load_workbook

MONTHS = "январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь".split()


def money(value):
    return Decimal(str(value)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)


def has_refund(funding):
    return bool(re.search(r": -[0-9]+(?:\.[0-9]+)?(?:, |$)", funding or ""))


def expense_month(value, year=None):
    if isinstance(value, date):
        return date(value.year, value.month, 1)
    text = str(value or "").strip().lower()
    named = re.fullmatch(r"(" + "|".join(MONTHS) + r")(?:\s+(\d{4}|\d{2}))?", text)
    if named:
        if named[2]:
            year = int(named[2]) + (2000 if len(named[2]) == 2 else 0)
        return date(year, MONTHS.index(named[1]) + 1, 1) if year else None
    periods = set()
    for day, month, y in re.findall(r"\b(\d{1,2})\.(\d{1,2})\.+(\d{4}|\d{2})\b", text):
        try:
            periods.add(date(int(y) + (2000 if len(y) == 2 else 0), int(month), int(day)).replace(day=1))
        except ValueError:
            return None
    return periods.pop() if len(periods) == 1 else None


def parse_workbook(upload):
    with ZipFile(upload) as archive:
        if sum(f.file_size for f in archive.infolist()) > 40_000_000:
            raise ValueError("Слишком большой объём распакованной книги.")
    upload.seek(0)
    book = load_workbook(upload, data_only=True, read_only=True, keep_links=False)
    try:
        for sheet in book:
            annual = re.fullmatch(r"Строительство дома (\d{4})", sheet.title)
            preliminary = sheet.title == "Предварительные расходы"
            if not (annual or preliminary):
                continue
            year = int(annual[1]) if annual else None
            first = 3 if preliminary else 6 if year == 2022 else 12
            labels = []
            for index, row in enumerate(sheet.iter_rows(max_col=9, values_only=True), 1):
                if index in (2, 10) and index < first:
                    labels = row[3:7]
                number = row[0]
                if index < first or not isinstance(number, (float, int)) or not row[2]:
                    continue
                raw_date = row[1]
                paid = raw_date.date() if isinstance(raw_date, datetime) else None
                period = expense_month(raw_date, year)
                parts = list(zip(labels, row[3:7]))
                if year and year >= 2023:
                    parts.append(("Копилка", row[8]))
                amounts = [(label, money(v)) for label, v in parts if isinstance(v, (int, float)) and v]
                total = sum((v for _, v in amounts if v > 0), Decimal(0))
                if not amounts and isinstance(row[7], (float, int)):
                    total = max(money(row[7]), Decimal(0))
                notes = f"Дата в книге: {raw_date or 'не указана'}. "
                if any(v < 0 for _, v in amounts):
                    notes += "Отрицательные значения учтены как возвраты плательщикам. "
                if not any(v for _, v in amounts) and not total:
                    notes += "Проверить наличие оплаты. "
                yield dict(source=f"{sheet.title} · {index}", original=notes + f"Сумма строки: {total}",
                           period=period, paid=paid, amount=total,
                           funding=", ".join(f"{k}: {v}" for k,v in amounts),
                           description=str(row[2]), category="Без категории",
                           kind="build" if preliminary or year and year <= 2023 else "run")
    finally:
        book.close()
