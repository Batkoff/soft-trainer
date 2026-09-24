from django.conf import settings
from django.db import migrations


def create_missing_profiles(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Profile = apps.get_model("trainer", "UserProfile")
    alias = schema_editor.connection.alias
    existing = set(Profile.objects.using(alias).values_list("user_id", flat=True))
    missing = [
        Profile(user_id=user_id, role="employee")
        for user_id in User.objects.using(alias).filter(is_superuser=False).exclude(pk__in=existing).values_list("pk", flat=True)
    ]
    if missing:
        Profile.objects.using(alias).bulk_create(missing)


class Migration(migrations.Migration):
    dependencies = [("trainer", "0014_aiconfiguration_proxy")]

    operations = [
        migrations.RunPython(create_missing_profiles, migrations.RunPython.noop),
    ]
