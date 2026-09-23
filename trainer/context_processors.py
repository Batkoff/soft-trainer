from .evaluation_profiles import current_profile, profile_has_key
from .people import display_name, user_role, ROLE_CHOICES, is_manager
from .releases import VERSION

def evaluator_context(request):
    profile = current_profile()
    user = request.user
    return {"app_version": VERSION, "team_access": user.is_authenticated and is_manager(user),
            "current_display_name": display_name(user) if user.is_authenticated else "",
            "current_avatar": getattr(getattr(user, "profile", None), "avatar", "🙂") if user.is_authenticated else "🙂",
            "current_role": dict(ROLE_CHOICES).get(user_role(user)) if user.is_authenticated else "",
            "grading_is_demo": profile["provider"] == "demo",
            "evaluator_profile": profile, "evaluator_key_present": profile_has_key(profile)}
