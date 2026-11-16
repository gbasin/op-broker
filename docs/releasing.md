# Releases

The first releases are experimental. No workflow publishes a release automatically, and there is no automatic updater.

1. Update `op_broker.__version__` and CHANGELOG.md in a reviewed change. Keep code and version changes in the same commit.
2. Require a green CI run on Apple Silicon and Intel macOS with the minimum and current Python versions. CI uses a fake CLI and has no 1Password credentials.
3. Complete the manual checks below against that commit. Keep account-specific logs and screenshots private; record only sanitized outcomes in the release notes.
4. Create and push an annotated `vX.Y.Z` tag pointing to the checked commit. Do not move published tags.
5. Run **Actions → Draft release**, supplying the existing tag. It repeats the test matrix, verifies tag/version/commit agreement, archives only tracked source, writes SHA256SUMS, and creates a draft GitHub release.
6. Download the draft's artifact and SHA256SUMS. Check `shasum -a 256 -c SHA256SUMS`, extract into a fresh directory, and smoke-test installation, `op --broker-status`, and uninstall in a separate test user or isolated test paths.
7. Review the notes and manual results, then publish the draft in GitHub. This is the manual publication gate. Leave incomplete verification visible in a draft; do not publish by assuming CI covers Touch ID.

## Manual live checks

Use a private or disposable vault. Do not run lock tests while other work depends on an unlocked vault.

- Approve a cold broker start once, then run several independent processes and fresh PTY calls. Verify no additional approval for the same account within the native session.
- Authorize a second account separately; verify each account retains its own approval.
- Run `python3 live_test.py --account ACCOUNT --vault VAULT`. Verify its synthetic item is deleted. Recently Deleted remains under the product's normal retention policy.
- Lock 1Password. Verify the next broker command requests authorization and exposes no vault result before approval; unlock and authorize manually.
- Leave the broker idle for more than 10 minutes, without account CLI requests. Verify the next request requires renewed approval. Status inspection alone must not refresh authorization.
- Stop the broker; verify its dedicated tmux server exits and existing unrelated tmux sessions remain. Restart and approve again.

The 12-hour hard limit follows 1Password's native implementation; document whether it was directly observed rather than implying a short test covers it.
