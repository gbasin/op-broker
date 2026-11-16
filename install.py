#!/usr/bin/env python3
"""Install or remove only the files managed by this wrapper."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
HOME_DIR = Path.home()
RUNTIME = HOME_DIR / ".local/lib/op-broker"
BIN = HOME_DIR / ".local/bin"
MARKER = "# Managed by op-broker; signed native CLI is preserved."


def atomic(path, data, mode):
    fd, name = tempfile.mkstemp(prefix=".op-broker-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main():
    manifest = RUNTIME / "manifest.json"
    if sys.argv[1:] == ["--uninstall"]:
        if not manifest.is_file():
            raise SystemExit("No managed installation found")
        files = json.loads(manifest.read_text())["files"]
        # Validate all files before deleting any, preserving subsequent user edits.
        for name, digest in files.items():
            p = Path(name)
            if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() != digest:
                raise SystemExit("Refusing to remove a modified managed file: " + name)
        r = subprocess.run(
            [str(BIN / "op"), "--broker-stop"], capture_output=True, timeout=10
        )
        if r.returncode and b"not running" not in r.stderr:
            raise SystemExit("Could not stop broker; no installed files removed")
        for name in files:
            Path(name).unlink(missing_ok=True)
        manifest.unlink()
        try:
            RUNTIME.rmdir()
        except OSError:
            pass
        print("Removed wrapper. The native CLI is unchanged.")
        return
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real-op", help="absolute path to the original signed op executable"
    )
    options = parser.parse_args()
    from op_broker import find_real_op, find_tmux

    real = options.real_op or find_real_op()
    if (
        not os.path.isabs(real)
        or not os.path.isfile(real)
        or not os.access(real, os.X_OK)
    ):
        raise SystemExit("Native op must be an absolute executable path")
    if Path(real).resolve() == (BIN / "op").resolve():
        raise SystemExit("Native op cannot be the broker wrapper")
    find_tmux()

    for directory in (RUNTIME, BIN):
        if directory.is_symlink():
            raise SystemExit("Refusing symlink installation directory")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if RUNTIME.stat().st_uid != os.getuid() or RUNTIME.stat().st_mode & 0o077:
        raise SystemExit("Runtime directory must be owned by you and mode 0700")
    if manifest.is_file():
        for name, digest in json.loads(manifest.read_text())["files"].items():
            p = Path(name)
            if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() != digest:
                raise SystemExit(
                    "Refusing to overwrite a modified managed file: " + name
                )
    for name in ("op", "op-native", "op-broker-uninstall"):
        p = BIN / name
        if p.exists() or p.is_symlink():
            if p.is_symlink() or (
                MARKER not in p.read_text()
                and "# Managed by op-broker; signed CLI remains " not in p.read_text()
            ):
                raise SystemExit("Refusing to overwrite existing " + str(p))
    files = {}
    payloads = {
        RUNTIME / "op_broker.py": (ROOT / "op_broker.py").read_bytes(),
        RUNTIME / "install.py": Path(__file__).read_bytes(),
        RUNTIME / "README.md": (ROOT / "README.md").read_bytes(),
    }
    python = shlex.quote(sys.executable)

    def script(line):
        return ("#!/bin/sh\n" + MARKER + "\nexec " + line + ' "$@"\n').encode()

    launcher = "#!/bin/sh\n" + MARKER + "\n"
    launcher += (
        'if [ -z "${OP_BROKER_REAL:-}" ]; then\n  OP_BROKER_REAL='
        + shlex.quote(real)
        + "\n  export OP_BROKER_REAL\nfi\n"
    )
    launcher += (
        "exec "
        + python
        + " -I "
        + shlex.quote(str(RUNTIME / "op_broker.py"))
        + ' "$@"\n'
    )
    payloads[BIN / "op"] = launcher.encode()
    payloads[BIN / "op-native"] = script(shlex.quote(real))
    payloads[BIN / "op-broker-uninstall"] = (
        "#!/bin/sh\n"
        + MARKER
        + "\nexec "
        + python
        + " -I "
        + shlex.quote(str(RUNTIME / "install.py"))
        + " --uninstall\n"
    ).encode()
    for path, data in payloads.items():
        atomic(path, data, 0o600 if path.name.endswith((".py", ".md")) else 0o700)
        files[str(path)] = hashlib.sha256(data).hexdigest()
    atomic(manifest, json.dumps({"files": files}, indent=2).encode(), 0o600)
    print("Installed " + str(BIN / "op") + "; native CLI preserved at " + real)
    print("Rollback: " + str(BIN / "op-broker-uninstall"))


if __name__ == "__main__":
    main()
