import concurrent.futures
import json
import os
import pty
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

WRAPPER = str(Path(__file__).with_name("op_broker.py"))
FAKE = (
    "#!"
    + sys.executable
    + """
import json,os,signal,subprocess,sys,time
mode=sys.argv[1]
if mode=='echo':
 data=sys.stdin.buffer.read();sys.stdout.buffer.write(data);sys.stderr.buffer.write(b'err\\x00\\xff')
elif mode=='context':
 with open('/dev/tty') as tty: identity=os.ttyname(tty.fileno())
 print(json.dumps({'tty':identity,'sid':os.getsid(0),'cwd':os.getcwd(),'value':os.environ.get('TEST_VALUE'),'argv':sys.argv[2:],'stdin_tty':os.isatty(0)}))
elif mode=='exit':sys.exit(int(sys.argv[2]))
elif mode=='signal':os.kill(os.getpid(),signal.SIGTERM)
elif mode=='mutate':
 with open(sys.argv[2],'ab') as f:f.write(sys.stdin.buffer.read())
elif mode=='sleep':
 with open(sys.argv[2],'w') as f:f.write(str(os.getpid()))
 time.sleep(60)
elif mode in ('descendant','stubborn'):
 if mode=='stubborn':signal.signal(signal.SIGTERM,signal.SIG_IGN)
 code='import time,signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)' if mode=='stubborn' else 'import time;time.sleep(60)'
 p=subprocess.Popen([sys.executable,'-c',code])
 with open(sys.argv[2],'w') as f:json.dump([os.getpid(),p.pid],f)
 time.sleep(60)
elif mode=='slow':time.sleep(float(sys.argv[2]));print('done')
elif mode=='run':subprocess.run(sys.argv[2:],check=True)
"""
)


def wait_for(path):
    for _ in range(150):
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError("fixture did not start")


def alive(pid):
    try:
        os.kill(pid, 0)
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
        )
        return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")
    except ProcessLookupError:
        return False


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="opb-", dir="/private/tmp" if sys.platform == "darwin" else "/tmp"
        )
        self.root = Path(self.temp.name)
        self.fake = self.root / "real-op"
        self.fake.write_text(FAKE)
        self.fake.chmod(0o700)
        self.env = dict(
            os.environ,
            OP_BROKER_DIR=str(self.root / "broker"),
            OP_BROKER_REAL=str(self.fake),
        )
        self.env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
        self.env.pop("OP_CONNECT_HOST", None)
        self.env.pop("OP_BROKER_BYPASS", None)

    def call(self, *args, **kwargs):
        return subprocess.run(
            [sys.executable, "-I", WRAPPER, *args],
            env=self.env,
            capture_output=True,
            timeout=15,
            **kwargs,
        )

    def tearDown(self):
        self.call("--broker-stop")
        self.temp.cleanup()

    def test_bytes_stderr_large_input_and_nonzero(self):
        data = bytes(range(256)) * 16384
        r = self.call("echo", input=data)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, data)
        self.assertEqual(r.stderr, b"err\x00\xff")
        self.assertEqual(self.call("exit", "37").returncode, 37)
        self.assertEqual(self.call("signal").returncode, -signal.SIGTERM)

    def test_independent_clients_share_terminal_preserve_env_cwd_argv(self):
        self.env["TEST_VALUE"] = "a\nvalue $()"
        contexts = []
        for _ in range(3):
            r = self.call("context", "space value", "$(false)", "", cwd=self.root)
            self.assertEqual(r.returncode, 0, r.stderr)
            contexts.append(json.loads(r.stdout))
        self.assertEqual(len({x["sid"] for x in contexts}), 1)
        self.assertEqual(len({x["tty"] for x in contexts}), 1)
        self.assertEqual(contexts[0]["cwd"], str(self.root))
        self.assertEqual(contexts[0]["value"], "a\nvalue $()")
        self.assertEqual(contexts[0]["argv"], ["space value", "$(false)", ""])
        self.assertFalse(contexts[0]["stdin_tty"])

    def test_concurrent_startup_and_isolated_streams(self):
        def one(i):
            data = (str(i) + "\x00").encode() * 50000
            r = self.call("echo", input=data)
            self.assertEqual(
                (r.returncode, r.stdout, r.stderr), (0, data, b"err\x00\xff")
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(one, range(15)))

    def test_mutation_runs_once_and_json_stdin(self):
        path = self.root / "mutation"
        data = b'{"value":"spaces\\nnull\\u0000"}'
        r = self.call("mutate", str(path), input=data)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(path.read_bytes(), data)

    def test_client_signal_and_disconnect_kill_descendants(self):
        for action in ("signal", "disconnect"):
            path = self.root / action
            p = subprocess.Popen(
                [sys.executable, "-I", WRAPPER, "descendant", str(path)],
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            wait_for(path)
            ids = json.loads(path.read_text())
            p.send_signal(signal.SIGTERM if action == "signal" else signal.SIGKILL)
            p.communicate(timeout=5)
            for _ in range(100):
                if not any(alive(pid) for pid in ids):
                    break
                time.sleep(0.03)
            self.assertFalse(any(alive(pid) for pid in ids))

    def test_cancelled_queued_mutation_never_runs(self):
        path = self.root / "busy"
        p = subprocess.Popen(
            [sys.executable, "-I", WRAPPER, "sleep", str(path)],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        wait_for(path)
        mutation = self.root / "must-not-exist"
        q = subprocess.Popen(
            [sys.executable, "-I", WRAPPER, "mutate", str(mutation)],
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.2)
        q.kill()
        q.wait(timeout=5)
        p.terminate()
        p.communicate(timeout=5)
        q.communicate(timeout=5)
        self.call("context")
        self.assertFalse(mutation.exists())

    def test_restart_and_stale_socket(self):
        first = json.loads(self.call("context").stdout)
        status = json.loads(self.call("--broker-status").stdout)
        os.kill(status["pid"], signal.SIGKILL)
        time.sleep(0.1)
        second = json.loads(self.call("context").stdout)
        self.assertNotEqual(first["sid"], second["sid"])

    def test_unsafe_directory_and_socket_rejected(self):
        directory = Path(self.env["OP_BROKER_DIR"])
        directory.mkdir(mode=0o755)
        self.assertEqual(self.call("context").returncode, 125)
        directory.chmod(0o700)
        (directory / "broker.sock").symlink_to(self.fake)
        self.assertEqual(self.call("context").returncode, 125)
        (directory / "broker.sock").unlink()
        (directory / "broker.sock").write_text("do not replace")
        self.assertEqual(self.call("context").returncode, 125)
        self.assertEqual((directory / "broker.sock").read_text(), "do not replace")
        (directory / "broker.sock").unlink()

    def test_malformed_client_does_not_break_broker(self):
        self.call("--broker-status")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.connect(str(Path(self.env["OP_BROKER_DIR"]) / "broker.sock"))
            c.sendall(b"X")
            c.shutdown(socket.SHUT_WR)
            self.assertTrue(c.recv(4096))
        self.assertEqual(self.call("context").returncode, 0)

    def test_nested_wrapper_does_not_deadlock(self):
        r = self.call("run", sys.executable, "-I", WRAPPER, "context")
        self.assertEqual(r.returncode, 0, r.stderr)
        context = json.loads(r.stdout)
        self.assertEqual(context["tty"], json.loads(self.call("context").stdout)["tty"])

    def test_fresh_pty_clients_also_share_terminal(self):
        results = []
        for _ in range(2):
            master, slave = pty.openpty()
            p = subprocess.Popen(
                [sys.executable, "-I", WRAPPER, "context"],
                env=self.env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
            )
            os.close(slave)
            data = b""
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                data += chunk
            os.close(master)
            p.wait(timeout=5)
            self.assertEqual(p.returncode, 0, data)
            self.assertTrue(data, "PTY client returned no output")
            results.append(json.loads(data))
        self.assertEqual(results[0]["sid"], results[1]["sid"])
        self.assertEqual(results[0]["tty"], results[1]["tty"])
        self.assertTrue(results[0]["stdin_tty"])

    def test_stubborn_descendants_cancel_and_control_stays_responsive(self):
        for action in ("client", "broker"):
            path = self.root / ("stubborn-" + action)
            p = subprocess.Popen(
                [sys.executable, "-I", WRAPPER, "stubborn", str(path)],
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            wait_for(path)
            ids = json.loads(path.read_text())
            start = time.monotonic()
            self.assertEqual(self.call("--broker-status").returncode, 0)
            self.assertLess(time.monotonic() - start, 2)
            if action == "client":
                p.terminate()
            else:
                self.assertEqual(self.call("--broker-stop").returncode, 0)
            p.communicate(timeout=5)
            for _ in range(100):
                if not any(alive(pid) for pid in ids):
                    break
                time.sleep(0.02)
            self.assertFalse(any(alive(pid) for pid in ids))
            time.sleep(0.1)

    def test_op_run_style_child_environment_and_cwd(self):
        self.env["TEST_VALUE"] = "propagated"
        code = 'import os;print(os.environ["TEST_VALUE"]);print(os.getcwd())'
        r = self.call("run", sys.executable, "-c", code, cwd=self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.decode().splitlines(), ["propagated", str(self.root)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
