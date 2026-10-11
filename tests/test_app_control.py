import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _ensure_windows_module(name: str) -> None:
    """Подменяет модули Windows только там, где их нет, не затирая системные."""
    if name in sys.modules:
        return
    try:
        __import__(name)
    except ModuleNotFoundError:
        sys.modules[name] = types.ModuleType(name)


for _module_name in ("winreg", "win32gui", "win32con", "win32process", "win32com", "win32com.client"):
    _ensure_windows_module(_module_name)

from main.commands import app_control
from main.commands.app_control import MailLaunch


ROOT = Path(__file__).resolve().parents[1]


class _Key(dict):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CLASSES_ROOT = "HKCR"

    def __init__(self, values):
        self.values = values

    def OpenKey(self, hive, path):
        key = self.values.get((hive, path))
        if key is None:
            raise FileNotFoundError(path)
        return key

    @staticmethod
    def QueryValueEx(key, name):
        if name not in key:
            raise FileNotFoundError(name)
        return key[name], 1


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


class MailClientCommandTests(unittest.TestCase):
    def test_open_and_launch_requests_start_the_default_client(self):
        launch = MailLaunch(argv=(r"C:\Mail\client.exe",))
        phrases = [
            "открой почту",
            "запусти почту",
            "Вера, открой почту",
            "откройте почту",
            "запустите почтовый клиент",
            "открыть почту",
            "запустить электронную почту",
            "открой email",
            "запусти e-mail",
            "открой mail",
            "открой, пожалуйста, почту",
        ]

        for phrase in phrases:
            with self.subTest(phrase=phrase):
                with (
                    patch.object(app_control, "resolve_default_mail_launch", return_value=launch),
                    patch.object(app_control, "perform_mail_launch") as perform,
                ):
                    self.assertEqual(
                        app_control.execute_mail_command(phrase),
                        "Открываю почтовый клиент по умолчанию.",
                    )
                perform.assert_called_once_with(launch)

    def test_unrelated_mail_phrases_are_not_intercepted(self):
        phrases = [
            "закрой почту",
            "проверь почту",
            "что в почте",
            "открой хром",
            "открой файл почта",
            "открой папку почта",
            "открой mail.ru",
            "открой https://mail.google.com",
            "открой почтальона",
            "привет",
        ]

        for phrase in phrases:
            with self.subTest(phrase=phrase):
                with patch.object(app_control, "resolve_default_mail_launch") as resolve:
                    self.assertIsNone(app_control.execute_mail_command(phrase))
                resolve.assert_not_called()

    def test_user_choice_wins_over_stale_registered_client(self):
        registry = FakeRegistry({
            (
                "HKCU",
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\mailto\UserChoice",
            ): _Key({"ProgId": "Thunderbird.Url.mailto"}),
            ("HKCR", r"Thunderbird.Url.mailto\shell\open\command"): _Key({
                "": r'"C:\Program Files\Mozilla Thunderbird\thunderbird.exe" -osint -compose "%1"',
            }),
            ("HKCU", r"Software\Clients\Mail"): _Key({"": "Microsoft Outlook"}),
            ("HKLM", r"Software\Clients\Mail\Microsoft Outlook\shell\open\command"): _Key({
                "": r'"C:\Program Files\Microsoft Office\root\Office16\OUTLOOK.EXE" /recycle',
            }),
        })

        launch = app_control.resolve_default_mail_launch(registry)

        self.assertEqual(
            launch.argv,
            (r"C:\Program Files\Mozilla Thunderbird\thunderbird.exe",),
        )
        self.assertEqual(launch.protocol, "")

    def test_registered_client_keeps_window_open_arguments(self):
        registry = FakeRegistry({
            ("HKCU", r"Software\Clients\Mail"): _Key({"": "Microsoft Outlook"}),
            ("HKLM", r"Software\Clients\Mail\Microsoft Outlook\shell\open\command"): _Key({
                "": r'"C:\Program Files\Microsoft Office\root\Office16\OUTLOOK.EXE" /recycle',
            }),
        })

        launch = app_control.resolve_default_mail_launch(registry)

        self.assertEqual(
            launch.argv,
            (r"C:\Program Files\Microsoft Office\root\Office16\OUTLOOK.EXE", "/recycle"),
        )

    def test_new_outlook_compose_command_launches_the_executable(self):
        registry = FakeRegistry({
            (
                "HKCU",
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\mailto\UserChoice",
            ): _Key({"ProgId": "OutlookForWindows"}),
            ("HKCR", r"OutlookForWindows\shell\open\command"): _Key({
                "": r'"%LOCALAPPDATA%\Microsoft\WindowsApps\olk.exe" "%1"',
            }),
        })

        with patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\Vera\AppData\Local"}):
            launch = app_control.resolve_default_mail_launch(registry)

        self.assertEqual(launch.argv, (r"C:\Users\Vera\AppData\Local\Microsoft\WindowsApps\olk.exe",))

    def test_windows_mail_wrapper_uses_its_protocol(self):
        registry = FakeRegistry({
            (
                "HKCU",
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\mailto\UserChoice",
            ): _Key({"ProgId": "AppXmail"}),
            ("HKCR", r"AppXmail\shell\open\command"): _Key({
                "": r'"C:\Windows\System32\LaunchWinApp.exe" microsoft.windowscommunicationsapps "%1"',
            }),
        })

        launch = app_control.resolve_default_mail_launch(registry)

        self.assertEqual(launch.protocol, "outlookmail:")
        self.assertEqual(launch.argv, ())

    def test_classic_mailto_class_is_used_when_user_choice_is_missing(self):
        registry = FakeRegistry({
            ("HKCR", r"mailto\shell\open\command"): _Key({
                "": r'"D:\Mail\mail.exe" -compose "%1"',
            }),
        })

        launch = app_control.resolve_default_mail_launch(registry)

        self.assertEqual(launch.argv, (r"D:\Mail\mail.exe",))

    def test_missing_registration_falls_back_to_mailto_protocol(self):
        launch = app_control.resolve_default_mail_launch(FakeRegistry({}))

        self.assertEqual(launch, MailLaunch(protocol="mailto:"))

    def test_perform_launch_starts_exe_or_protocol(self):
        with patch.object(app_control.subprocess, "Popen") as popen:
            app_control.perform_mail_launch(
                MailLaunch(argv=(r"C:\Outlook\OUTLOOK.EXE", "/recycle"))
            )
        popen.assert_called_once_with([r"C:\Outlook\OUTLOOK.EXE", "/recycle"])

        with patch.object(app_control.os, "startfile", create=True) as startfile:
            app_control.perform_mail_launch(MailLaunch(protocol="mailto:"))
        startfile.assert_called_once_with("mailto:")

    def test_app_alias_falls_back_to_shell_execute(self):
        with (
            patch.object(app_control.subprocess, "Popen", side_effect=OSError("alias")),
            patch.object(app_control, "_shell_execute", return_value=True) as shell,
        ):
            app_control.perform_mail_launch(
                MailLaunch(argv=(r"C:\Users\Vera\AppData\Local\Microsoft\WindowsApps\olk.exe",))
            )

        shell.assert_called_once_with(
            r"C:\Users\Vera\AppData\Local\Microsoft\WindowsApps\olk.exe",
            None,
        )

    def test_launch_failure_is_reported(self):
        with (
            patch.object(
                app_control,
                "resolve_default_mail_launch",
                return_value=MailLaunch(protocol="mailto:"),
            ),
            patch.object(
                app_control,
                "perform_mail_launch",
                side_effect=OSError("нет клиента"),
            ),
        ):
            self.assertEqual(
                app_control.execute_mail_command("открой почту"),
                "Ошибка открытия почты: нет клиента",
            )

    def test_mail_handler_is_registered_before_generic_app_launch(self):
        init_py = (ROOT / "main" / "commands" / "__init__.py").read_text(encoding="utf-8")
        handlers = init_py[init_py.index("HANDLERS = (") :]

        self.assertLess(
            handlers.index("execute_mail_command"),
            handlers.index("execute_app_command"),
        )

    def test_help_lists_mail_command(self):
        help_py = (ROOT / "main" / "commands" / "help_commands.py").read_text(encoding="utf-8")

        self.assertIn("Открой почту", help_py)
        self.assertIn("запусти почту", help_py)


if __name__ == "__main__":
    unittest.main()
