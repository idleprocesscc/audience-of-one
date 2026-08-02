from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audience_of_one import shortcuts


class ShortcutTests(unittest.TestCase):
    def test_install_builds_stable_launcher_and_importable_workflow(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            config = home / ".config" / "audience-of-one" / "config.toml"
            with mock.patch("pathlib.Path.home", return_value=home), \
                 mock.patch("sys.executable", str(home / "venv" / "bin" / "python")):
                receipt = shortcuts.install(config, "local_mac", open_import=False)
                launcher = Path(receipt["launcher"])
                workflow = Path(receipt["workflow"])
                self.assertTrue(launcher.is_file())
                text = launcher.read_text()
                self.assertIn("-m audience_of_one", text)
                self.assertIn(str(home / "venv" / "bin" / "python"), text)
                self.assertIn(str(config), text)
                self.assertEqual(launcher.stat().st_mode & 0o777, 0o700)
                document = workflow / "Contents" / "document.wflow"
                with document.open("rb") as handle:
                    payload = plistlib.load(handle)
                command = payload["actions"][0]["action"]["ActionParameters"][
                    "COMMAND_STRING"
                ]
                self.assertEqual(command, str(launcher))

    def test_doctor_is_read_only_and_reports_each_layer(self):
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch("pathlib.Path.home", return_value=Path(raw)), \
             mock.patch("audience_of_one.shortcuts.shutil.which", return_value="/usr/bin/shortcuts"), \
             mock.patch("audience_of_one.shortcuts.subprocess.run") as run:
            run.return_value.stdout = f"{shortcuts.SHORTCUT_NAME}\n"
            payload = shortcuts.doctor()
            self.assertFalse(payload["ready"])
            self.assertTrue(payload["checks"]["shortcut"])
            self.assertFalse(payload["checks"]["launcher"])


if __name__ == "__main__":
    unittest.main()
