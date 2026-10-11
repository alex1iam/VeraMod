import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

def _ensure_importable(module_name: str) -> None:
    if module_name in sys.modules:
        return
    try:
        __import__(module_name)
    except ImportError:
        sys.modules[module_name] = types.ModuleType(module_name)


for _optional_module in ("winreg", "win32gui", "win32con", "win32process"):
    _ensure_importable(_optional_module)

from main.commands import app_control


class _RegistryKey:
    def __init__(self, values: dict):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CLASSES_ROOT = "HKCR"

    def __init__(self, keys: dict):
        self.keys = keys

    def OpenKey(self, root, subkey):
        values = self.keys.get((root, subkey))
        if values is None:
            raise OSError(2, "not found")
        return _RegistryKey(values)

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise OSError(2, "not found")
        return key.values[name], 1


class AppControlTests(unittest.TestCase):
    def test_app_index_is_loaded_lazily_and_cached(self):
        indexed_app = {
            "display_name": "Calculator",
            "exe_name": "calc.exe",
            "lnk_path": "",
        }

        with (
            patch.object(app_control, "APP_INDEX", None),
            patch.object(app_control, "load_app_index", return_value=[indexed_app]) as load,
        ):
            self.assertEqual(app_control._best_app_match("calculator"), indexed_app)
            self.assertEqual(app_control._best_app_match("calculator"), indexed_app)

        load.assert_called_once_with()

    def test_mail_handler_runs_before_generic_app_search(self):
        source = Path(app_control.__file__).with_name("__init__.py").read_text(encoding="utf-8")
        handlers = source[source.index("HANDLERS = (") :]
        self.assertLess(
            handlers.index("execute_mail_command"),
            handlers.index("execute_app_command"),
        )

    def test_open_mail_launches_user_choice_client_without_compose_args(self):
        winreg = _FakeWinreg({
            (
                "HKCU",
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\mailto\UserChoice",
            ): {"ProgId": "Outlook.URL.mailto.15"},
            ("HKCR", r"Outlook.URL.mailto.15\shell\open\command"): {
                "": r'"C:\Program Files\Microsoft Office\root\Office16\OUTLOOK.EXE" -c IPM.Note /m "%1"',
            },
        })

        with (
            patch.dict(sys.modules, {"winreg": winreg}),
            patch.object(app_control.subprocess, "Popen") as popen,
            patch.object(app_control.os, "startfile", create=True) as startfile,
        ):
            result = app_control.execute_mail_command("Вера, открой почту")

        self.assertEqual(result, "Открываю почтовый клиент по умолчанию.")
        popen.assert_called_once_with([r"C:\Program Files\Microsoft Office\root\Office16\OUTLOOK.EXE"])
        startfile.assert_not_called()

    def test_open_mail_accepts_common_phrasings(self):
        for phrase in (
            "запусти почтовый клиент",
            "открыть электронную почту",
            "открой мне e-mail",
            "запусти mail",
            "открой имейл",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(app_control._is_open_mail_request(phrase))

    def test_open_mail_ignores_unrelated_requests(self):
        for phrase in (
            "какая почта",
            "закрой почту",
            "запомни мой email",
            "открой файл почта.docx",
            "открой папку почта",
            "открой сайт почта",
            "открой браузер",
            "открой документ с почтой",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(app_control.execute_mail_command(phrase))

    def test_store_mail_app_opens_by_protocol_when_command_is_a_launcher(self):
        winreg = _FakeWinreg({
            (
                "HKCU",
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\mailto\UserChoice",
            ): {"ProgId": "AppXmail"},
            ("HKCR", r"AppXmail\shell\open\command"): {
                "": r'"C:\Windows\System32\LaunchWinApp.exe" AppXmail',
            },
        })

        with (
            patch.dict(sys.modules, {"winreg": winreg}),
            patch.object(app_control.subprocess, "Popen") as popen,
            patch.object(app_control.os, "startfile", create=True) as startfile,
        ):
            result = app_control.execute_mail_command("открой почту")

        self.assertEqual(result, "Открываю почтовый клиент по умолчанию.")
        popen.assert_not_called()
        startfile.assert_called_once_with("mailto:")

    def test_shell_appsfolder_mail_command_is_launched_as_is(self):
        command = (
            r"C:\Windows\explorer.exe "
            r"shell:AppsFolder\Microsoft.WindowsCommunicationsApps_8wekyb3d8bbwe!microsoft.windowslive.mail"
        )
        winreg = _FakeWinreg({
            (
                "HKCU",
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\mailto\UserChoice",
            ): {"ProgId": "AppXmail"},
            ("HKCR", r"AppXmail\shell\open\command"): {"": command},
        })

        with (
            patch.dict(sys.modules, {"winreg": winreg}),
            patch.object(app_control.subprocess, "Popen") as popen,
            patch.object(app_control.os, "startfile", create=True) as startfile,
        ):
            result = app_control.execute_mail_command("запусти почту")

        self.assertEqual(result, "Открываю почтовый клиент по умолчанию.")
        popen.assert_called_once_with([
            r"C:\Windows\explorer.exe",
            r"shell:AppsFolder\Microsoft.WindowsCommunicationsApps_8wekyb3d8bbwe!microsoft.windowslive.mail",
        ])
        startfile.assert_not_called()

    def test_clients_mail_is_used_when_user_choice_is_missing(self):
        winreg = _FakeWinreg({
            ("HKCU", r"Software\Clients\Mail"): {"": "Mozilla Thunderbird"},
            ("HKCU", r"Software\Clients\Mail\Mozilla Thunderbird\shell\open\command"): {
                "": r'"C:\Program Files\Mozilla Thunderbird\thunderbird.exe" -mail',
            },
        })

        with (
            patch.dict(sys.modules, {"winreg": winreg}),
            patch.object(app_control.subprocess, "Popen") as popen,
            patch.object(app_control.os, "startfile", create=True) as startfile,
        ):
            result = app_control.execute_mail_command("открой почту")

        self.assertEqual(result, "Открываю почтовый клиент по умолчанию.")
        popen.assert_called_once_with([r"C:\Program Files\Mozilla Thunderbird\thunderbird.exe"])
        startfile.assert_not_called()

    def test_mailto_protocol_is_the_fallback(self):
        winreg = _FakeWinreg({})

        with (
            patch.dict(sys.modules, {"winreg": winreg}),
            patch.object(app_control.subprocess, "Popen") as popen,
            patch.object(app_control.os, "startfile", create=True) as startfile,
        ):
            result = app_control.execute_mail_command("открой почту")

        self.assertEqual(result, "Открываю почтовый клиент по умолчанию.")
        popen.assert_not_called()
        startfile.assert_called_once_with("mailto:")


if __name__ == "__main__":
    unittest.main()
