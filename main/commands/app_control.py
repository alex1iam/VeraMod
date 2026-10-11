import os
import re
import subprocess
import ctypes
from pathlib import Path
from typing import NamedTuple, Optional
import psutil

from main.config_manager import get_config
from main.app_indexer import load_app_index, build_app_index
from main.lang_ru import ru_to_en
from main.utils.fuzzy import fuzzy_match

# Индекс приложений может требовать длительного сканирования файловой системы и
# записи в пользовательский каталог. Загружаем его только по первой команде,
# которой действительно нужен поиск приложения.
APP_INDEX: Optional[list[dict[str, str]]] = None


# Глобальные переменные
_config = get_config()
COMMANDS_CFG = _config.get("commands", default={})


def _current_username() -> str:
    """Возвращает текущее имя пользователя."""
    return os.environ.get("USERNAME") or os.environ.get("USER") or Path.home().name


def _expand_config_placeholders(s: str) -> str:
    """Раскрывает плейсхолдеры в конфигурации."""
    return s.replace("${USER}", _current_username())


def _get_app_index() -> list[dict[str, str]]:
    global APP_INDEX
    if APP_INDEX is None:
        try:
            APP_INDEX = load_app_index()
        except Exception as e:
            print(f"[APP_INDEX] Ошибка: {e}")
            APP_INDEX = []
    return APP_INDEX


def kill_process(name: str) -> bool:
    """Завершает процесс по имени."""
    found = False
    for proc in psutil.process_iter(['name']):
        try:
            if name.lower() in (proc.info.get('name') or '').lower():
                proc.kill()
                found = True
        except Exception:
            pass
    return found


def _shell_execute(path: str, params: Optional[str] = None, runas: bool = False) -> bool:
    """Выполняет команду через ShellExecute."""
    try:
        verb = "runas" if runas else "open"
        rc = ctypes.windll.shell32.ShellExecuteW(None, verb, path, params, None, 1)
        return rc > 32
    except Exception:
        return False


def execute_predefined_command(text: str) -> Optional[str]:
    """Выполняет предопределённые команды из config.json."""
    lowered = text.lower()
    
    for key, meta in COMMANDS_CFG.items():
        if key not in lowered:
            continue
        
        if 'запусти' in lowered or 'открой' in lowered:
            cmd = meta.get('open')
            if not cmd:
                continue
            try:
                if isinstance(cmd, (list, tuple)):
                    subprocess.Popen(list(cmd))
                    return f"Запускаю {key}."
                
                s = _expand_config_placeholders(str(cmd))
                # Поиск исполняемого файла
                for ext in (".exe", ".bat", ".cmd", ".com", ".ps1"):
                    idx = s.lower().find(ext)
                    if idx != -1:
                        exe_path = s[:idx + len(ext)].strip().strip('"')
                        args = s[idx + len(ext):].strip().split() if s[idx + len(ext):].strip() else []
                        subprocess.Popen([exe_path, *args])
                        return f"Запускаю {key}."
                return "Ошибка: команда не содержит исполняемый файл."
            except Exception as e:
                return f"Ошибка запуска {key}: {e}"
        
        elif 'закрой' in lowered or 'выключи' in lowered:
            process_name = meta.get('close')
            if not process_name:
                continue
            success = kill_process(process_name)
            return f"Закрываю {key}." if success else f"{key.capitalize()} не запущен."
    
    return None


def _best_app_match(query: str) -> Optional[dict]:
    """ Нечёткий поиск приложения с учётом транслитерации."""
    q = re.sub("[^a-z\\u0430-\\u044f\\u04510-9]+", "", query.lower())
    q_en = ru_to_en(q)
    best_item, best_score = None, 0.0
    
    for item in _get_app_index():
        for cand in [item.get("display_name", "").lower(), 
                     item.get("exe_name", "").lower(),
                     Path(item.get("lnk_path", "")).stem.lower()]:
            if not cand:
                continue
            
            c_norm = re.sub("[^a-z\\u0430-\\u044f\\u04510-9]+", "", cand)
            # Используем fuzzy_match для обоих вариантов (оригинал + транслит)
            score = max(
                fuzzy_match(q, c_norm, boost_substring=False),
                fuzzy_match(q_en, c_norm, boost_substring=False)
            )
            
            # Дополнительный бонус за вхождение подстроки
            if (q in c_norm) or (q_en in c_norm):
                score += 0.2
            
            if score > best_score:
                best_score = score
                best_item = item
    
    return best_item if best_score >= 0.55 else None


def execute_app_command(text: str) -> Optional[str]:
    """Универсальный запуск/закрытие приложений через индекс."""
    lowered = text.lower().strip()
    
    # Исключаем системные команды (компьютер - это power_command)
    if re.search(r"\bкомпьютер\b", lowered):
        return None
    
    # Быстрые команды для командной строки (cmd)
    if re.search(r"\b(открой|запусти)\b.*\b(cmd|командн\w*\s*строк\w*|ком\s*строк\w*|консол[ьи]|терминал)\b", lowered):
        os.startfile("cmd.exe")
        return "Открываю командную строку."
    
    # Определение действия
    if re.search(r"\b(закрой|выключи)\b", lowered):
        action = "close"
    elif re.search(r"\b(открой|запусти)\b", lowered):
        action = "open"
    else:
        return None
    
    # Извлечение цели
    m = re.search(r"(?:открой|запусти|закрой|выключи)\s+(.+)", lowered)
    if not m or re.search(r"\bисточник\w*\b", m.group(1)):
        return None
    
    target_name = m.group(1)
    cand = _best_app_match(target_name)
    
    if not cand:
        return None
    
    print(f"[OPEN_APP] Найдено: {cand.get('display_name')}")
    
    if action == "open":
        return _open_app(cand)
    else:
        return _close_app(cand)


def _open_app(app: dict) -> str:
    exe_path = app.get("exe_path", "")
    exe_name = app.get("exe_name", "").strip()
    lnk_path = app.get("lnk_path", "")
    
    try:
        # Приоритет: lnk -> exe -> имя
        if lnk_path and os.path.isfile(lnk_path):
            os.startfile(lnk_path)
        elif exe_path and os.path.isfile(exe_path):
            try:
                os.startfile(exe_path)
            except OSError as e:
                if getattr(e, "winerror", 0) == 740:  # Требуются права администратора
                    if not _shell_execute(exe_path, runas=True):
                        raise
                else:
                    raise
        elif exe_name:
            if not _shell_execute(exe_name) and not _shell_execute(exe_name, runas=True):
                raise OSError("ShellExecute не удалось запустить процесс")
        else:
            return "Не удалось запустить: недостаточно данных."
        
        return f"Запускаю {app.get('display_name') or exe_name or 'приложение'}."
    except Exception as e:
        return f"Ошибка запуска: {e}"


def _close_app(app: dict) -> str:
    candidates = []
    
    if exe_name := app.get("exe_name", "").strip():
        candidates.extend([exe_name, Path(exe_name).stem])
    
    if disp := app.get("display_name", "").strip():
        candidates.append(disp)
    
    # Добавляем имя родительской папки (кроме системных)
    for path in [app.get("exe_path"), app.get("lnk_path")]:
        if path:
            parent = Path(path).parent.name
            if parent and parent.lower() not in {
                "system32", "windows", "program files", 
                "program files (x86)", "microsoft", "microsoft office"
            }:
                candidates.append(parent)
    
    # Уникализация
    seen = set()
    unique = []
    for c in candidates:
        c_low = str(c).strip().lower()
        if c_low and len(c_low) >= 2 and c_low not in seen:
            seen.add(c_low)
            unique.append(c)
    
    # Попытка завершить процесс
    any_killed = any(kill_process(c) for c in unique)
    return "Закрываю приложение." if any_killed else "Приложение не найдено среди процессов."


def execute_browser_command(text: str) -> Optional[str]:
    """Открывает браузер по умолчанию."""
    lowered = text.lower().strip()
    
    # Проверяем команду открытия браузера
    if not re.search(r"\b(открой|запусти)\b", lowered):
        return None
    
    if not re.search(r"\bбраузер\w*\b", lowered):
        return None
    
    try:
        # Получаем браузер по умолчанию из реестра Windows
        import winreg
        try:
            # Пытаемся получить путь к браузеру из реестра
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 
                               r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice") as key:
                prog_id = winreg.QueryValueEx(key, "ProgId")[0]
            
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, 
                               rf"{prog_id}\shell\open\command") as key:
                command = winreg.QueryValueEx(key, "")[0]
            
            # Извлекаем путь к exe из команды
            if command:
                # Убираем параметры командной строки
                exe_path = command.split('"')[1] if '"' in command else command.split()[0]
                subprocess.Popen([exe_path])
                return "Открываю браузер по умолчанию."
        except Exception:
            # Если не удалось через реестр, используем простой способ
            os.startfile("http://")  # type: ignore
            return "Открываю браузер по умолчанию."
    except Exception as e:
        return f"Ошибка открытия браузера: {e}"


class MailLaunch(NamedTuple):
    """Как запустить почтовый клиент: argv программы или протокол Windows."""

    argv: tuple = ()
    protocol: str = ""


_MAIL_OPEN_RE = re.compile(
    r"\b(?:открой(?:те)?|запусти(?:те)?|открыть|запустить)\b",
    re.IGNORECASE,
)
_MAIL_TARGET_RE = re.compile(
    r"\b(?:почт(?:а|у|е|ы|ой|ою|ов\w*)|e-?mail|mail|мейл|е-?мейл|имейл)\b",
    re.IGNORECASE,
)
# Файл, папка и адрес сайта — не запуск клиента.
_MAIL_NOT_CLIENT_RE = re.compile(
    r"(https?://|www\.|\b[\w-]+\.(?:ru|com|net|org|io|рф)\b|\b(?:файл|папк)\w*\b)",
    re.IGNORECASE,
)
_COMPOSE_ARG_RE = re.compile(
    r"^(?:-compose|-c|/m(?::.*)?|/mailurl(?::.*)?|ipm\.note)$",
    re.IGNORECASE,
)
_PROTOCOL_RE = re.compile(r"^[a-z][a-z0-9+.-]*:$", re.IGNORECASE)
_WRAPPER_EXES = {
    "launchwinapp.exe",
    "rundll32.exe",
    "explorer.exe",
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "hh.exe",
}


def _is_open_mail_request(text: str) -> bool:
    """Просьба открыть или запустить почту, а не файл, папку или сайт."""
    lowered = (text or "").lower().strip()
    if not lowered or not _MAIL_OPEN_RE.search(lowered):
        return False
    if not _MAIL_TARGET_RE.search(lowered):
        return False
    if _MAIL_NOT_CLIENT_RE.search(lowered):
        return False
    return True


def _windows_registry():
    import winreg

    return winreg


def _reg_value(reg, hive, path: str, name: str = "") -> Optional[str]:
    try:
        with reg.OpenKey(hive, path) as key:
            value, _type = reg.QueryValueEx(key, name)
    except OSError:
        return None
    text = str(value or "").strip()
    return text or None


def _split_windows_args(rest: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    quote = ""
    for char in rest:
        if quote:
            if char == quote:
                quote = ""
            else:
                current.append(char)
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char.isspace():
            if current:
                args.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        args.append("".join(current))
    return args


def _expand_windows_command(command: str) -> str:
    """Раскрывает %VAR% и переменные окружения в команде из реестра."""

    def replace_percent(match: re.Match) -> str:
        return os.environ.get(match.group(1), match.group(0))

    return os.path.expandvars(re.sub(r"%([^%]+)%", replace_percent, command))


def _split_windows_command(command: str) -> list[str]:
    """Разбирает команду из реестра Windows на исполняемый файл и аргументы."""
    command = _expand_windows_command(command.strip())
    if not command:
        return []
    if command[0] == '"':
        end = command.find('"', 1)
        if end == -1:
            return [command.strip('"')]
        exe = command[1:end]
        return [exe, *_split_windows_args(command[end + 1 :].strip())]

    lower = command.lower()
    for ext in (".exe", ".bat", ".cmd", ".com"):
        idx = lower.find(ext)
        if idx != -1 and (idx + len(ext) == len(command) or command[idx + len(ext)].isspace()):
            exe = command[: idx + len(ext)].strip().strip('"')
            return [exe, *_split_windows_args(command[idx + len(ext) :].strip())]
    return _split_windows_args(command)


def _is_compose_arg(arg: str) -> bool:
    return "%" in arg or bool(_COMPOSE_ARG_RE.match(arg))


def _windows_exe_name(path: str) -> str:
    """Имя файла из пути Windows. Не зависит от ОС, на которой запущен разбор."""
    normalized = path.replace("/", "\\").rstrip("\\")
    return normalized.rsplit("\\", 1)[-1].lower()


def _normalize_mail_command(command: str) -> MailLaunch:
    """Превращает команду регистрации клиента в запуск окна, а не нового письма."""
    parts = _split_windows_command(command)
    if not parts or not parts[0]:
        raise ValueError("пустая команда почтового клиента")
    if _PROTOCOL_RE.match(parts[0]):
        return MailLaunch(protocol=parts[0])

    exe_name = _windows_exe_name(parts[0])
    if exe_name in _WRAPPER_EXES:
        raise LookupError(exe_name)
    if any(_is_compose_arg(part) for part in parts[1:]):
        return MailLaunch(argv=(parts[0],))
    return MailLaunch(argv=tuple(parts))


def _protocol_for_handler(prog_id: str, command: str) -> str:
    blob = f"{prog_id}\n{command}".lower()
    if "outlookforwindows" in blob or "olk.exe" in blob:
        return "outlook:"
    if any(
        token in blob
        for token in (
            "windowscommunicationsapps",
            "hxoutlook",
            "windowsmail",
            "microsoft.windowslive.mail",
        )
    ):
        return "outlookmail:"
    return "mailto:"


def _mailto_user_choice(reg) -> tuple[str, str]:
    prog_id = _reg_value(
        reg,
        reg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\mailto\UserChoice",
        "ProgId",
    )
    if not prog_id:
        return "", ""
    command = _reg_value(
        reg,
        reg.HKEY_CLASSES_ROOT,
        rf"{prog_id}\shell\open\command",
        "",
    )
    return prog_id, command or ""


def _registered_client_command(reg) -> Optional[str]:
    """Команда shell\\open клиента из «Программы по умолчанию»."""
    hives = {
        "HKCU": reg.HKEY_CURRENT_USER,
        "HKLM": reg.HKEY_LOCAL_MACHINE,
    }
    defaults: list[tuple[str, str]] = []
    for hive_name, path in (
        ("HKCU", r"Software\Clients\Mail"),
        ("HKLM", r"Software\Clients\Mail"),
        ("HKLM", r"Software\WOW6432Node\Clients\Mail"),
    ):
        name = _reg_value(reg, hives[hive_name], path, "")
        if name and (hive_name, name) not in defaults:
            defaults.append((hive_name, name))

    for hive_name, name in defaults:
        hive_order = []
        for candidate in (hive_name, "HKCU", "HKLM"):
            if candidate not in hive_order:
                hive_order.append(candidate)
        for hive in hive_order:
            for base in (
                rf"Software\Clients\Mail\{name}\shell\open\command",
                rf"Software\WOW6432Node\Clients\Mail\{name}\shell\open\command",
            ):
                command = _reg_value(reg, hives[hive], base, "")
                if command:
                    return command
    return None


def resolve_default_mail_launch(reg=None) -> MailLaunch:
    """Находит почтовый клиент, назначенный в Windows по умолчанию."""
    reg = _windows_registry() if reg is None else reg
    prog_id, mailto_command = _mailto_user_choice(reg)

    if mailto_command:
        try:
            return _normalize_mail_command(mailto_command)
        except LookupError:
            return MailLaunch(protocol=_protocol_for_handler(prog_id, mailto_command))

    if prog_id:
        return MailLaunch(protocol=_protocol_for_handler(prog_id, ""))

    client_command = _registered_client_command(reg)
    if client_command:
        try:
            return _normalize_mail_command(client_command)
        except LookupError:
            return MailLaunch(protocol=_protocol_for_handler("", client_command))

    class_command = _reg_value(reg, reg.HKEY_CLASSES_ROOT, r"mailto\shell\open\command", "") or ""
    if class_command:
        try:
            return _normalize_mail_command(class_command)
        except LookupError:
            return MailLaunch(protocol=_protocol_for_handler("", class_command))

    return MailLaunch(protocol="mailto:")


def _start_mail_process(argv: list[str]) -> None:
    """Запускает exe. Для app execution alias пробует ShellExecute, если CreateProcess не смог."""
    try:
        subprocess.Popen(argv)
        return
    except OSError as exc:
        params = subprocess.list2cmdline(argv[1:]) if len(argv) > 1 else None
        if _shell_execute(argv[0], params):
            return
        startfile = getattr(os, "startfile", None)
        if startfile is not None and len(argv) == 1:
            startfile(argv[0])
            return
        raise exc


def perform_mail_launch(launch: MailLaunch) -> None:
    """Запускает найденный клиент или его протокол."""
    if launch.argv:
        _start_mail_process(list(launch.argv))
        return

    protocol = launch.protocol or "mailto:"
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise OSError("Запуск почтового клиента поддерживается только в Windows")
    startfile(protocol)


def execute_mail_command(text: str) -> Optional[str]:
    """Открывает почтовый клиент по умолчанию."""
    if not _is_open_mail_request(text):
        return None

    try:
        launch = resolve_default_mail_launch()
        print(f"[OPEN_MAIL] {launch.argv or launch.protocol}")
        perform_mail_launch(launch)
        return "Открываю почтовый клиент по умолчанию."
    except Exception as e:
        return f"Ошибка открытия почты: {e}"


def execute_rebuild_index_command(text: str) -> Optional[str]:
    """Обновляет индекс приложений по команде пользователя."""
    lowered = text.lower().strip()
    
    if not re.search(r"\b(обнови|обновить|переиндексир|переиндексируй|пересканируй)\b", lowered):
        return None
    
    if not re.search(r"\b(приложени|программ|индекс)\w*\b", lowered):
        return None
    
    try:
        global APP_INDEX
        print("[APP_INDEX] Запуск переиндексирования...")
        APP_INDEX = build_app_index()
        return f"Индекс приложений обновлён. Найдено приложений: {len(APP_INDEX)}."
    except Exception as e:
        return f"Ошибка обновления индекса: {e}"


def execute_coin_flip_command(text: str) -> Optional[str]:
    """Подбрасывает монетку."""
    if not any(t in text.lower() for t in ['подбрось монет', 'орёл или решк', 'монетк', 'орел или решк']):
        return None
    
    import random
    result = random.choice(['орёл', 'решка'])
    return f"{'Выпал' if result == 'орёл' else 'Выпала'} {result}"

