"""Исправляет только исходное имя учебного аккаунта, сохраняя пользовательские правки."""
from django.conf import settings
from django.db import migrations


def correct_name(apps, schema_editor):
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)
    User.objects.using(schema_editor.connection.alias).filter(
        username="demo1", first_name="Даниил", last_name="Батков"
    ).update(last_name="Батьков")


class Migration(migrations.Migration):
    dependencies = [
        ("trainer", "0007_alter_calibrationcase_options_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.RunPython(correct_name, migrations.RunPython.noop)]
