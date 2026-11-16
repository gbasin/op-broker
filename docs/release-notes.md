Initial experimental macOS release of op-broker.

Independent coding-agent processes share one persistent native 1Password CLI terminal authorization. Standard streams are passed by file descriptor, without a credential cache or service-account token.

Read SECURITY.md before installing: approval is intentionally shared with processes running as your local OS user. Fully interactive `/dev/tty` workflows use `op-native`.

This release is a source archive requiring Python 3.11+, tmux, and the signed 1Password CLI. SHA256SUMS covers the uploaded archive. No automatic updater is included.

Maintainer: complete docs/releasing.md's manual verification checklist before publishing this draft.
