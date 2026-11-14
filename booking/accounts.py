"""Пользователи: список и роли, приглашения по ссылке, личный профиль, защита входа от перебора."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import InviteAcceptForm, InviteForm, ProfileForm, UserCreateForm, UserEditForm
from .models import Invite
from .permissions import ROLE_HELP, admin_required, display_name, set_role

User = get_user_model()


def invite_url(invite: Invite) -> str:
    return settings.PUBLIC_BASE_URL.rstrip("/") + reverse("invite_accept", args=[invite.token])


# ---------------------------------------------------------------- пользователи (только администратор)


@admin_required
def users_view(request):
    users = User.objects.order_by("-is_active", "-is_superuser", "first_name", "username")
    invites = list(Invite.objects.select_related("created_by", "used_by")[:30])
    for inv in invites:
        inv.url = invite_url(inv)
    active_invites = [i for i in invites if i.is_valid]
    other_invites = [i for i in invites if not i.is_valid]
    return render(
        request,
        "booking/users.html",
        {
            "users": users,
            "active_invites": active_invites,
            "other_invites": other_invites,
            "invite_form": InviteForm(),
            "role_help": ROLE_HELP,
        },
    )


@admin_required
def user_create(request):
    form = UserCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        messages.success(request, f"Пользователь «{display_name(user)}» создан. Передайте ему логин и пароль.")
        return redirect("users")
    return render(request, "booking/user_form.html", {"form": form, "role_help": ROLE_HELP, "is_new": True})


@admin_required
def user_edit(request, pk: int):
    target = get_object_or_404(User, pk=pk)
    editing_self = target.pk == request.user.pk
    form = UserEditForm(instance=target, editing_self=editing_self)
    password_form = SetPasswordForm(target)

    if request.method == "POST":
        if "set_password" in request.POST:
            password_form = SetPasswordForm(target, request.POST)
            if password_form.is_valid():
                password_form.save()
                if editing_self:
                    update_session_auth_hash(request, target)
                messages.success(request, f"Пароль для «{display_name(target)}» изменён.")
                return redirect("users")
        else:
            form = UserEditForm(request.POST, instance=target, editing_self=editing_self)
            if form.is_valid():
                form.save()
                messages.success(request, f"Изменения для «{display_name(target)}» сохранены.")
                return redirect("users")

    return render(
        request,
        "booking/user_form.html",
        {
            "form": form,
            "password_form": password_form,
            "target": target,
            "editing_self": editing_self,
            "role_help": ROLE_HELP,
            "is_new": False,
        },
    )


@admin_required
@require_POST
def invite_create(request):
    form = InviteForm(request.POST)
    if form.is_valid():
        invite = form.save(commit=False)
        invite.created_by = request.user
        invite.save()
        messages.success(request, "Приглашение создано. Скопируйте ссылку и отправьте её человеку — она действует 7 дней.")
    else:
        messages.error(request, "Не удалось создать приглашение: проверьте поля.")
    return redirect(f"{reverse('users')}#invites")


@admin_required
@require_POST
def invite_revoke(request, pk: int):
    invite = get_object_or_404(Invite, pk=pk)
    if invite.is_valid:
        invite.revoked_at = timezone.now()
        invite.save(update_fields=["revoked_at"])
        messages.success(request, "Приглашение отозвано — ссылка больше не работает.")
    return redirect(f"{reverse('users')}#invites")


# ---------------------------------------------------------------- приглашение (открывается без входа)


def invite_accept(request, token: str):
    invite = Invite.objects.filter(token=token).first()
    if invite is None or not invite.is_valid:
        return render(request, "booking/invite_invalid.html", {"invite": invite}, status=410 if invite else 404)
    if request.user.is_authenticated:
        return render(request, "booking/invite_invalid.html", {"invite": invite, "logged_in": True})

    form = InviteAcceptForm(request.POST or None, initial={"first_name": invite.name.split(",")[0].strip()})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            invite = Invite.objects.get(pk=invite.pk)  # перечитываем внутри транзакции
            if not invite.is_valid:
                return render(request, "booking/invite_invalid.html", {"invite": invite}, status=410)
            user = form.save(commit=False)
            set_role(user, invite.role)
            user.save()
            invite.used_at = timezone.now()
            invite.used_by = user
            invite.save(update_fields=["used_at", "used_by"])
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, f"Добро пожаловать, {display_name(user)}! Вход выполнен.")
        return redirect("calendar")
    return render(request, "booking/invite_accept.html", {"form": form, "invite": invite})


# ---------------------------------------------------------------- профиль (любой пользователь)


@login_required
def account_view(request):
    profile_form = ProfileForm(instance=request.user)
    password_form = PasswordChangeForm(request.user)
    if request.method == "POST":
        if "change_password" in request.POST:
            password_form = PasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, "Пароль изменён.")
                return redirect("account")
        else:
            profile_form = ProfileForm(request.POST, instance=request.user)
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, "Имя сохранено.")
                return redirect("account")
    return render(request, "booking/account.html", {"profile_form": profile_form, "password_form": password_form})


# ---------------------------------------------------------------- вход с защитой от перебора паролей

MAX_ATTEMPTS_PER_LOGIN = 5  # неудачных попыток для одного логина с одного адреса
MAX_ATTEMPTS_PER_IP = 20  # всего неудачных попыток с одного адреса
LOCK_SECONDS = 15 * 60


def client_ip(request) -> str:
    # За Caddy настоящий адрес клиента приходит в X-Forwarded-For (последнее значение добавляет Caddy).
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")


class ThrottledLoginView(LoginView):
    template_name = "booking/login.html"
    redirect_authenticated_user = True

    def _keys(self):
        ip = client_ip(self.request)
        username = (self.request.POST.get("username") or "").strip().lower()
        return f"login-fail:{ip}:{username}", f"login-fail:{ip}"

    def post(self, request, *args, **kwargs):
        user_key, ip_key = self._keys()
        if cache.get(user_key, 0) >= MAX_ATTEMPTS_PER_LOGIN or cache.get(ip_key, 0) >= MAX_ATTEMPTS_PER_IP:
            form = self.get_form()
            form.errors.clear()
            form.add_error(None, "Слишком много неудачных попыток входа. Подождите 15 минут и попробуйте снова.")
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        for key in self._keys():
            try:
                cache.incr(key)
            except ValueError:
                cache.set(key, 1, LOCK_SECONDS)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete(self._keys()[0])
        return super().form_valid(form)
