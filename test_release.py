import importlib.util
import contextlib
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "build_release", Path(__file__).parent / "scripts/build_release.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_archive_is_reproducible_and_excludes_untracked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                subprocess.run(
                    ["git", "-C", str(root), *args], check=True, capture_output=True
                )

            git("init")
            git("config", "user.name", "Release Test")
            git("config", "user.email", "release-test@example.invalid")
            git("config", "commit.gpgsign", "false")
            (root / "op_broker.py").write_text('__version__ = "0.1.0"\n')
            git("add", "op_broker.py")
            git("commit", "-m", "fixture")
            git("tag", "v0.1.0")
            (root / "private-untracked.txt").write_text("must not ship")
            with contextlib.redirect_stdout(io.StringIO()):
                artifact = release.build("v0.1.0", root)
                first = artifact.read_bytes()
                release.build("v0.1.0", root)
            self.assertEqual(first, artifact.read_bytes())
            self.assertIn(
                hashlib.sha256(first).hexdigest(),
                (root / "dist/SHA256SUMS").read_text(),
            )
            with tarfile.open(artifact) as archive:
                self.assertEqual(
                    archive.getnames(),
                    ["op-broker-0.1.0", "op-broker-0.1.0/op_broker.py"],
                )
            (root / "op_broker.py").write_text('__version__ = "0.1.0"\n# dirty\n')
            with self.assertRaises(ValueError):
                release.build("v0.1.0", root)

    def test_release_tag_validation(self):
        release.validate_tag("v0.1.0", "0.1.0")
        for tag in ("main", "v0.2.0", "v0.1.0; false", "../v0.1.0", "v0.1.0\n"):
            with self.assertRaises(ValueError):
                release.validate_tag(tag, "0.1.0")


if __name__ == "__main__":
    unittest.main()
