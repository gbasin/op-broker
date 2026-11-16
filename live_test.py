"""Opt-in create/read/run/delete test in a vault selected by the operator."""

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--wrapper", default=str(Path(__file__).with_name("op_broker.py")))
parser.add_argument("--account", required=True)
parser.add_argument(
    "--vault",
    required=True,
    help="private or disposable vault where a synthetic item may be created and deleted",
)
options = parser.parse_args()
wrapper = options.wrapper
command = [sys.executable, "-I", wrapper] if wrapper.endswith(".py") else [wrapper]
account = options.account
vault = options.vault
results = []


def call(*args, body=None, env=None):
    started = time.monotonic()
    r = subprocess.run(
        [*command, *args, "--account", account],
        input=json.dumps(body).encode() if body is not None else None,
        capture_output=True,
        env=env,
        timeout=120,
    )
    results.append(
        {
            "command": "read synthetic item"
            if args[0] == "read"
            else " ".join(args[:2]),
            "ok": r.returncode == 0,
            "seconds": round(time.monotonic() - started, 3),
        }
    )
    if r.returncode:
        raise RuntimeError("live CLI test failed: " + r.stderr.decode()[:250])
    return r.stdout


item_id = None
marker = secrets.token_hex(16)
try:
    body = {
        "title": "Temporary op-broker integration test " + marker[:8],
        "category": "SECURE_NOTE",
        "fields": [
            {
                "id": "notesPlain",
                "type": "STRING",
                "purpose": "NOTES",
                "label": "notesPlain",
                "value": marker,
            }
        ],
    }
    # First prove JSON stdin without creating anything.
    preview = json.loads(
        call(
            "item",
            "create",
            "-",
            "--vault",
            vault,
            "--dry-run",
            "--format=json",
            body=body,
        )
    )
    assert preview["title"] == body["title"]
    item = json.loads(
        call("item", "create", "-", "--vault", vault, "--format=json", body=body)
    )
    item_id = item["id"]
    got = json.loads(call("item", "get", item_id, "--format=json"))
    assert next(f["value"] for f in got["fields"] if f["id"] == "notesPlain") == marker
    ref = "op://" + vault + "/" + item_id + "/notesPlain"
    assert call("read", ref).decode().strip() == marker
    env = dict(os.environ, OP_BROKER_TEST_NOTE=ref)
    # Invoke op run separately because its trailing flags belong to the child.
    start = time.monotonic()
    r = subprocess.run(
        [
            *command,
            "run",
            "--account",
            account,
            "--",
            sys.executable,
            "-c",
            'import os,sys; assert os.environ["OP_BROKER_TEST_NOTE"]==sys.argv[1]',
            marker,
        ],
        env=env,
        capture_output=True,
        timeout=120,
    )
    results.append(
        {
            "command": "run synthetic secret injection",
            "ok": r.returncode == 0,
            "seconds": round(time.monotonic() - start, 3),
        }
    )
    assert r.returncode == 0, "op run failed"
finally:
    if item_id:
        call("item", "delete", item_id)
        deleted = True
    else:
        deleted = False
print(json.dumps({"synthetic_item_deleted": deleted, "checks": results}, indent=2))
