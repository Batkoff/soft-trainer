from importlib import import_module

from django.db import migrations, models


def update_default_prompt(apps, schema_editor):
    prompt_model = apps.get_model("trainer", "EvaluationPrompt")
    current = prompt_model.objects.filter(pk=1).first()
    if current is None:
        from trainer.evaluation_prompt import SYSTEM_PROMPT
        prompt_model.objects.create(pk=1, version=7, text=SYSTEM_PROMPT)
        return

    previous_prompt = import_module(
        "trainer.migrations.0019_install_attached_evaluation_prompt"
    ).PROMPT
    if current.version <= 6 and current.text == previous_prompt:
        from trainer.evaluation_prompt import SYSTEM_PROMPT
        current.version = 7
        current.text = SYSTEM_PROMPT
        current.save(update_fields=["version", "text", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("trainer", "0019_install_attached_evaluation_prompt")]

    operations = [
        migrations.RunPython(update_default_prompt, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="evaluationprompt",
            name="version",
            field=models.PositiveSmallIntegerField(default=7, editable=False, verbose_name="Версия"),
        ),
    ]
