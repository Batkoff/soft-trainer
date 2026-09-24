"""Фото нормализуется при загрузке: только небольшой JPEG без исходных метаданных."""
from io import BytesIO
import warnings
from django import forms
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, Http404
from django.views.decorators.http import require_GET
from PIL import Image, ImageOps, UnidentifiedImageError
from .models import UserProfile


def normalize_photo(upload):
    if upload.size > 5 * 1024 * 1024:
        raise forms.ValidationError("Фото должно быть не больше 5 МБ.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(upload) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"}:
                    raise forms.ValidationError("Используйте JPG, PNG или WebP.")
                if source.width * source.height > 20_000_000:
                    raise forms.ValidationError("Слишком большое разрешение: максимум 20 мегапикселей.")
                image = ImageOps.exif_transpose(source).convert("RGB")
                image = ImageOps.fit(image, (256, 256), method=Image.Resampling.LANCZOS)
                output = BytesIO()
                image.save(output, "JPEG", quality=85, optimize=True)
                return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise forms.ValidationError("Не удалось прочитать фото. Загрузите исправный JPG, PNG или WebP.")


@login_required
@require_GET
def avatar(request, user_id):
    # Коллеги видят фото в общем рейтинге; остальные данные профиля здесь не выдаются.
    profile = UserProfile.objects.filter(user_id=user_id).only("avatar_data", "avatar_version").first()
    if not profile or not profile.avatar_data:
        raise Http404
    etag = f'"{profile.avatar_version}"'
    response = HttpResponse(status=304) if request.headers.get("If-None-Match") == etag else HttpResponse(bytes(profile.avatar_data), content_type="image/jpeg")
    response["ETag"] = etag
    response["Cache-Control"] = "private, no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    return response
