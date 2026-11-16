#!/usr/bin/env python3
"""Share 1Password CLI's terminal identity, never cache credentials or output."""

import array
import ctypes
import fcntl
import json
import os
from pathlib import Path
import shlex
import select
import signal
import socket
import stat
import struct
import shutil
import subprocess
import sys
import time

__version__ = "0.1.0"


def find_real_op():
    """Find the signed native CLI without resolving this wrapper recursively."""
    selected = os.environ.get("OP_BROKER_REAL")
    if selected:
        return selected
    for candidate in ("/opt/homebrew/bin/op", "/usr/local/bin/op"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("native op not found; set OP_BROKER_REAL to its absolute path")


def find_tmux():
    candidate = shutil.which("tmux")
    if not candidate:
        raise RuntimeError("tmux not found; install tmux and add it to PATH")
    return str(Path(candidate).resolve())


MAX_REQUEST = 2 * 1024 * 1024


def peer_uid(sock):
    if sys.platform == "darwin":
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
            raise OSError(ctypes.get_errno(), "getpeereid failed")
        return uid.value
    return struct.unpack(
        "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    )[1]


def secure_dir(path):
    path = Path(path)
    # Reject symlink components, including an otherwise secure final directory.
    for component in [*reversed(path.parents), path]:
        if component.is_symlink():
            raise RuntimeError("broker directory contains a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise RuntimeError("broker directory must be owned by you and mode 0700")
    return path


def exact(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError(
                "broker connection ended; command outcome may be unknown; not retried"
            )
        data.extend(chunk)
    return bytes(data)


def send_json(sock, value):
    data = json.dumps(value, ensure_ascii=True).encode()
    if len(data) > MAX_REQUEST:
        raise ValueError("request too large")
    sock.sendall(struct.pack("!I", len(data)) + data)


def recv_json(sock):
    size = struct.unpack("!I", exact(sock, 4))[0]
    if size > MAX_REQUEST:
        raise ValueError("request too large")
    return json.loads(exact(sock, size))


def receive_fds(conn):
    data, ancillary, flags, _ = conn.recvmsg(
        1, socket.CMSG_SPACE(16 * array.array("i").itemsize)
    )
    fds = []
    try:
        for level, kind, payload in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                values = array.array("i")
                values.frombytes(
                    payload[: len(payload) - len(payload) % values.itemsize]
                )
                fds.extend(values)
        if data != b"F" or flags & socket.MSG_CTRUNC or len(fds) != 3:
            raise ValueError("expected three standard-stream descriptors")
        for fd in fds:
            os.set_inheritable(fd, False)
        return fds
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


def signal_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def execute(conn, request, fds, real, tty_fd, server, accept_request):
    args, env, cwd = request["args"], request["env"], request["cwd"]
    if not isinstance(args, list) or not all(
        isinstance(x, str) and "\0" not in x for x in args
    ):
        raise ValueError("invalid arguments")
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and "\0" not in k + v and "=" not in k
        for k, v in env.items()
    ):
        raise ValueError("invalid environment")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ValueError("invalid working directory")
    # A queued caller may have died or cancelled before its turn. Never start it.
    if select.select([conn], [], [], 0)[0]:
        control = conn.recv(64)
        if not control:
            return 143
        if any(n in (2, 3, 15, 1) for n in control):
            return -next(n for n in control if n in (2, 3, 15, 1))
    # Nested op run/plugin calls already inherit this controlling terminal.
    # Re-entering the serialized socket would deadlock behind their own parent.
    env["OP_BROKER_BYPASS"] = "1"
    env["OP_BROKER_REAL"] = real
    gate_r, gate_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(gate_w)
            os.setpgid(0, 0)
            if os.read(gate_r, 1) != b"G":
                os._exit(125)
            os.close(gate_r)
            for sig in (
                signal.SIGINT,
                signal.SIGTERM,
                signal.SIGHUP,
                signal.SIGQUIT,
                signal.SIGPIPE,
                signal.SIGTTOU,
                signal.SIGTTIN,
            ):
                signal.signal(sig, signal.SIG_DFL)
            for target, fd in enumerate(fds):
                os.dup2(fd, target)
            os.chdir(cwd)
            os.execve(real, [real, *args], env)
        except BaseException:
            os.write(2, b"op-broker: unable to execute real CLI\n")
            os._exit(126)
    os.close(gate_r)
    cancel_at = None
    disconnected = False
    reaped = False
    try:
        os.setpgid(pid, pid)
        os.tcsetpgrp(tty_fd, pid)
        os.write(gate_w, b"G")
        os.close(gate_w)
        gate_w = -1
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                reaped = True
                return os.waitstatus_to_exitcode(status)
            if disconnected:
                if time.monotonic() - cancel_at > 1:
                    signal_group(pid, signal.SIGKILL)
                time.sleep(0.02)
                continue
            readable, _, _ = select.select([conn, server], [], [], 0.05)
            if server in readable:
                accept_request()
            if conn in readable:
                data = conn.recv(64)
                if not data:
                    disconnected = True
                    cancel_at = time.monotonic()
                    signal_group(pid, signal.SIGTERM)
                else:
                    for number in data:
                        if number in (
                            signal.SIGINT,
                            signal.SIGTERM,
                            signal.SIGHUP,
                            signal.SIGQUIT,
                            signal.SIGWINCH,
                        ):
                            signal_group(pid, number)
                            if number != signal.SIGWINCH:
                                disconnected = True
                                cancel_at = time.monotonic()
    finally:
        if gate_w >= 0:
            os.close(gate_w)
        # Kill/reap on every exceptional path, and clean up op run descendants.
        signal_group(pid, signal.SIGTERM)
        deadline = time.monotonic() + 1
        while True:
            if not reaped:
                try:
                    done, _ = os.waitpid(pid, os.WNOHANG)
                    reaped = bool(done)
                except ChildProcessError:
                    reaped = True
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                signal_group(pid, signal.SIGKILL)
                break
            time.sleep(0.02)
        if not reaped:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        try:
            os.tcsetpgrp(tty_fd, os.getpgrp())
        except OSError:
            pass


def worker(directory, real):
    os.umask(0o077)
    directory = secure_dir(directory)
    lock_fd = open_lock(directory / "broker.lock")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return
    path = directory / "broker.sock"
    if path.exists() or path.is_symlink():
        st = path.lstat()
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
            raise RuntimeError("unexpected broker socket path")
        path.unlink()
    # A dedicated tmux server owns the controlling PTY and gives 1Password a
    # recognized, persistent application ancestor for visible approval dialogs.
    tty_fd = os.dup(0)
    os.set_inheritable(tty_fd, False)
    for fd in (0, 1, 2):
        null = os.open(os.devnull, os.O_RDWR)
        os.dup2(null, fd)
        os.close(null)
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    signal.signal(signal.SIGTTIN, signal.SIG_IGN)
    path = Path(directory) / "broker.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(64)

    def shutdown(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGHUP, shutdown)
    pending = []

    def close_request(conn, fds):
        conn.close()
        for fd in fds:
            os.close(fd)

    def accept_request():
        conn, _ = server.accept()
        fds = []
        keep = False
        try:
            conn.settimeout(5)
            if peer_uid(conn) != os.getuid():
                raise PermissionError("wrong socket peer")
            fds = receive_fds(conn)
            request = recv_json(conn)
            conn.settimeout(None)
            method = request.get("method", "exec")
            if method == "status":
                send_json(
                    conn,
                    {
                        "pid": os.getpid(),
                        "tty": os.ttyname(tty_fd),
                        "real": real,
                        "queued": len(pending),
                        "version": __version__,
                    },
                )
            elif method == "stop":
                send_json(conn, {"stopped": True})
                raise SystemExit(0)
            elif method == "exec" and len(pending) < 64:
                pending.append((conn, request, fds))
                keep = True
            else:
                raise ValueError("unknown method or queue full")
        except Exception:
            try:
                send_json(
                    conn, {"error": "broker rejected request; CLI was not started"}
                )
            except OSError:
                pass
        finally:
            if not keep:
                close_request(conn, fds)

    try:
        while True:
            if not pending:
                ready, _, _ = select.select([server], [], [])
                if server in ready:
                    accept_request()
                continue
            conn, request, fds = pending.pop(0)
            try:
                rc = execute(conn, request, fds, real, tty_fd, server, accept_request)
                send_json(conn, {"exit": rc})
            except Exception:
                try:
                    send_json(
                        conn,
                        {
                            "error": "broker request failed; command outcome may be unknown; not retried"
                        },
                    )
                except OSError:
                    pass
            finally:
                close_request(conn, fds)
                del request
    finally:
        for conn, _, fds in pending:
            close_request(conn, fds)
        server.close()
        path.unlink(missing_ok=True)
        os.close(tty_fd)
        os.close(lock_fd)


def clean_daemon_env():
    return {
        key: os.environ[key]
        for key in ("HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL")
        if key in os.environ
    }


def open_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        os.close(fd)
        raise RuntimeError("unsafe broker lock")
    os.set_inheritable(fd, False)
    return fd


def launch(directory, real):
    fd = open_lock(directory / "startup.lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        path = directory / "broker.sock"
        if path.exists() and not path.is_symlink():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(path))
                return
            except ConnectionRefusedError:
                pass
            finally:
                probe.close()
        command = "exec " + shlex.join(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "__worker",
                str(directory),
                real,
            ]
        )
        r = subprocess.run(
            [
                find_tmux(),
                "-S",
                str(directory / "terminal.sock"),
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "op-broker",
                "-x",
                "100",
                "-y",
                "30",
                command,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=clean_daemon_env(),
            timeout=5,
        )
        if r.returncode:
            raise RuntimeError("dedicated terminal startup failed")
        for _ in range(100):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(path))
                return
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.02)
            finally:
                probe.close()
        raise RuntimeError("broker terminal did not become ready")
    finally:
        os.close(fd)


def connect(directory, real, autostart=True):
    directory = secure_dir(directory)
    path = directory / "broker.sock"

    def attempt():
        if path.is_symlink():
            raise RuntimeError("socket is a symlink")
        st = path.lstat()
        if (
            not stat.S_ISSOCK(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & 0o077
        ):
            raise RuntimeError("unsafe broker socket")
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(str(path))
            if peer_uid(conn) != os.getuid():
                raise RuntimeError("wrong broker peer")
            return conn
        except BaseException:
            conn.close()
            raise

    try:
        return attempt()
    except (FileNotFoundError, ConnectionRefusedError):
        if not autostart:
            raise RuntimeError("broker is not running") from None
    # No request has been sent: startup retries cannot duplicate a CLI operation.
    bootstrap = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(Path(__file__).resolve()),
            "__launch",
            str(directory),
            real,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=clean_daemon_env(),
    )
    for _ in range(100):
        try:
            return attempt()
        except (FileNotFoundError, ConnectionRefusedError):
            if bootstrap.poll() not in (None, 0):
                raise RuntimeError("broker failed to start")
            time.sleep(0.03)
    raise RuntimeError("broker startup timed out")


def client(args, directory, real, method="exec"):
    request = {"method": method}
    if method == "exec":
        request.update(args=args, cwd=os.getcwd(), env=dict(os.environ))
        # Keep explicit service-account/manual/Connect auth settings intact.
        # No authorization tokens are introduced or stored by the broker.
    with connect(directory, real, autostart=method != "stop") as conn:
        conn.sendmsg(
            [b"F"],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [0, 1, 2]))],
        )
        send_json(conn, request)
        old_handlers = {}

        def forward(number, _):
            try:
                conn.sendall(bytes([number]))
            except OSError:
                pass

        if method == "exec":
            for sig in (
                signal.SIGINT,
                signal.SIGTERM,
                signal.SIGHUP,
                signal.SIGQUIT,
                signal.SIGWINCH,
            ):
                old_handlers[sig] = signal.signal(sig, forward)
        try:
            result = recv_json(conn)
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
    if "error" in result:
        raise RuntimeError(result["error"])
    if method != "exec":
        print(json.dumps(result))
        return 0
    code = result["exit"]
    if code < 0:
        signal.signal(-code, signal.SIG_DFL)
        os.kill(os.getpid(), -code)
    return code


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "__worker":
        worker(sys.argv[2], sys.argv[3])
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "__launch":
        launch(secure_dir(sys.argv[2]), sys.argv[3])
        return 0
    directory = Path(
        os.environ.get(
            "OP_BROKER_DIR", str(Path.home() / "Library/Application Support/op-broker")
        )
    )
    real = find_real_op()
    if not os.path.isabs(real) or Path(real).resolve() == Path(__file__).resolve():
        raise RuntimeError(
            "real CLI must be an absolute executable path, different from wrapper"
        )
    args = sys.argv[1:]
    if args == ["--broker-version"]:
        print(__version__)
        return 0
    if args[:1] == ["--broker-status"]:
        return client([], directory, real, "status")
    if args[:1] == ["--broker-stop"]:
        return client([], directory, real, "stop")
    # PTY-backed agent calls must also share approval. Native interactive escape
    # hatch is explicit; credentialed automation alternatives remain native.
    if (
        any(os.environ.get(k) for k in ("OP_SERVICE_ACCOUNT_TOKEN", "OP_CONNECT_HOST"))
        or os.environ.get("OP_BROKER_BYPASS") == "1"
    ):
        os.execv(real, [real, *args])
    return client(args, directory, real)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Never interpolate request data or arbitrary OSError paths/arguments.
        if isinstance(error, (RuntimeError, ConnectionError)):
            message = str(error)
        else:
            message = type(error).__name__
        print("op-broker: " + message, file=sys.stderr)
        sys.exit(125)
