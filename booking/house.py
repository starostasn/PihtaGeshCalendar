"""Страница «О доме» и фотографии дома."""

import uuid
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Max
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import House, HousePhoto
from .permissions import admin_required

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_PHOTOS = 40
FULL_SIZE = 2000
THUMB_SIZE = 560
ALLOWED_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}


def media_root() -> Path:
    root = Path(settings.DATA_DIR) / "media"
    (root / "house").mkdir(parents=True, exist_ok=True)
    return root


class PhotoError(Exception):
    pass


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        return background
    return img.convert("RGB") if img.mode != "RGB" else img


def save_photo(upload) -> HousePhoto:
    """Проверяет файл, сохраняет уменьшенную копию и превью в JPEG без метаданных (геометка удаляется)."""
    if upload.size > MAX_UPLOAD_BYTES:
        raise PhotoError(f"«{upload.name}»: файл больше 15 МБ")
    try:
        with Image.open(upload) as probe:
            fmt = probe.format
            probe.verify()
        upload.seek(0)
        img = Image.open(upload)
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise PhotoError(f"«{upload.name}»: это не фотография или файл повреждён") from exc
    if fmt not in ALLOWED_FORMATS:
        raise PhotoError(f"«{upload.name}»: формат {fmt} не поддерживается — пришлите JPEG, PNG или WebP")

    icc = img.info.get("icc_profile")  # цветовой профиль сохраняем, остальные метаданные — нет
    img = _to_rgb(ImageOps.exif_transpose(img))
    name = uuid.uuid4().hex
    root = media_root()
    full_rel, thumb_rel = f"house/{name}.jpg", f"house/{name}_t.jpg"

    full = img.copy()
    full.thumbnail((FULL_SIZE, FULL_SIZE), Image.Resampling.LANCZOS)
    full.save(root / full_rel, "JPEG", quality=85, optimize=True, progressive=True, icc_profile=icc)
    thumb = img.copy()
    thumb.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)
    thumb.save(root / thumb_rel, "JPEG", quality=80, optimize=True, progressive=True, icc_profile=icc)

    last = HousePhoto.objects.aggregate(m=Max("position"))["m"] or 0
    return HousePhoto.objects.create(
        image=full_rel, thumb=thumb_rel, width=full.width, height=full.height, position=last + 1
    )


def delete_photo_files(photo: HousePhoto) -> None:
    root = media_root()
    for rel in (photo.image, photo.thumb):
        path = (root / rel).resolve()
        if path.is_file() and root.resolve() in path.parents:
            path.unlink()


# ---------------------------------------------------------------- страницы


@login_required
def house_view(request):
    house = House.get()
    photos = list(HousePhoto.objects.all())
    cover = next((p for p in photos if p.is_cover), photos[0] if photos else None)
    sections = {
        "layout": House.lines(house.layout),
        "amenities": House.lines(house.amenities),
        "location": House.lines(house.location_info),
        "rules": House.lines(house.rules),
        "services": House.lines(house.extra_services),
        "team_contacts": House.lines(house.team_contacts),
    }
    photo_list = [
        {"id": p.pk, "src": reverse("photo_file", args=[p.pk, "full"]), "caption": p.caption} for p in photos
    ]
    return render(
        request,
        "booking/house.html",
        {
            "house": house,
            "photos": photos,
            "cover": cover,
            "s": sections,
            "max_photos": MAX_PHOTOS,
            "photo_list": photo_list,
        },
    )


class HouseInfoForm(forms.ModelForm):
    class Meta:
        model = House
        fields = [
            "name", "tagline", "description", "address", "avito_url",
            "area_m2", "plot_sotok", "floors", "rooms", "max_guests",
            "check_in_time", "check_out_time",
            "layout", "amenities", "location_info", "rules",
            "deposit", "prepayment_percent", "cancellation_policy", "extra_services",
            "checkin_instructions", "wifi_name", "wifi_password", "lockbox_code", "team_contacts",
            "admin_notes",
        ]
        widgets = {
            "check_in_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "check_out_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            **{f: forms.Textarea(attrs={"rows": 5}) for f in (
                "description", "layout", "amenities", "location_info", "rules",
                "cancellation_policy", "extra_services", "checkin_instructions", "team_contacts", "admin_notes",
            )},
        }


@admin_required
def house_edit(request):
    house = House.get()
    form = HouseInfoForm(request.POST or None, instance=house)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Сведения о доме сохранены")
        return redirect("house")
    return render(request, "booking/house_edit.html", {"form": form})


@admin_required
@require_POST
def photo_upload(request):
    files = request.FILES.getlist("photos")
    if not files:
        messages.error(request, "Выберите фотографии")
        return redirect("house")
    room = MAX_PHOTOS - HousePhoto.objects.count()
    saved, errors = 0, []
    for upload in files[: max(room, 0)]:
        try:
            photo = save_photo(upload)
            photo.uploaded_by = request.user
            photo.save(update_fields=["uploaded_by"])
            saved += 1
        except PhotoError as exc:
            errors.append(str(exc))
    if len(files) > room:
        errors.append(f"Можно хранить до {MAX_PHOTOS} фото — лишние не загружены")
    if saved:
        if not HousePhoto.objects.filter(is_cover=True).exists():
            HousePhoto.objects.filter(pk=HousePhoto.objects.first().pk).update(is_cover=True)
        messages.success(request, f"Загружено фото: {saved}")
    for err in errors:
        messages.error(request, err)
    return redirect("/house/#photos")


@admin_required
@require_POST
def photo_update(request, pk: int):
    photo = get_object_or_404(HousePhoto, pk=pk)
    action = request.POST.get("action", "")
    with transaction.atomic():
        if action == "caption":
            photo.caption = request.POST.get("caption", "").strip()[:200]
            photo.save(update_fields=["caption"])
        elif action == "cover":
            HousePhoto.objects.update(is_cover=False)
            HousePhoto.objects.filter(pk=pk).update(is_cover=True)
        elif action in ("up", "down"):
            ordered = list(HousePhoto.objects.all())
            i = next(n for n, p in enumerate(ordered) if p.pk == pk)
            j = i - 1 if action == "up" else i + 1
            if 0 <= j < len(ordered):
                ordered[i], ordered[j] = ordered[j], ordered[i]
                for n, p in enumerate(ordered, start=1):
                    if p.position != n:
                        HousePhoto.objects.filter(pk=p.pk).update(position=n)
        elif action == "delete":
            was_cover = photo.is_cover
            delete_photo_files(photo)
            photo.delete()
            first = HousePhoto.objects.first()
            if was_cover and first:
                HousePhoto.objects.filter(pk=first.pk).update(is_cover=True)
            messages.success(request, "Фото удалено")
    return redirect("/house/#photos")


@login_required
def photo_file(request, pk: int, size: str):
    photo = get_object_or_404(HousePhoto, pk=pk)
    rel = photo.thumb if size == "thumb" else photo.image
    path = (media_root() / rel).resolve()
    if not path.is_file():
        raise Http404()
    resp = FileResponse(open(path, "rb"), content_type="image/jpeg")
    resp["Cache-Control"] = "private, max-age=86400"
    return resp
