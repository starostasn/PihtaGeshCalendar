from .models import House
from .permissions import display_name, is_admin, role_label


def house(request):
    if not request.user.is_authenticated:
        return {}
    return {
        "house": House.get(),
        "is_admin": is_admin(request.user),
        "me_name": display_name(request.user),
        "me_role": role_label(request.user),
    }
