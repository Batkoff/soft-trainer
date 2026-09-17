"""Локальная настройка API. Не отправляет запросы, не печатает и не журналирует ключ."""
import getpass
import os
import re
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS = {"1": "openai", "2": "openrouter", "3": "demo"}

def supports_hidden_input():
    """Windows getpass читает Win32-консоль. Канал Git Bash может быть обычной pipe."""
    if os.name != "nt":
        return True  # На Unix сам getpass проверяет терминал и выдаёт GetPassWarning.
    import ctypes
    import msvcrt
    from ctypes import wintypes
    get_mode = ctypes.windll.kernel32.GetConsoleMode
    get_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_mode.restype = wintypes.BOOL
    try:
        for stream in (sys.stdin, sys.stdout):
            handle = msvcrt.get_osfhandle(stream.fileno())
            mode = wintypes.DWORD()
            if not get_mode(handle, ctypes.byref(mode)):
                return False
    except (AttributeError, OSError, ValueError):
        return False
    return True

def manual_setup_message(provider):
    key_name = "OPENAI_API_KEY" if provider == "openai" else "OPENROUTER_API_KEY"
    return ("В этом терминале скрытый ввод ключа недоступен. Настройки не изменены.\n"
            "Запустите мастер из PowerShell или выполните: notepad .env\n"
            "В .env замените существующие строки (если их нет — добавьте):\n"
            f"EVALUATOR_BACKEND={provider}\nEVALUATOR_MODEL=\n{key_name}=ваш-API-ключ\n"
            "Сохраните файл и выполните: docker compose up --build -d")

def main():
    path = ROOT / ".env"
    if not path.exists():
        raise SystemExit("Сначала выполните python scripts/prepare_demo.py")
    print("Оценщик: 1 — OpenAI, 2 — OpenRouter, 3 — демонстрационный")
    provider = PROVIDERS.get(input("Выберите 1, 2 или 3: ").strip())
    if not provider:
        raise SystemExit("Настройки не изменены: неизвестный вариант.")
    updates = {"EVALUATOR_BACKEND": provider, "EVALUATOR_MODEL": ""}
    if provider != "demo":
        if not supports_hidden_input():
            # Проверка ДО getpass: иначе Windows Python может ждать невидимую консоль.
            raise SystemExit(manual_setup_message(provider))
        print(f"Выбран {provider}. Вставьте API-ключ и нажмите Enter. Символы и звёздочки не отображаются.", flush=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("").strip()
        except getpass.GetPassWarning:
            raise SystemExit(manual_setup_message(provider))
        # Простое значение dotenv без интерполяции, переносов строк и управляющих символов.
        if not re.fullmatch(r"[A-Za-z0-9_.-]{16,512}", key):
            raise SystemExit("Ключ имеет неподходящий формат. Настройки не изменены.")
        updates["OPENAI_API_KEY" if provider == "openai" else "OPENROUTER_API_KEY"] = key
    lines, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.split("=", 1)[0].strip()
        if name in updates:
            if name not in seen:
                lines.append(f"{name}={updates[name]}")
            seen.add(name)
        else:
            lines.append(line)
    lines.extend(f"{name}={value}" for name, value in updates.items() if name not in seen)
    # Временный файл с теми же приватными правами; замена атомарна на одном диске.
    temporary = path.with_name(".env.ai-tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines)+"\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Сохранён оценщик: {provider}. Ключ не проверялся сетевым запросом.")
    print("Теперь: docker compose up --build -d")
    print("Затем: Песочница → Реальная оценка нейросетью. Старый демо-конкурс остаётся тестовым.")

if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nНастройка прервана.")
