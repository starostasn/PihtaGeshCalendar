from .models import House


def house(request):
    if not request.user.is_authenticated:
        return {}
    return {"house": House.get()}
