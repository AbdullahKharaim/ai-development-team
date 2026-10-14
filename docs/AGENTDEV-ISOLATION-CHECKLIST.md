# `agentdev` WSL distribution — preparation checklist (for M2)

Purpose: give real Claude/Codex runs a disposable Linux environment that cannot see the
owner's Windows files, cannot launch Windows programs, and does not contain the owner's
existing Ubuntu home, keys or lab directories.

**Nothing in this document has been executed.** Every step is for the human owner to
perform and verify. Commands must be checked against the installed WSL version
(`wsl --help`) before use.

## 1. What this isolation does and does not give

| Gives (once verified) | Does not give |
|---|---|
| No `/mnt/c` (Windows drives not mounted) | Isolation between processes of the same Linux user inside agentdev |
| No Windows interop (`powershell.exe`, `cmd.exe`, `wsl.exe` unreachable from Linux) | Kernel isolation — all WSL2 distros share one VM and kernel |
| A separate root filesystem: no access to the main Ubuntu distro's `/home/<your-user>` | Network restrictions: the distro has normal outbound network |
| An ordinary account without sudo | Protection of the agents' own login tokens from tools they run |

Do not describe this as a hardened sandbox. It is a blast-radius reduction for accidents and
ordinary prompt injection on a personal machine.

## 2. Create a fresh distribution (human, PowerShell)

- Create it from a **fresh official Ubuntu image** — e.g. `wsl --install <Ubuntu distro> --name agentdev`
  if the installed WSL supports `--name`, otherwise `wsl --import agentdev <dir> <official rootfs.tar>`.
- **Do not** `wsl --export` the existing Ubuntu: that would copy `/home/<your-user>`, the existing CLI logins,
  SSH keys and all labs into the new distro.

## 3. Configure `/etc/wsl.conf` inside agentdev (as root, via `wsl -d agentdev -u root`)

```ini
[automount]
enabled = false
mountFsTab = false

[interop]
enabled = false
appendWindowsPath = false

[user]
default = dev
```

Then from PowerShell: `wsl --terminate agentdev` and start it again so the settings apply.
These settings are per-distribution; the main Ubuntu distro is unaffected.

## 4. Ordinary non-administrative account

- Create user `dev`; ensure it is **not** in `sudo` (or `admin`, `wheel`) groups; no NOPASSWD sudoers entries.
- Root remains reachable only from Windows via `wsl -d agentdev -u root` — acceptable because interop is
  disabled, so nothing inside the distro can call `wsl.exe`.

## 5. Verify the boundary (as `dev`, record the output)

| Check | Expected |
|---|---|
| `ls /mnt/c` ; `mount \| grep -Ei 'drvfs\|9p'` | no such directory / no Windows drive mounts |
| `cat /proc/sys/fs/binfmt_misc/WSLInterop` ; `/mnt/c/Windows/System32/cmd.exe /c ver` ; `powershell.exe -c 1` | absent / not found / fails |
| `echo $PATH` | no `/mnt/c/...` entries |
| `id` ; `sudo -n true` | no sudo group; sudo fails |
| `ls /home` ; `ls ~` | only `dev`; no keys, no lab directories |
| `ls /mnt/wsl` | inspect: shared across distros; must contain nothing sensitive |
| From the main Ubuntu: `ls /home/<your-user>` is not visible inside agentdev | confirmed |
| Network: `getent hosts api.anthropic.com` | resolves (network is intentionally available) |
| Windows-host services: note WSL networking mode (NAT/mirrored) | document which Windows localhost services are reachable |

Any unexpected result → stop and fix before installing the CLIs.

## 6. Native CLIs and subscription login (human only)

- Install Claude Code and Codex CLI inside agentdev with their official installers, as `dev`.
- The human runs the interactive logins (Claude subscription login; `codex login`, device flow if needed).
  No agent, script or orchestrator handles these steps or reads the resulting token files.
- Pin and record versions (`claude --version`, `codex --version`).
- Install Python/git as needed for the orchestrator and target project (via the human, using root once).

## 7. Native sandbox behaviour checks (first real, human-authorized runs — M2 setup)

These require real inference and therefore explicit approval. Run them in a scratch git repo:

- **Codex** `codex exec --sandbox workspace-write --cd <scratch>`: ask it to (a) write a file in
  `<scratch>` — expected to succeed; (b) write `~/outside.txt` — expected to be denied; (c) fetch a URL
  with `curl` — record whether network is denied in this mode on the installed version.
  Never use `--dangerously-bypass-approvals-and-sandbox` or `--approve-for-me`.
- **Claude** `claude -p --allowedTools "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*)"`: ask it to edit a
  file — expected refusal; confirm with `git status` (the orchestrator check also detects it).
- Record exact commands, versions and outcomes in a short evidence file. Behaviour that differs from the
  expectation is a finding, not something to work around.

## 8. Residual risks to disclose

- Agents can read their own CLI's credentials (`~/.claude`, `~/.codex`). A prompt-injected agent with
  network access could exfiltrate them. Mitigation: only owner-authored repos and tasks; revoke/re-login
  if in doubt; Codex network off in workspace-write (to be verified).
- Claude has network access for its own API; its reviewer tool allowlist excludes web tools and general Bash.
- Anything the `dev` user can write inside agentdev can be modified by a misbehaving agent; the
  orchestrator only detects changes inside the task worktree. Keep nothing valuable in agentdev outside git.
- Subscription quota consumption; bounded by max 2 cycles and the task timeout.
- WSL2 shares a kernel across distros; kernel-level escapes are out of scope.
