"""Загрузка старых броней из выгрузки RealtyCalendar (Excel XML 2003, .xls).

Порядок: parse_file() -> строки; build_plan() -> что будет сделано с каждой строкой (предпросмотр);
apply_batch() -> создать всё одной транзакцией; revert_batch() -> отменить загрузку.
Повторная загрузка того же файла ничего не задвоит: брони на те же даты считаются дублями.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime

from django.db import transaction
from django.utils import timezone

from .models import Booking, Channel, Guest, ImportBatch, Payment
from .phones import format_phone, normalize_phone

SS = "urn:schemas-microsoft-com:office:spreadsheet"
MAX_FILE_BYTES = 5 * 1024 * 1024
COLUMNS = {
    "object": "Объект",
    "check_in": "Заезд",
    "check_out": "Выезд",
    "contacts": "Контакты",
    "notes": "Примечания",
    "guests": "Гостей",
    "total": "Сумма",
    "source": "Источник",
    "manager": "Менеджер",
}
IMPORT_MARK = "Импорт из RealtyCalendar"


class ImportFileError(Exception):
    pass


# ---------------------------------------------------------------- чтение файла


def read_spreadsheet(data: bytes) -> list[list[str]]:
    """Excel XML 2003 (SpreadsheetML) -> список строк (ячейки как текст)."""
    if len(data) > MAX_FILE_BYTES:
        raise ImportFileError("Файл больше 5 МБ")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    head = data[:1000].lstrip()
    if b"<!DOCTYPE" in data[:2000] or b"<!ENTITY" in data[:5000]:
        raise ImportFileError("Файл содержит недопустимые XML-конструкции")
    if not head.startswith(b"<?xml") and b"<Workbook" not in head:
        raise ImportFileError(
            "Это не выгрузка в формате Excel XML. Выгрузите брони из RealtyCalendar в .xls и загрузите файл без изменений."
        )
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ImportFileError(f"Не удалось прочитать файл: {exc}") from exc
    table = root.find(f".//{{{SS}}}Worksheet/{{{SS}}}Table")
    if table is None:
        raise ImportFileError("В файле нет таблицы")
    rows = []
    for row in table.findall(f"{{{SS}}}Row"):
        cells: list[str] = []
        for cell in row.findall(f"{{{SS}}}Cell"):
            index = cell.get(f"{{{SS}}}Index")
            if index and index.isdigit():
                while len(cells) < int(index) - 1:
                    cells.append("")
            node = cell.find(f"{{{SS}}}Data")
            cells.append("".join(node.itertext()).strip() if node is not None else "")
        rows.append(cells)
    return rows


# ---------------------------------------------------------------- разбор ячеек

PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{8,}\d")
DATE_IN_TEXT_RE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b")


def parse_date(value: str) -> date | None:
    value = (value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            continue
    return None


def parse_contacts(text: str) -> tuple[str, str]:
    """'Анна Смирнова +7 912 345-67-89' -> ('Анна Смирнова', '+79123456789')."""
    text = DATE_IN_TEXT_RE.sub(" ", (text or "").strip())  # даты (например, дату рождения) не храним
    phone = ""
    for match in PHONE_RE.finditer(text):
        phone = normalize_phone(match.group())
        if phone:
            text = text[: match.start()] + " " + text[match.end() :]
            break
    name = " ".join(text.replace(",", " ").replace(";", " ").split()).strip(" -–:")
    return name, phone


GUESTS_PATTERNS = [
    re.compile(r"гост\w*\s*[:\-]?\s*(\d{1,2})\b", re.I),  # «Гостей: 3»
    re.compile(r"\b(\d{1,2})\s*(?:гост|чел|взросл)", re.I),  # «4 гостей», «3 человека», «2 взрослых»
]
CHILDREN_RE = re.compile(r"\b(\d{1,2})\s*(?:реб|дет)", re.I)
ONE_CHILD_RE = re.compile(r"(?:\bи|\+)\s*(?:реб[её]нок|ребенка)", re.I)


def parse_guests_count(column: str, notes: str) -> int | None:
    if (column or "").strip().isdigit():
        return int(column)
    adults = None
    for pattern in GUESTS_PATTERNS:
        m = pattern.search(notes or "")
        if m:
            adults = int(m.group(1))
            break
    if adults is None:
        return None
    children = 0
    m = CHILDREN_RE.search(notes or "")
    if m:
        children = int(m.group(1))
    elif ONE_CHILD_RE.search(notes or ""):
        children = 1
    total = adults + children
    return total if 0 < total <= 50 else None


def parse_total(value: str) -> int | None:
    value = (value or "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        number = float(value)
    except ValueError:
        return None
    return round(number) if number >= 0 else None


def parse_file(data: bytes) -> list[dict]:
    rows = read_spreadsheet(data)
    if not rows:
        raise ImportFileError("Файл пустой")
    header = [h.strip() for h in rows[0]]
    index = {key: header.index(title) for key, title in COLUMNS.items() if title in header}
    missing = [COLUMNS[k] for k in ("check_in", "check_out") if k not in index]
    if missing:
        raise ImportFileError(f"В файле нет колонок: {', '.join(missing)}. Нужна выгрузка броней из RealtyCalendar.")

    def cell(r, key):
        i = index.get(key)
        return r[i].strip() if i is not None and i < len(r) else ""

    parsed = []
    for number, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        check_in, check_out = parse_date(cell(r, "check_in")), parse_date(cell(r, "check_out"))
        name, phone = parse_contacts(cell(r, "contacts"))
        notes = cell(r, "notes")
        error = ""
        if not check_in or not check_out:
            error = "не распознаны даты"
        elif check_out <= check_in:
            error = "выезд не позже заезда"
        parsed.append(
            {
                "line": number,
                "object": cell(r, "object"),
                "check_in": check_in.isoformat() if check_in else "",
                "check_out": check_out.isoformat() if check_out else "",
                "contacts": cell(r, "contacts"),
                "name": name,
                "phone": phone,
                "notes": notes,
                "guests": parse_guests_count(cell(r, "guests"), notes),
                "total": parse_total(cell(r, "total")),
                "source": cell(r, "source"),
                "manager": cell(r, "manager"),
                "error": error,
            }
        )
    if not parsed:
        raise ImportFileError("В файле нет броней")
    return parsed


# ---------------------------------------------------------------- источники -> площадки

KNOWN_SOURCES = [
    ("avito", "Авито", "авито"),
    ("yandex", "Яндекс Путешествия", "яндекс"),
    ("sutochno", "Суточно", "суточно"),
    ("ostrovok", "Островок", "островок"),
    ("booking", "Booking.com", "booking"),
    ("airbnb", "Airbnb", "airbnb"),
    ("tvil", "ТВИЛ", "твил"),
]
DIRECT_SOURCES = {"", "manual", "widget", "direct", "phone", "site"}


def default_mapping(sources: list[str]) -> dict[str, str]:
    """Значение для каждого источника: '' — напрямую, 'ch:<id>' — площадка, 'new:<название>' — создать."""
    channels = list(Channel.objects.all())
    mapping = {}
    for source in sources:
        key = source.strip().lower()
        value = ""
        if key not in DIRECT_SOURCES:
            for needle, title, ru in KNOWN_SOURCES:
                if needle in key:
                    found = next((c for c in channels if ru in c.name.lower() or needle in c.name.lower()), None)
                    value = f"ch:{found.pk}" if found else f"new:{title}"
                    break
        mapping[source] = value
    return mapping


def mapping_choices(sources: list[str]) -> list[tuple[str, str]]:
    choices = [("", "Напрямую")] + [(f"ch:{c.pk}", c.name) for c in Channel.objects.all()]
    existing = {label.lower() for _, label in choices}
    for source in sources:
        for needle, title, _ in KNOWN_SOURCES:
            if needle in source.lower() and title.lower() not in existing:
                choices.append((f"new:{title}", f"Создать площадку «{title}»"))
                existing.add(title.lower())
    return choices


def source_label(value: str) -> str:
    if not value:
        return "Напрямую"
    if value.startswith("new:"):
        return value[4:] + " (новая)"
    channel = Channel.objects.filter(pk=value[3:]).first() if value[3:].isdigit() else None
    return channel.name if channel else "?"


# ---------------------------------------------------------------- план (предпросмотр)


@dataclass
class PlanRow:
    data: dict
    action: str  # create | enrich | same | conflict | error
    note: str = ""
    target: Booking | None = None
    guest: Guest | None = None
    guest_is_new: bool = False
    source_value: str = ""
    paid: bool = False
    is_block: bool = False  # закрытые даты (своё проживание, заморозка), а не бронь гостя

    @property
    def check_in(self):
        return date.fromisoformat(self.data["check_in"]) if self.data["check_in"] else None

    @property
    def check_out(self):
        return date.fromisoformat(self.data["check_out"]) if self.data["check_out"] else None

    @property
    def nights(self):
        return (self.check_out - self.check_in).days if self.check_in and self.check_out else 0

    @property
    def phone_display(self):
        return format_phone(self.data["phone"])

    @property
    def source_display(self):
        return source_label(self.source_value)


def build_plan(batch: ImportBatch) -> list[PlanRow]:
    today = timezone.localdate()
    mapping = batch.options.get("mapping", {})
    mark_paid = batch.options.get("mark_paid", True)
    zero_as_block = batch.options.get("zero_as_block", True)
    guests_by_phone = {g.phone: g for g in Guest.objects.exclude(phone="")}
    guests_by_name = {}
    for g in Guest.objects.filter(phone=""):
        guests_by_name.setdefault(Guest.search_key(g.name), g)
    new_by_phone, new_by_name = {}, {}
    plan = []
    for row in batch.rows:
        item = PlanRow(data=row, action="create", source_value=mapping.get(row["source"], ""))
        item.is_block = bool(zero_as_block and not row["name"] and not row["phone"] and not row["total"])
        if item.is_block:
            item.source_value = ""
        if row["error"]:
            item.action, item.note = "error", row["error"]
            plan.append(item)
            continue
        # гость
        key = Guest.search_key(row["name"])
        if row["phone"]:
            item.guest = guests_by_phone.get(row["phone"])
            if item.guest is None:
                item.guest_is_new = True
                new_by_phone.setdefault(row["phone"], row)
        elif key:
            item.guest = guests_by_name.get(key)
            if item.guest is None:
                item.guest_is_new = True
                new_by_name.setdefault(key, row)
        # пересечения с бронями, которые уже есть
        overlaps = list(
            Booking.objects.active()
            .overlapping(item.check_in, item.check_out)
            .select_related("guest", "channel")
            .order_by("check_in")
        )
        same = [b for b in overlaps if b.check_in == item.check_in and b.check_out == item.check_out]
        item.paid = bool(mark_paid and row["total"] and item.check_in < today and not item.is_block)
        if same:
            t = same[0]
            item.target = t
            fillable = t.kind == Booking.Kind.BOOKING and not item.is_block  # закрытые даты не дополняем
            would_change = IMPORT_MARK not in t.comment or (
                fillable
                and (
                    (t.guest_id is None and (item.guest is not None or item.guest_is_new))
                    or (t.total_price is None and row["total"] is not None)
                    or (t.guests is None and row["guests"] is not None)
                    or (item.paid and not t.payments.exists())
                )
            )
            if would_change:
                item.action, item.note = "enrich", "уже есть бронь на эти даты — дополним пустые поля"
            else:
                item.action, item.note = "same", "уже загружено — без изменений"
        elif overlaps:
            o = overlaps[0]
            item.action = "conflict"
            item.note = f"пересекается с бронью {o.check_in:%d.%m}–{o.check_out:%d.%m.%Y} ({o.title}) — пропустим"
        plan.append(item)
    return plan


def plan_summary(plan: list[PlanRow]) -> dict:
    new_guests = set()
    for p in plan:
        if p.action in ("create", "enrich") and p.guest_is_new:
            new_guests.add(p.data["phone"] or "name:" + Guest.search_key(p.data["name"]))
    return {
        "total": len(plan),
        "create": sum(p.action == "create" for p in plan),
        "enrich": sum(p.action == "enrich" for p in plan),
        "same": sum(p.action == "same" for p in plan),
        "conflict": sum(p.action == "conflict" for p in plan),
        "error": sum(p.action == "error" for p in plan),
        "new_guests": len(new_guests),
        "no_phone": sum(1 for p in plan if p.action in ("create", "enrich") and p.data["name"] and not p.data["phone"]),
        "paid": sum(p.paid for p in plan if p.action in ("create", "enrich")),
        "future": sum(1 for p in plan if p.action == "create" and p.check_in and p.check_in >= timezone.localdate()),
        "blocks": sum(1 for p in plan if p.action == "create" and p.is_block),
    }


# ---------------------------------------------------------------- применение и отмена


def _comment(row: dict) -> str:
    head = IMPORT_MARK + (f" · менеджер: {row['manager']}" if row["manager"] else "")
    return f"{head}\n{row['notes']}".strip()


@transaction.atomic
def apply_batch(batch: ImportBatch, user) -> dict:
    if batch.status != ImportBatch.Status.DRAFT:
        raise ImportFileError("Эта загрузка уже применена или отменена")
    plan = build_plan(batch)  # пересчитываем на момент применения — данные могли измениться
    channels: dict[str, Channel | None] = {"": None}
    created_channels = []

    def channel_for(value: str) -> Channel | None:
        if value not in channels:
            if value.startswith("new:"):
                title = value[4:]
                ch = Channel.objects.filter(name__iexact=title).first()
                if ch is None:
                    ch = Channel.objects.create(name=title, color="#8E8E93")
                    created_channels.append(ch.pk)
                channels[value] = ch
            else:
                channels[value] = Channel.objects.filter(pk=value[3:]).first()
        return channels[value]

    new_guests: dict[str, Guest] = {}

    def guest_for(item: PlanRow) -> Guest | None:
        if item.guest is not None:
            return item.guest
        if not item.guest_is_new:
            return None
        row = item.data
        key = row["phone"] or "name:" + Guest.search_key(row["name"])
        if key not in new_guests:
            new_guests[key] = Guest.objects.create(
                name=row["name"] or "Без имени",
                phone=row["phone"],
                notes="Добавлен при импорте из RealtyCalendar" + ("" if row["phone"] else " — телефона в файле не было"),
                import_batch=batch,
                created_by=user,
            )
        return new_guests[key]

    def add_payment(booking: Booking, row: dict, channel: Channel | None):
        Payment.objects.create(
            booking=booking,
            amount=row["total"],
            date=booking.check_in,
            method=Payment.Method.PLATFORM if channel else Payment.Method.OTHER,
            comment="Импорт: бронь оплачена",
            import_batch=batch,
            created_by=user,
        )

    created = enriched_count = skipped = unchanged = 0
    enriched_log = []
    for item in plan:
        row = item.data
        if item.action in ("error", "conflict"):
            skipped += 1
            continue
        if item.action == "same":
            unchanged += 1
            continue
        channel = channel_for(item.source_value)
        guest = guest_for(item)
        if item.action == "create" and item.is_block:
            Booking.objects.create(
                kind=Booking.Kind.BLOCK,
                status=Booking.Status.CONFIRMED,
                check_in=item.check_in,
                check_out=item.check_out,
                comment=(row["notes"] or "Закрыто") + "\n" + IMPORT_MARK,
                import_batch=batch,
                created_by=user,
            )
            created += 1
        elif item.action == "create":
            booking = Booking.objects.create(
                kind=Booking.Kind.BOOKING,
                status=Booking.Status.CONFIRMED,
                check_in=item.check_in,
                check_out=item.check_out,
                channel=channel,
                guest=guest,
                guests=row["guests"],
                total_price=row["total"],
                comment=_comment(row),
                import_batch=batch,
                created_by=user,
            )
            if item.paid:
                add_payment(booking, row, channel)
            created += 1
        else:  # enrich: дополняем только пустые поля существующей брони
            booking = item.target
            changes = {}
            fillable = booking.kind == Booking.Kind.BOOKING and not item.is_block
            if not fillable:
                guest = None
                row = {**row, "total": None, "guests": None}
            if booking.guest_id is None and guest is not None:
                changes["guest"] = guest.pk
                booking.guest = guest
            if booking.total_price is None and row["total"] is not None:
                changes["total_price"] = row["total"]
                booking.total_price = row["total"]
            if booking.guests is None and row["guests"] is not None:
                changes["guests"] = row["guests"]
                booking.guests = row["guests"]
            if IMPORT_MARK not in booking.comment:
                addition = _comment(row)
                changes["comment_added"] = addition
                booking.comment = (booking.comment + "\n\n" if booking.comment else "") + addition
            booking.save()
            if fillable and item.paid and booking.total_price and not booking.payments.exists():
                add_payment(booking, {**row, "total": booking.total_price}, booking.channel)
            enriched_log.append({"booking": booking.pk, "changes": changes})
            enriched_count += 1

    batch.status = ImportBatch.Status.APPLIED
    batch.applied_at = timezone.now()
    batch.enriched = enriched_log
    batch.result = {
        "created": created,
        "enriched": enriched_count,
        "skipped": skipped,
        "unchanged": unchanged,
        "guests": len(new_guests),
        "channels": created_channels,
    }
    batch.save()
    return batch.result


@transaction.atomic
def revert_batch(batch: ImportBatch) -> dict:
    if batch.status != ImportBatch.Status.APPLIED:
        raise ImportFileError("Отменить можно только применённую загрузку")
    payments = batch.payments.count()
    batch.payments.all().delete()
    bookings = batch.bookings.count()
    for booking in batch.bookings.all():
        booking.payments.all().delete()
        booking.delete()
    for entry in batch.enriched:
        booking = Booking.objects.filter(pk=entry["booking"]).first()
        if booking is None:
            continue
        ch = entry["changes"]
        if "guest" in ch and booking.guest_id == ch["guest"]:
            booking.guest = None
        if "total_price" in ch and booking.total_price == ch["total_price"]:
            booking.total_price = None
        if "guests" in ch and booking.guests == ch["guests"]:
            booking.guests = None
        if "comment_added" in ch:
            booking.comment = booking.comment.replace("\n\n" + ch["comment_added"], "").replace(ch["comment_added"], "")
        booking.save()
    guests = 0
    for guest in batch.guests.all():
        if not guest.bookings.exists():
            guest.delete()
            guests += 1
    for pk in batch.result.get("channels", []):
        ch = Channel.objects.filter(pk=pk).first()
        if ch and not ch.bookings.exists() and not ch.import_url:
            ch.delete()
    batch.status = ImportBatch.Status.REVERTED
    batch.reverted_at = timezone.now()
    batch.save(update_fields=["status", "reverted_at"])
    return {"bookings": bookings, "payments": payments, "guests": guests}
