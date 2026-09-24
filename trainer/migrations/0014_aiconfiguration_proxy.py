from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("trainer", "0013_userprofile_unit_name")]

    operations = [
        migrations.AddField(
            model_name="aiconfiguration",
            name="proxy_enabled",
            field=models.BooleanField(default=False, verbose_name="Использовать прокси"),
        ),
        migrations.AddField(
            model_name="aiconfiguration",
            name="proxy_url",
            field=models.CharField(blank=True, max_length=500, verbose_name="Адрес HTTP(S)-прокси"),
        ),
        migrations.AddField(
            model_name="aiconfiguration",
            name="proxy_username",
            field=models.CharField(blank=True, max_length=255, verbose_name="Логин прокси"),
        ),
        migrations.AddField(
            model_name="aiconfiguration",
            name="proxy_secret",
            field=models.TextField(blank=True, verbose_name="Пароль прокси"),
        ),
    ]
