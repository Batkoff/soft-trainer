from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("trainer", "0017_evaluationrecheck")]

    operations = [
        migrations.AlterField(
            model_name="userprofile",
            name="role",
            field=models.CharField(
                choices=[
                    ("employee", "Сотрудник"),
                    ("group_leader", "Руководитель группы"),
                    ("sector_leader", "Руководитель сектора"),
                    ("unassigned", "Без роли"),
                ],
                default="employee",
                max_length=20,
                verbose_name="Роль",
            ),
        ),
    ]
