from io import BytesIO
from html.parser import HTMLParser
from PIL import Image
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from .models import UserProfile
from .user_admin import TeamChangeForm


def photo():
    stream = BytesIO()
    Image.new("RGB", (480, 320), "red").save(stream, "PNG")
    return SimpleUploadedFile("photo.png", stream.getvalue(), content_type="image/png")


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ProfilePhotoTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("owner", password="pass")
        self.employee = User.objects.create_user("employee", password="pass")

    def test_browser_hidden_fields_allow_existing_names_to_save(self):
        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.data = {}
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "input" and attrs.get("type") == "hidden" and attrs.get("name"):
                    self.data[attrs["name"]] = attrs.get("value", "")
        self.client.force_login(self.admin)
        for user, role in [(self.admin, "admin"), (self.employee, "employee")]:
            UserProfile.objects.get_or_create(user=user)
            page = self.client.get(f"/admin/auth/user/{user.pk}/change/")
            inputs = Inputs()
            inputs.feed(page.content.decode())
            data = {**inputs.data, "username": user.username, "first_name": "Даниил",
                "last_name": "Батьков", "role": role, "is_active": "on", "_save": "1"}
            response = self.client.post(f"/admin/auth/user/{user.pk}/change/", data)
            self.assertEqual(response.status_code, 302)
            user.refresh_from_db()
            self.assertEqual(user.get_full_name(), "Даниил Батьков")
            self.assertEqual(user.is_superuser, role == "admin")
        form = TeamChangeForm(data={"username": "employee", "role": "employee", "reports": [self.admin.pk]}, instance=self.employee)
        self.assertFalse(form.is_valid())
        self.assertIn("reports", form.errors)

    def test_upload_read_replace_and_remove_photo(self):
        self.client.force_login(self.employee)
        self.assertEqual(self.client.post("/profile/", {"display_name": "Фото", "photo": photo()}).status_code, 302)
        profile = UserProfile.objects.get(user=self.employee)
        image = Image.open(BytesIO(bytes(profile.avatar_data)))
        self.assertEqual((image.format, image.size), ("JPEG", (256, 256)))
        url = f"/avatars/{self.employee.pk}/"
        response = self.client.get(url)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(self.client.get(url, HTTP_IF_NONE_MATCH=response["ETag"]).status_code, 304)
        self.assertContains(self.client.get("/profile/"), url)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.employee)
        self.assertEqual(self.client.post("/profile/", {"display_name": "Фото", "remove_photo": "on"}).status_code, 302)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_invalid_upload_does_not_overwrite_photo_or_nickname(self):
        self.client.force_login(self.employee)
        self.client.post("/profile/", {"display_name": "До", "photo": photo()})
        original = bytes(UserProfile.objects.get(user=self.employee).avatar_data)
        for bad in [b"<svg></svg>", b"x" * (5 * 1024 * 1024 + 1)]:
            response = self.client.post("/profile/", {"display_name": "После", "photo": SimpleUploadedFile("bad.png", bad)})
            self.assertEqual(response.status_code, 200)
            self.assertIn("photo", response.context["form"].errors)
            profile = UserProfile.objects.get(user=self.employee)
            self.assertEqual(profile.display_name, "До")
            self.assertEqual(bytes(profile.avatar_data), original)

    def test_prompt_migration_updates_existing_settings_and_preserves_snapshot(self):
        from importlib import import_module
        from types import SimpleNamespace
        from django.apps import apps
        from django.db import connection
        from .models import EvaluationPrompt
        from .evaluation_prompt import SYSTEM_PROMPT
        from .evaluation_profiles import current_profile
        from .evaluation_http import LiveEvaluator
        migration = import_module("trainer.migrations.0016_profile_photos_and_prompt")
        EvaluationPrompt.objects.update_or_create(pk=1, defaults={"text": "Прежние правила", "version": 9})
        with override_settings(ALLOW_TEST_EVALUATOR=False):
            snapshot = current_profile()
            migration.install_prompt(apps, SimpleNamespace(connection=connection))
            self.assertEqual(current_profile()["prompt_text"], SYSTEM_PROMPT)
            self.assertEqual(current_profile()["prompt_version"], "soft-v10")
            self.assertEqual(LiveEvaluator(snapshot).system_prompt, "Прежние правила")
            migration.install_prompt(apps, SimpleNamespace(connection=connection))
            self.assertEqual(EvaluationPrompt.objects.get(pk=1).version, 10)
        self.assertEqual(migration.NEW_PROMPT, SYSTEM_PROMPT)
