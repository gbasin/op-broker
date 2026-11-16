# op-broker

Share one 1Password CLI authorization across independent agent shells and subprocesses on macOS.

Coding agents often start a fresh terminal for each command. `op-broker` sends those commands through one persistent terminal, so an approval can cover subsequent commands in the same account. The original signed `op` executable still performs every operation.

**Experimental, macOS only.** Approval is shared with other processes running as your local OS user. Read [SECURITY.md](SECURITY.md) before installing. This is an independent project, not a 1Password product.

## Requirements

- Python 3.11 or later.
- `tmux` on PATH.
- The signed [1Password CLI](https://www.1password.dev/cli/get-started) and desktop app, with CLI integration enabled.

The broker uses a dedicated tmux server with its own socket. It does not alter your existing tmux sessions.

## Install

Clone this repository or download and verify a release archive:

```sh
git clone https://github.com/gbasin/op-broker.git
cd op-broker
python3 install.py
```

The installer discovers `op` in `/opt/homebrew/bin` or `/usr/local/bin`. For another location, pass `--real-op /absolute/path/to/op`.

Put the wrapper before the original CLI on PATH in the environment used by your agents:

```sh
export PATH="$HOME/.local/bin:$PATH"
command -v op
op --broker-status
```

The installer does not edit shell startup files. Existing shells may need `rehash`; already-running agents may need a new session to inherit PATH changes. Absolute calls to the native CLI bypass the wrapper.

## Use

Keep calling `op` normally. The first account request presents the native Touch ID approval for the dedicated tmux terminal. Later processes reuse that terminal's account authorization:

```sh
op vault list --account example.1password.com
op item get 'Example item' --account example.1password.com
```

Status does not make a vault request or refresh authorization:

```sh
op --broker-status
op --broker-version   # wrapper version
op --version          # native 1Password CLI version
```

Accounts require separate approvals. The broker does not poll to keep authorization alive. [1Password's native rules](https://www.1password.dev/cli/app-integration-security) govern expiration and app-lock revocation.

For interactive workflows that use `/dev/tty`, call `op-native` instead. `OP_BROKER_BYPASS=1` also selects native execution. Service-account and Connect environments retain native execution.

## What passes through

The broker passes argv, environment, working directory, and the caller's actual stdin/stdout/stderr file descriptors. JSON stdin, binary output, pipes and captured stderr stay separate. Nested `op run` children invoke the native CLI inside the same terminal instead of waiting on their own queued request.

Requests execute serially. A long-running `op run` delays other requests; use native execution for long interactive work. Status and stop remain responsive. Cancelled queued requests are skipped. Executed requests are never automatically retried: an interrupted write may have completed even if its result was lost.

The socket directory is mode 0700 and the socket is mode 0600. Peer UID checks exclude other OS users. No service-account token is introduced, and CLI output and credentials are not written to a cache or log. See SECURITY.md for process-memory and terminal-buffer limits.

## Stop, update or remove

```sh
op --broker-stop
op-broker-uninstall
```

Stop cancels active requests and ends the dedicated terminal; the next call starts a new one and needs approval. Stop the broker before installing an update so its next startup uses the updated worker.

The installer records hashes and refuses to overwrite or remove modified managed files. It installs code in `~/.local/lib/op-broker` and launchers in `~/.local/bin`. The original CLI is preserved. Uninstall may leave the empty owner-only state directory and lock files under `~/Library/Application Support/op-broker`.

## Tests

Automated tests use a fake CLI and require no account or credentials:

```sh
python3 -m unittest -v test_broker test_install test_release
```

The opt-in live test creates a synthetic Secure Note in the account and vault you select, checks JSON stdin, reads it, injects its value through `op run`, then deletes it in a `finally` block:

```sh
python3 live_test.py --account example.1password.com --vault 'Private test'
```

Use a private or disposable vault. Deleted test items remain recoverable in Recently Deleted. Do not include account-specific test output in public issues.

CI covers Apple Silicon and Intel macOS with Python 3.11 and 3.15. Touch ID, app-lock and inactivity checks require a real desktop session; CI does not establish those results. [The release checklist](docs/releasing.md) keeps those manual checks explicit. Release automation creates checksummed **drafts**; publication is manual. There is no automatic updater.

## License

MIT. See [LICENSE](LICENSE).
