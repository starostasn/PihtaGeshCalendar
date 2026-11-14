"""Роли пользователей календаря.

Роль хранится в стандартном признаке Django is_superuser:
- администратор (is_superuser=True) — всё, включая цены, площадки и пользователей;
- менеджер (is_superuser=False) — брони, платежи, статистика; цены только просмотр.
"""

from functools import wraps

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_CHOICES = [
    (ROLE_ADMIN, "Администратор"),
    (ROLE_MANAGER, "Менеджер"),
]
ROLE_HELP = {
    ROLE_ADMIN: "Всё: брони, платежи, цены, площадки, статистика и пользователи.",
    ROLE_MANAGER: "Брони, платежи и статистика. Цены — только просмотр; площадки и пользователи недоступны.",
}


def is_admin(user) -> bool:
    return bool(user and user.is_authenticated and user.is_active and user.is_superuser)


def role_of(user) -> str:
    return ROLE_ADMIN if user.is_superuser else ROLE_MANAGER


def role_label(user) -> str:
    return dict(ROLE_CHOICES)[role_of(user)]


def set_role(user, role: str) -> None:
    """Администратор получает и доступ к служебной админке Django (/admin/), если она включена."""
    user.is_superuser = user.is_staff = role == ROLE_ADMIN


def display_name(user) -> str:
    if user is None:
        return ""
    return user.get_full_name() or user.username


def active_admins():
    return get_user_model().objects.filter(is_active=True, is_superuser=True)


def forbidden(request, text: str = "Этот раздел доступен только администраторам."):
    return render(request, "booking/forbidden.html", {"text": text}, status=403)


def admin_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not is_admin(request.user):
            return forbidden(request)
        return view(request, *args, **kwargs)

    return wrapper
