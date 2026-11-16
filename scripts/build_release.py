#!/usr/bin/env python3
"""Build only tracked, tagged source; never include local proof files or credentials."""

import argparse
import gzip
import hashlib
import io
from pathlib import Path
import re
import subprocess
import tarfile


def validate_tag(tag, version):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) or tag != "v" + version:
        raise ValueError("tag must exactly match the source version")


def build(tag, root=Path(".")):
    text = (root / "op_broker.py").read_text()
    version = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", text).group(1)
    validate_tag(tag, version)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args])

    head = git("rev-parse", "HEAD").strip()
    tagged = git("rev-parse", tag + "^{commit}").strip()
    if head != tagged:
        raise ValueError("checkout must be exactly the tagged commit")
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError(
            "tracked working tree changes must be committed before release"
        )
    prefix = f"op-broker-{version}/"
    source = git("archive", "--format=tar", "--prefix=" + prefix, tag)
    with tarfile.open(fileobj=io.BytesIO(source)) as archive:
        names = archive.getnames()
        for name in names:
            relative = name.removeprefix(prefix)
            if relative in (
                "VERIFIED.md",
                "live-reuse-proof.json",
                "live-write-proof.json",
            ) or relative.startswith((".env", "dist/")):
                raise ValueError("local/private artifact is tracked; refusing release")
    output = root / "dist"
    output.mkdir(exist_ok=True)
    artifact = output / f"op-broker-{version}.tar.gz"
    # Stable gzip metadata so identical tagged source has an identical checksum.
    with artifact.open("wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", filename="", mtime=0) as compressed:
            compressed.write(source)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(f"{digest}  {artifact.name}\n")
    print(artifact)
    return artifact


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    build(parser.parse_args().tag)
