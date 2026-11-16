import contextlib
import importlib.util
import io
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "installer", Path(__file__).with_name("install.py")
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opbi-", dir="/private/tmp")
        self.root = Path(self.temp.name)
        self.runtime = self.root / "lib/op-broker"
        self.bin = self.root / "bin"
        self.patches = [
            patch.object(installer, "RUNTIME", self.runtime),
            patch.object(installer, "BIN", self.bin),
            patch.dict(os.environ, {"OP_BROKER_DIR": str(self.root / "state")}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def run_install(self, *args):
        with (
            patch(
                "sys.argv",
                ["install.py", *args]
                if args
                else ["install.py", "--real-op", __import__("sys").executable],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            installer.main()

    def test_install_and_hash_checked_rollback(self):
        self.run_install()
        self.assertTrue((self.bin / "op").is_file())
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.runtime / "op_broker.py").stat().st_mode & 0o777, 0o600)
        smoke = subprocess.run(
            [str(self.bin / "op"), "--version"], capture_output=True, timeout=10
        )
        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        self.assertIn(b"Python", smoke.stdout)
        self.run_install("--uninstall")
        self.assertFalse((self.bin / "op").exists())
        self.assertFalse(self.runtime.exists())

    def test_preserve_existing_and_subsequently_modified_files(self):
        self.bin.mkdir()
        existing = self.bin / "op"
        existing.write_text("existing user wrapper")
        with self.assertRaises(SystemExit):
            self.run_install()
        self.assertEqual(existing.read_text(), "existing user wrapper")
        existing.unlink()
        self.run_install()
        existing.write_text("modified after install")
        with self.assertRaises(SystemExit):
            self.run_install()
        with self.assertRaises(SystemExit):
            self.run_install("--uninstall")
        self.assertEqual(existing.read_text(), "modified after install")
        self.assertTrue((self.runtime / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
