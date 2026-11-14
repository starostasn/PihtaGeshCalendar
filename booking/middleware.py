"""Заголовки безопасности, которых нет в стандартном Django 5.2."""

from django.conf import settings


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if settings.CONTENT_SECURITY_POLICY:
            response.headers.setdefault("Content-Security-Policy", settings.CONTENT_SECURITY_POLICY)
        if settings.PERMISSIONS_POLICY:
            response.headers.setdefault("Permissions-Policy", settings.PERMISSIONS_POLICY)
        return response
