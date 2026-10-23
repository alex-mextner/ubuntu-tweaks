# Spec: classic snaps in `packages.toml` (`snap_classic`)

Ticket: [#91](https://github.com/alex-mextner/ubuntu-tweaks/issues/91). Extends *`packages.toml`* of
`ubuntu-tweak-cli.md`; nothing else in that section changes.

## Why

A snap with classic confinement (VS Code `code`, `intellij-idea-*`, `go`, ...) is refused by plain
`snap install NAME` ("requires classic confinement", a flag the user must pass on purpose). The `snap`
array cannot therefore carry it. The migration audit of the retired laptop found VS Code installed as the
classic snap `code` (channel `latest/stable`); a fresh machine must get it from the profile.

## Key

```toml
snap = ["telegram-desktop"]
snap_classic = ["code"]      # installed with `snap install --classic`
```

- `snap_classic` is an array of non-empty strings, validated like `snap`: a name starting with `-` or empty
  is invalid config (exit 2, `packages.toml snap_classic: '--x' is not a package name`); unknown keys stay
  rejected.
- Shared and overlay lists **union** (shared order first), like every `packages.toml` list.
- Installed means a `snap list` row with that name, the same check as `snap`; classic confinement is not
  re-verified (the install either carried the flag or the user installed it by hand).

## Plan

One `package` action, **after** the `snap` action and before `flatpak`, root (`[sudo]`), described as
`snap --classic: install <missing names>`, run as `sudo -- snap install --classic <names>`. Nothing is
planned when every name is installed. It never removes or refreshes a snap.

## Coverage and capture

`snap:<name>` of a `snap_classic` entry is `tracked` (owned by `packages.toml`), exactly as a `snap`
entry. `capture` still proposes untracked snaps into `snap`; moving a classic one into `snap_classic` is a
manual edit (the classic flag is the `classic` word in the `Notes` column of `snap list`, which this change does not
parse; a proposal that puts a classic snap into `snap` is refused at the next `apply` by the conflict rule only when
the name is also in `snap_classic`, otherwise by snap itself). Follow-up, not in this change: have capture read the
`Notes` column and propose `snap_classic` for such snaps.

## Failure cases

- `snap` missing on the machine: the install fails with snap's own error (as for `snap`).
- A name listed in both `snap` and `snap_classic` (in one file or across the shared list and the overlay) is
  invalid config (exit 2): raised when `packages.toml` is loaded, so every command sees it (`... snap_classic: 'code' is also in snap; list it once, in snap_classic`).
  Nothing would succeed anyway: the plain install of a classic snap is refused by snap.

## Tests

`tests/test_planner_system.py` (`PackagesTests`): the classic batch (argv, description, convergence), the
union with the overlay and the invalid name; `tests/test_coverage_adapters.py` keeps owning coverage.
