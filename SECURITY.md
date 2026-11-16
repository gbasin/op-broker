# Security model

This is an independent, experimental wrapper, not a 1Password product. It intentionally broadens terminal-scoped CLI authorization to processes running as the same local OS user. It is not a sandbox or an agent permission system.

Once an account is authorized in the broker terminal, every process able to access the broker's owner-only socket can exercise that account's native CLI authority. This includes `op run`, which can execute local commands. Peer UID checks and mode 0700/0600 paths exclude other OS users; they do not distinguish trusted agents from other software under your UID. Do not use shared user accounts or expose/forward the socket.

The broker does not introduce a service-account token, persist credentials or CLI output, retry executed requests, or refresh authorization on an idle timer. Native account permissions and 1Password authorization expiration/revocation remain authoritative. The native CLI and desktop app must remain installed and trusted.

Arguments, environment and open standard-stream descriptors travel through the local socket and process memory. Standard input/output/error are passed as descriptors rather than stored in temporary files or relayed through logs. Python cannot guarantee secure memory erasure. Unexpected `/dev/tty` output can remain in tmux's in-memory pane history; automated commands should use standard streams. The tool is not designed to hide credentials from software already running as your user.

Cancellation stops the request's process group, but a write may already have completed. The result can be unknown; inspect the account before manually retrying. Descendants that deliberately create another session/process group are outside the cleanup guarantee. Full interactive terminal workflows should use `op-native`.

## Reporting a vulnerability

Use the repository's private vulnerability reporting form under **Security → Report a vulnerability**. Do not post credentials, account identifiers, vault contents, or private command output in public issues. If private reporting is unavailable, open an issue asking for a private contact without including exploit details or sensitive data.
