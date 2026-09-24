from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("trainer", "0012_userprofile_manager_userprofile_role")]
    operations = [migrations.AddField(
        model_name="userprofile", name="unit_name",
        field=models.CharField("Название группы или сектора", max_length=120, blank=True),
    )]
