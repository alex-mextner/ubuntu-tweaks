# Spec: `apt-sources` module (declarative apt repositories)

Ticket #84. Parent spec: `docs/specs/ubuntu-tweak-cli.md` (*Modules*, *Service modules*, *Reconcile and failover*,
*Coverage*). Kind `system`, root, one Python script `modules/apt-sources/apt-sources` on the shared scaffolding
(`ubuntu_tweaks/module_script.py`).

## What it does

The user's extra apt repositories are **profile data**: `[modules.apt-sources]` in `profile.toml` / `machine.toml`
lists them, the engine module stays generic. For each source the module makes true:

| File | Content |
|---|---|
| `/etc/apt/sources.list.d/<name>.sources` | one deb822 stanza, `Signed-By: /etc/apt/keyrings/<name>.<asc\|gpg>` |
| `/etc/apt/keyrings/<name>.asc` or `.gpg` | the repository signing key, **only** after its fingerprint matched the pinned one |
| `/etc/apt/preferences.d/<name>` | the optional `pin` text (e.g. Mozilla's deb Firefox beating the snap shim) |

Never `apt-key`, never `/etc/apt/trusted.gpg*`: a key is trusted for its one repository through `Signed-By`.
Managed files start with `# Managed by ubuntu-tweaks (apt-sources)`; that marker is how the module knows a file is
its own.

## Options

Two equivalent shapes. The **table form** merges key by key across `profile.toml` and a machine overlay (so
`machine.toml` can say `[modules.apt-sources.sources.docker] enabled = false` and nothing else); the **list form**
(`sources = [{ name = "docker", ... }]`) replaces the whole list when a second layer sets it.

```toml
[modules.apt-sources.sources.mozilla]
uris = ["https://packages.mozilla.org/apt"]
suites = ["mozilla"]
components = ["main"]
architectures = ["amd64"]          # optional
types = ["deb"]                    # optional, default ["deb"]
key_url = "https://packages.mozilla.org/apt/repo-signing-key.gpg"
key_urls = ["https://mirror.example/mozilla.asc"]   # optional mirrors, tried after key_url, in order
key_cached = """-----BEGIN PGP PUBLIC KEY BLOCK-----..."""  # optional copy kept in the profile, tried last
key_format = "asc"                 # asc (default): store armored; gpg: store binary (dearmored if needed)
fingerprint = "35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3"   # REQUIRED while enabled
pin = """Package: firefox*\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n"""   # optional
enabled = true                     # false: the source is declared but absent from the machine
```

- `name` (list form; the table key in table form): `[a-z0-9][a-z0-9._-]*`, becomes the file names.
- `uris`, `suites` non-empty lists; `components` non-empty unless every suite ends in `/` (a flat repository).
  `architectures` and `types` optional. URIs are `http(s)://`; **key URLs must be `https://`**.
- `fingerprint` is the 40-hex (or 64-hex, v6 keys) **primary key** fingerprint (spaces and case ignored), a string or a list for a repo
  that signs with several primaries. It is REQUIRED for an enabled source, so a repository can never be added
  without a pinned key. An unknown option, a bad type or a missing field is exit 1 naming the source.
- Options are never secrets (`UBUNTU_TWEAK_MODULE_CONFIG` shows in `ps`); keys are public.

## Key verification

Every candidate key (download or cached copy) is read with `gpg --homedir <temp> --show-keys --with-colons` and
accepted only when **the set of primary-key fingerprints in the file equals the pinned set**: a missing pinned key
and an *extra* primary key (apt would trust it for the repository too) are both refused. Only then is the key
converted to `key_format` (armor to binary with `gpg --dearmor`; an armored key is never produced from binary) and
**verified again in its final form** before the atomic write (temp file + rename, mode 0644). A mismatch is a hard
failure for that candidate, reported as `fingerprint mismatch: wanted X, got Y`, never installed.

## Failover

Key candidates are tried in order: `key_url`, each of `key_urls`, then `key_cached`. The first that downloads **and**
verifies wins; `status` and the install output say which one (`key from key_urls[1]`). Redirects must stay on
`https://` (`--proto-redir =https`). Network causes (curl exit 5, 6, 7, 28, 35, 52, 55, 56, the 60-second watchdog,
HTTP 429/502/503/504) are *transient*; a mismatch, HTTP 404, an unreadable key are
*permanent*. A source whose candidates all failed for transient causes only makes the install exit **75**
(`EX_TEMPFAIL`; the system-module planner turns an installer exit 75 into a `Transient`, which the engine retries
with backoff: `tests/test_system_modules.py`); any permanent cause makes it exit 1. A source may have only `key_cached` (a vendor that publishes no key URL: the pin is then only as independent as the
copy's provenance, which the profile's comment states). A key already installed and
matching the pin is **never downloaded again**, so a converged machine works offline. Root unavailable is the
engine's *root remainder* (the exact `sudo` command is printed); there is no user-level alternative to `/etc/apt`.

One source never blocks another: each source ends in a result line (`ok`, `unchanged`, `FAILED <name>: <cause>`),
the other sources are still written, and the exit code is non-zero at the end naming every failed source.

## Migration of hand-written files

A file in `/etc/apt/sources.list.d/` that is not the managed file and declares the **same repository** (a one-line
`.list` entry or a deb822 `.sources` stanza with an intersecting normalized URI + suite) would make apt warn about
duplicate targets. The module migrates it: the file is copied to `/var/backups/ubuntu-tweaks/apt-sources/<UTC
timestamp>/<file>` and removed, and the managed `.sources` takes over. `plan` shows each migration. A file shared by
several declared sources is removed only after **every** one of them converged (until then it is the only
declaration of the repository whose key could not be fetched: the output says `kept <file>: waits for <name>`). A file
that also declares *other* repositories (or `/etc/apt/sources.list` itself) is **not** edited: it is a `manual` finding
with the file name. A foreign file with the managed name itself (`1password.sources` written by the 1Password package) is
backed up the same way and replaced. A foreign pin file with the managed name is backed up and replaced. Key files
the migrated entry referenced are left where they are.

Sources the module does not manage (`ubuntu.sources`, a hand-added repository nobody declared) are left alone and
listed by `status` as `unmanaged`.
A file that carries the module's marker but belongs to a source the profile no longer declares (the entry was deleted
instead of set to `enabled = false`) is a `manual` finding: the module proves it owns the file, yet never removes a
repository because a line vanished from the profile. Declare the source again, or declare it with `enabled = false`. The key and pin files of a dropped source stay
where they are until it is declared (`enabled = false` removes them with the source file).

## `enabled = false`

The source is declared, with the reason in the profile, but wanted **absent**. If the module's own marked
`<name>.sources` exists (a previous run, or the source was enabled before), it is backed up and removed together
with its key and pin; foreign files are never touched.

## Commands

| Command (alias) | Contract |
|---|---|
| `check` (`--check`) | Read-only, no root, no network. Exit 0 matches, 3 drift (unmet facts printed), other = crashed. |
| `status` | The same facts, all lines, plus `unmanaged` entries. |
| `plan` (`--dry-run`) | Read-only: the exact steps, a unified diff for every text file, the key fingerprint and candidate order, each migration and backup, the `apt-get update` that will run. Prints `nothing to change` when converged. |
| `install` | Root. Per source: key, then `.sources`, then pin (a source without its key is never written); then `apt-get update` for the changed sources. |

`apt-get update` runs **only when something was written**, and only for the changed sources
(`-o Dir::Etc::sourcelist=sources.list.d/<name>.sources -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0`), so
one broken unrelated repository cannot fail this module. Sources whose index refresh did not succeed are listed in
`/var/lib/ubuntu-tweaks/apt-sources/pending-update` and are **drift** for `check` until a later `install` refreshed
them: the retry after a transient failure still refreshes the index.

## Staging and tests

`APT_SOURCES_ROOT=DIR` points `check`, `status`, `plan` and `install` at a staged root (every path below `DIR`,
no root needed, `apt-get` still goes through `PATH` so a test puts a double there); under sudo the variable is
dropped by `env_reset`, so it can never redirect a real install. Tests (`modules/apt-sources/tests`) use a generated
GPG key (never a vendor key), a fake `curl` / `apt-get` on `PATH` and the real `gpg`.

## Coverage

`modules/apt-sources/module.toml` claims `etc:/etc/apt/keyrings/*` as `tracked`. The managed source names come from the
profile, so the profile declares `etc:/etc/apt/sources.list.d/<name>.sources` for each of its sources in its own
`coverage.toml`; a hand-written source nobody declared stays an `untracked` finding, which is the point.

## Real-root verification

(What staged tests cannot show.) As the desktop user: `ubuntu-tweak apply --dry-run --only apt-sources` shows the
diffs; then, in a terminal, `--commit`; `apt-get update` shows the repos without a `Key is stored in legacy`
or duplicate-target warning; `ubuntu-tweak status --only apt-sources` exits 0; a second `--commit` says `0 changed`.

Verified without root on 2026-10-23 (not a substitute for the above): with the profile's nine sources staged under
`APT_SOURCES_ROOT`, the real vendor key URLs were fetched and every key matched its pinned fingerprint
(1Password, ChatGPT from the cached copy, Claude Desktop, Docker, Chrome, Mozilla, NVIDIA container toolkit, ProtonVPN,
Tailscale); real `apt-get update` with the single-source `-o Dir::Etc::sourcelist=<file>` options accepted each
generated deb822 file and verified its `InRelease` with the stored key.
