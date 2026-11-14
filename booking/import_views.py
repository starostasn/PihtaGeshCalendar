"""Страницы загрузки старых броней (только администратор)."""

from datetime import timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .importer import (
    ImportFileError,
    apply_batch,
    build_plan,
    default_mapping,
    mapping_choices,
    parse_file,
    plan_summary,
    revert_batch,
)
from .models import ImportBatch
from .permissions import admin_required


@admin_required
def import_view(request):
    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "Выберите файл")
            return redirect("import")
        try:
            rows = parse_file(upload.read())
        except ImportFileError as exc:
            messages.error(request, str(exc))
            return redirect("import")
        # старые черновики больше не нужны
        ImportBatch.objects.filter(status=ImportBatch.Status.DRAFT, created_at__lt=timezone.now() - timedelta(days=1)).delete()
        sources = sorted({r["source"] for r in rows})
        batch = ImportBatch.objects.create(
            filename=upload.name[:255],
            rows=rows,
            options={"mapping": default_mapping(sources), "mark_paid": True, "zero_as_block": True},
            created_by=request.user,
        )
        return redirect("import_preview", pk=batch.pk)
    batches = ImportBatch.objects.exclude(status=ImportBatch.Status.DRAFT).select_related("created_by")[:20]
    return render(request, "booking/import.html", {"batches": batches})


@admin_required
def import_preview(request, pk: int):
    batch = get_object_or_404(ImportBatch, pk=pk)
    sources = sorted({r["source"] for r in batch.rows})

    if request.method == "POST" and batch.status == ImportBatch.Status.DRAFT:
        valid = {value for value, _ in mapping_choices(sources)}
        mapping = {}
        for i, source in enumerate(sources):
            value = request.POST.get(f"source_{i}", "")
            mapping[source] = value if value in valid else ""
        batch.options = {
            "mapping": mapping,
            "mark_paid": request.POST.get("mark_paid") == "on",
            "zero_as_block": request.POST.get("zero_as_block") == "on",
        }
        batch.save(update_fields=["options"])
        if request.POST.get("action") == "apply":
            try:
                result = apply_batch(batch, request.user)
            except ImportFileError as exc:
                messages.error(request, str(exc))
                return redirect("import_preview", pk=pk)
            messages.success(
                request,
                f"Загружено: создано броней {result['created']}, дополнено {result['enriched']}, "
                f"без изменений {result['unchanged']}, пропущено {result['skipped']}, новых гостей {result['guests']}.",
            )
        return redirect("import_preview", pk=pk)

    context = {"batch": batch}
    if batch.status == ImportBatch.Status.DRAFT:
        plan = build_plan(batch)
        choices = mapping_choices(sources)
        context.update(
            plan=plan,
            summary=plan_summary(plan),
            sources=[
                {"index": i, "raw": s, "value": batch.options["mapping"].get(s, ""), "count": sum(r["source"] == s for r in batch.rows)}
                for i, s in enumerate(sources)
            ],
            choices=choices,
        )
    return render(request, "booking/import_preview.html", context)


@admin_required
@require_POST
def import_revert(request, pk: int):
    batch = get_object_or_404(ImportBatch, pk=pk)
    try:
        result = revert_batch(batch)
    except ImportFileError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Загрузка отменена: удалено броней {result['bookings']}, платежей {result['payments']}, "
            f"гостей {result['guests']}; дополнения к существующим броням убраны.",
        )
    return redirect("import_preview", pk=pk)
