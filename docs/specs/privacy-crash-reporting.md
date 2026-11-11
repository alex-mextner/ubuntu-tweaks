# Spec: the `privacy-crash-reporting` module

Source of truth for `modules/privacy-crash-reporting/`. Expresses "Ubuntu's crash upload is disabled" as a declared
state (ticket #178). **Opt-in, off everywhere**: the module's default is `enabled = false`, and no profile enables it
until its owner decides; a machine opts in with `[modules.privacy-crash-reporting] enabled = true`.

## Contract

`kind = "system"`, `root = true`; the checker protocol of `ubuntu-tweak-cli.md` (*Source kinds*): `privacy-crash-reporting
check|status|plan|install` (exit 0 satisfied, 3 drift). `check`, `status` and `plan` are read-only and need no root;
`install` runs through sudo only with `apply --commit`.

| Fact | Read with |
|---|---|
| `whoopsie.path` and `whoopsie.service` are masked | `systemctl is-enabled` answers `masked` (persistent, in `/etc/systemd/system`); a unit that does not exist (`not-found`) counts as satisfied. `masked-runtime` (gone after a reboot), `disabled`, `static`, `enabled` and every other word are drift |
| neither unit is running | `systemctl is-active` (`active`, `activating`, `reloading` are drift, a masked unit included) |
| `/etc/default/apport` has `enabled=0` (only with `apport = true`) | file read, the last `enabled=` line decides |

`whoopsie.path` is what starts whoopsie when a crash report appears, and `whoopsie.service` is `static` (it has no
`[Install]` section), so disabling the *service* alone changes nothing: both units are checked. A unit that is already
masked and stopped is left out of the plan. Install: `systemctl mask --now <unit>` for each unit that is not masked or
is running (it stops the unit and links it to `/dev/null`); with the option, the last `enabled=` line of
`/etc/default/apport` becomes `enabled=0` (a missing line or file is added; other lines and comments are kept).

*Decision (#196): mask, not just disable.* `apport-autoreport.service` (started by apport's autoreport timer/path when
automatic reporting is on) `Wants=` `whoopsie.path`, so a unit that is merely disabled can be started again between
two applies and upload crash reports in the gap. A masked unit cannot be started by anything (systemd refuses the
start job and drops the failed `Wants=`). Mask does not remove an existing enablement link: the links stay beneath the
mask, so `systemctl unmask` restores the earlier state and nothing else of the system changes. Cost: unmasking by hand
is drift for as long as the module is enabled. Test: `test_a_unit_a_service_wants_stays_masked_after_the_apply_so_it_cannot_be_started_again`.

## Options (`[modules.privacy-crash-reporting]`)

| Option | Default | Meaning |
|---|---|---|
| `apport` | `false` | also disable apport (`enabled=0`). Off by default: apport only writes reports to `/var/crash`; whoopsie is what uploads them |

A systemctl query that fails (no systemctl, a timeout, a refused call, an answer outside systemd's words) is an error with exit 1 naming the unit, never a pass. Any other option is refused (exit 1). `PRIVACY_CRASH_REPORTING_ROOT=DIR` prefixes `/etc/default/apport` for tests;
`systemctl` is found on `PATH`, so tests put a fake one first.

## Coverage

`/etc/default/apport` is `tracked` by this module's manifest.
