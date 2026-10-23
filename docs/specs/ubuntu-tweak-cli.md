# Spec: ubuntu-tweaks umbrella and the `ubuntu-tweak` CLI

Source of truth for how this repo installs, configures and updates its tweaks, and for the
Ubuntu settings profile it applies. Every user request is recorded verbatim under *Requests*;
*Requirements* is what they mean for the implementation. Change this file in the same commit
as any behavior change.

## Requests (verbatim, in order)

1. «сделай этот репозиторий зонтиком для других которые отдельные. добавь их сюда как
   субмодули думаю и сделай ubuntu-tweak init (cli) и другие команды чтобы все легко было
   установить на новой системе, настроить, обновить на существующией. напимер сюда же док,
   я его переименовал в hyperdock, обзор приложений и окон текущего приложения и тд.
   возможно стоит сделать реконциляцию как в http://git.hyperide.ai/ultrabricks/rig-cli»
2. «Обзор приложений это встроенная функция убунту, а окна вроде начаты в omp сессии про
   док, надо поставить задачу потом все поселить под зонтиком» — the app grid is GNOME's
   own; the per-app window overview is hyperdock's `AppSpread`
   ([#17](https://github.com/alex-mextner/ubuntu-tweaks/issues/17)).
3. «B» — CLI in Python, rig-cli style reconcile.
4. «A …» — hyperdock gets its own uuid/schema
   ([hyperdock#3](https://github.com/alex-mextner/hyperdock/issues/3)).
5. «про вопросам пока B, но с архитектунрой возможности добавить в будущем C» — modules
   cover extensions and built-in GNOME settings now; extensions.gnome.org later.
6. «обновления должны происходить автоматически бесшовно по релизу (должно
   настраиваться)» ([#19](https://github.com/alex-mextner/ubuntu-tweaks/issues/19)).
7. «у меня есть на гх .files. давай там сделаем поддиректорию ubuntu. и пусть все что я
   настраиваю помимо дотфайлов тоже там хранится. так же надо отдельные .config файлы там
   хранить а в .config держить симлинки. и нужна система развертывания всего этого добра.
   и перед тем как включим хочу увидеть что попадет в конфиг. так же ubuntu-tweaks должен
   тоже применятся автоматически. мб под него все и завернуть? я там просил сделать cli,
   если не доделано доделай. какой из проектов взять за основу сам подумай. я так понимаю
   как минимум надо все что в Settings настравается в системе. часть должно быть
   индивидуально для машины»
8. «доделывай все. и покажи сводку что попадет на capture --dry-run. и мне важно чтобы
   попадали только измененные значения а не все включая дефолты. и надо чтобы можно было
   комменты с причинами настройки указывать. и еще я хочу иметь возможность группировать
   настройки из разных мест в файлы по назначениям чтобы потом они мержились. в ha есть
   подобная концепция, кажется packages называется» — answered by `capture --dry-run` with a
   SUMMARY block, the changed-only rule and the `auto` skip class, reasons as comments, and
   *bundles* (see *Settings bundles*).
9. «открой мне на ubuntu-pc на ultra по ssh tailscale в наглядном виде все настройки с пояснениями на
   русском и значений. и доделай все. и настрой чтобы ubuntu-tweaks так же устанавливал и включел ssh
   сервер при инициализации и док и синхронизацию паролей. и rig опционально и .files, и rig должен все
   по возможности активировать и настройки rig должне переноситься (я начинал это делать, проверь по
   веткам и доделай) и риг в том чесле должен настраивать tmux и omp. доделай все.
   и еще пароли должны подставляться в firefox, а док не должен выскакивать если курсор около границы,
   только если он на границе 300мс+
   и файрфокс должен авторизовываться автоматически при инициализации ubuntu-tweaks
   конечно, перед этим должен быть установлен и настроен passwords-app и из него и будут браться пароли
   для всех авторизаций автоматически. еще вот это отстой: «! Failed to copy one-time code to clipboard
   — No clipboard utilities available. Please install xsel, xclip, wl-clipboard…» git, gh -- тоже должны
   быть установлены и настроены. убунту из коробки вообще тлен какой-то даже curl нету. и brew пусть
   ставится для линукса и если после будут нужны доп действия тоже пусть делаются. и конечно же zsh должен
   быть установлен и активирован в самом начале» — the overview part is answered by `show` (see *show*);
   the rest by the `init` pipeline and the base modules (see *Init pipeline*, *Base modules*), the service
   modules (see *Service modules*: ssh server, passwords-app, Firefox) and, for the optional steps 10
   (rig and `.files`), *Optional modules: rig, dotfiles*; the dock's edge dwell is hyperdock's
   ([hyperdock#17](https://github.com/alex-mextner/hyperdock/pull/17), `docs/specs/mac-dock.md`).
10. «еще zip нету. все необходимые тулы должны быть в .files ставиться» — `zip` and the other archivers
    are in the engine's base list; the rest of a person's tools live in the profile's `packages.toml`
    (see *Base modules*, *The engine's list and the profile's list*).
11. «надо чтобы ставился ctmux мой, это обертка над cmux с лучшей поддержкой tmux. его должен rig ставить.
    надо убедиться что будет не только на маке но и на убунте работать эффективно» — `ctmux` is a
    first-class rig tool (`tools.items.ctmux`), installed by `rig activate` on macOS and Ubuntu; see
    *Optional modules: rig, dotfiles*.
12. «а еще нужно чтобы ставились эти твики для файрфокса: - https://github.com/alex-mextner/firefox-passkey-qr — установщик passkey-qr. Перед каждым шагом он спрашивает
   подтверждение. Если Bluetooth на ПК нет, а HA найден, он сам ставит мост. Если Bluetooth есть, этот шаг пропускается.» — the `firefox-passkey-qr` module (see *Service modules*).
13. «когда все доделаешь начни применение на ubuntu-pc, попутно доработай все что пойдет не так чтобы когда я буду новые свои машины настривать все прошло как по маслу в одну команду» — `init` is one command on a fresh machine (`scripts/bootstrap.sh`), its manual leftovers are printed at the end (see *Init pipeline*, *Manual steps*); the apply on `ubuntu-pc` follows the integrated release.
14. «Важно, Ubuntu-tweaks это OSS проект а не мой личный приватный. Он не должен Хардкодить мои ник имя ссылки,
    .files и тп. Он должен искать через gh .files или dotfiles и сам устанавливать

    А еще все выводы в интерактивном терминале должны быть покрашены и пояснены

    Например status сейчас очень непонятный» — the generic-OSS half (no hardcoded identity, dotfiles repo found
    through gh, profile-supplied modules) is in *Engine and profile*, *Profile discovery* and *Profile modules*;
    the output half is in *Output*: every interactive output is colored and explained, `status` first.
15. «ubuntu-tweaks должен уметь запрашивать sudo и чинить всё что не так» — the engine obtains root itself,
    once and up front (*Elevation*), and repairs every drift in one command (request 18).
16. «Все что видит пользователь в cli писалось в логи, чтобы ты мог анализировать» — every command's complete
    user-visible session (stdout and stderr, prompts and answers included) is also written to a log file an
    agent can read afterwards; see *Session log*.
17. Not a quote: the observed problem. The CLI ran from a temporary git worktree through the `~/.local/bin`
    symlink, and the plan linked the GNOME extensions into that worktree, so the links dangle once the worktree
    is removed. Persistent links must target a stable checkout (*Checkout*).
18. «Зачем fix? Разве не init/apply должен быть только? Как в rig» — there is no `fix` command: `apply --commit`
    and `init --commit` ask for sudo once, install the missing apt packages (what `doctor --yes` does) and apply
    the plan in the same run, with no question in between (*Repair in one command*, *Elevation*). Where `rig`
    differs and no decision of the user is recorded, the difference is listed under *Decisions*.
19. «И --dry-run добавь везде» — every command accepts `--dry-run` (*Dry run*).
20. «Почему zprofile не из .files» — the dotfiles repository's own shell rc files (`.zprofile`, `.zshrc`,
    `.profile`) are what the machine gets; the generic `shell-rc` step only fills in what neither the profile nor
    the repository has, and the git identity is taken from the profile, the machine, the repository's `.gitconfig`
    or `gh` instead of being left unset (*rc files from the repository*, *git and gh*).
21. «важнее не чтобы агент ad hoc починил, а чтобы ubuntu-tweaks init/apply работали из коробки. Делай в apply
    полноценную реконсиляцию с failover'ом» — `apply` and `init` are a level-triggered reconcile with failover:
    one failing action never aborts the run; failures are classified, retried or routed to a declared fallback,
    their dependents are marked blocked, everything independent still runs, and the run ends with a summary and a
    stable exit code (*Reconcile and failover*). A fix an agent makes by hand on a machine is not done until the
    engine, a module or the profile reproduces it.
22. «надо не точечно конфиги в терминале и что там я ещё упомянул, надо комплексное решение по всем конфигам
    системы и приложений» — one configuration-coverage system instead of one-off fixes: every configuration
    location is tracked, excluded with a reason, secret or reported as untracked, through one adapter interface
    (*Coverage*).
23. «ещё есть диск на 1тб с мака, он должен сам монтироваться и работать. это тоже должен ubuntu-tweaks притягивать в
    общем виде. там тоже может быть что-то важное и полезное» — a generic `disks` module: any disk the profile
    declares (USB or internal, apfs, ntfs, exfat, ext4, ...) gets its driver, a marked `/etc/fstab` block, a mount point
    and a systemd automount; `capture` proposes the declaration for disks nobody declared (*Disks*).

## Requirements

### Engine and profile

Request 7 is answered by splitting the system in two:

- **Engine = this repo** (`alex-mextner/ubuntu-tweaks`, public open source): the `ubuntu-tweak` CLI, the
  generic modules (base tools, zsh, clipboard, git and gh, Homebrew, ssh server, memguard, apple-hid-battery,
  and the bundled extensions hyperdock and english-in-overview), the modules that integrate tools published by
  the project author (*Author-published tool modules*) and the machinery that applies a profile.
  There is no second tool.
- **Profile = a directory of personal data**, the `ubuntu/` subdirectory of the user's own dotfiles repository
  (`.files`, `dotfiles`, `.dotfiles` or `dot-files` on their GitHub account; *Profile discovery*):
  Settings-panel values, linked config files, packages, root-level system settings, per-machine overlays and
  the user's own modules (*Profile modules*).

The public engine never contains personal settings and names no person, host or repository owner of its own
(request 14): the dotfiles repository is found through `gh`, the only places that name a tool of the author
are the manifests and installers of the *Author-published tool modules* (each switchable, each default URL
overridable), other tools that belong to one person are modules of that person's profile, and
person-specific values (git name and email, the keychain item of a token) come from the profile only and have
no default. `tests/test_no_identity.py` fails on a new occurrence of the author's identifiers;
`tests/identity_allowlist.txt` lists each allowed path and why (the project's own repository URL, the upstream
URL of a bundled extension, the tool references of the author-published modules). The private profile never
contains engine code.
The shell rc files at the root of a dotfiles repository (`.zprofile`, `.zshrc`, `.profile`) are linked into `$HOME` by the plan (*rc files from the repository*); whatever else it holds (zplug, brew, aliases) is not touched by this system; `ubuntu/` is
self-contained. Profile location: `--profile DIR` > `$UBUNTU_TWEAK_PROFILE` > the profile found by
*Profile discovery*. An explicit location that does not exist is invalid config (exit 2); no discovered
profile means "no profile" and only module defaults apply, with the header saying what was looked for.

Fresh machine:

```sh
gh auth login                                          # once; the dotfiles repository is found through it
git clone --recurse-submodules https://github.com/alex-mextner/ubuntu-tweaks ~/ubuntu-tweaks
~/ubuntu-tweaks/bin/ubuntu-tweak init      # doctor -> submodules -> apply preview -> --commit
```

### Safety invariants

1. Every mutating command is **preview by default**; only `--commit` changes anything.
   `capture` and `apply` both print the exact plan and change nothing without `--commit`.
2. The preview is computed by the same code path as the commit (the plan holds only actions
   whose `check()` fails), so what the user saw is what runs.
3. Nothing from the live system enters the profile except through `capture`, which shows
   everything it would write first (see *capture*).
4. **No secrets in the profile.** Deny rules and a value-pattern scan (PEM blocks, tokens,
   emails) skip or mask keys and make `capture --commit` refuse (exit 5). The `.files` repo
   CI runs gitleaks with `--exit-code 1`; `capture` never produces output that fails it.
5. Replacing a real file with a symlink always copies the original to
   `~/.local/state/ubuntu-tweaks/backup/<UTC timestamp>/<path>` first.
6. Tests and smoke runs never touch the user's dconf or `$HOME`: they use `dbus-run-session`
   with a scratch `DCONF_PROFILE`, a temporary `HOME` and temporary `XDG_*`. Reading the live
   system (capture preview, status) is allowed; writing happens only when the user runs a
   command with `--commit`.
7. Extras (on disk, not declared) are reported, never removed.

### Commands

`ubuntu-tweak` is one executable, `bin/ubuntu-tweak`, running the package
`ubuntu_tweaks/` from the repo checkout (no install step; the checkout is the program).
Commands self-register: `ubuntu_tweaks/commands/<name>.py` exposes
`register(subparsers)`, and `cli.py` never names a command; dropping a file adds one.

PyGObject (`gi`) is a distro package, so a pyenv/uv `python3` first in `PATH` lacks it. When the
interpreter running the launcher cannot import `gi`, `bin/ubuntu-tweak` re-executes itself once
under `/usr/bin/python3` (the invoked path is kept, so the `~/.local/bin` symlink keeps working).
When that has no `gi` either the launcher still runs, so `doctor` (which installs `python3-gi`) and
the commands that need no `gi` work; a command that needs it ends with exit 1 and
`sudo apt install python3-gi` (`apply`, `status` and `update` through `require_gi`, anything else
through `cli.main`'s handler of the missing module). The system interpreter path is overridable
with `UBUNTU_TWEAK_SYSTEM_PYTHON` (a test seam); `UBUNTU_TWEAK_REEXEC` is the loop guard the
launcher sets itself (to its pid, which `exec` keeps, so a stale value from the shell is ignored).
Covered by `tests/test_launcher.py`.

| Command | What it does |
|---|---|
| `init [--commit] [--no-sudo]` | Fresh system. Runs `doctor` (read-only without `--commit`; with `--commit` it installs the missing apt packages as part of the run, with no question: `--commit` is the consent, the preview names the packages; `--no-sudo` skips that install and goes on), then plans `git submodule update --init --recursive` and a symlink of the CLI into `~/.local/bin` (kinds `cli-link` / `cli-link-replace`: not extension code, so no re-login hint), then the `apply` plan; without `--commit` it only prints all of that, with `--commit` it executes it (*Repair in one command*). `--only` limits it to the named modules (no CLI link). A missing default profile directory is not an error: `init` says which dotfiles repository gh found (or what was looked for, or `needs manual: ... gh auth login`), the `dotfiles` module clones it with `--commit` and the same run applies it (*Profile discovery*); without one the plan covers the modules only. Re-running is safe. |
| `apply [--commit] [--no-sudo]` | Prints the plan: every action needed to make the system match modules + profile, the `N changed` count after `--commit`, and the re-login hint. Without `--commit` it changes nothing and says so. In a terminal: badges, `[sudo]` marks, a reason per action, numbered progress and a summary (*Output*). |
| `apply --commit` | Executes the plan. Idempotent: a second run reports `0 changed`. When the plan holds root actions (and not `--no-sudo`), root is obtained once before the first of them runs (*Elevation*); when it cannot be, the run finishes all user-level work, lists the root remainder and exits 6 (*Reconcile and failover*). When the plan installs code that adds new schemas (submodule init, schema compile, extension link), the plan is rebuilt once the earlier actions ran, so settings of a fresh extension are applied in the same invocation (at most three passes); the preview says so. |
| `status [--explain\|-v] [--json]` | Drift both ways (see *Drift*, *Output*), grouped as Missing / Modified / Extra / Manual, with a verdict line, a definition of each group and the next command in a terminal; compact text when piped; `--json` for scripts. Exit 3 when anything declared is Missing or Modified; extras and manual items alone exit 0. |
| `update [--commit] [--no-sudo]` | `git pull --ff-only` of the umbrella, `git submodule update --init --recursive`, then `apply`. Without `--commit` it prints the two git commands it would run and the `apply` preview of the current checkout. Channel and schedule come from #19. |
| `doctor [--yes]` | Checks `python3-gi`, `dconf-cli`, `git`, `glib-compile-schemas` and every enabled module's `[deps] apt`. A tool that an enabled module installs itself (`[install] apt`, e.g. `git` by `base-tools`) is reported `planned`, not `MISSING` (*Init pipeline*); `init --no-sudo` keeps them hard because its root actions are skipped. Exit 3 when something is missing; `--yes` installs the missing apt packages with `sudo apt-get install` (root is obtained first, *Elevation*). |
| `list` | Modules with kind, enabled state and source, from the manifests and the merged config. Read-only. |
| `machine` | Read-only: detected machine name, whether its overlay exists, and for each module setting, settings bundle and linked file whether it comes from the shared profile or the machine overlay. |
| `capture [--commit] [--dry-run] [--summary] [--report FILE] [--only …] [-v]` | Inventory of the live system, printed as the exact diff the profile would receive, SUMMARY block first (see *capture*). `--dry-run` is the explicit no-write mode (excludes `--commit`); `--summary` prints only the summary. |
| `bundles [--show NAME]` | Read-only: the settings bundles with origin, key count, description, whether they apply here, and the shadowed, duplicated and conflicting keys; `--show` prints one bundle's merged result (see *Settings bundles*). |
| `bundle-move <bundle> <key-or-glob>... [--commit]` | Moves matching keys, with their comments, from whichever bundle holds them into `<bundle>`. Preview by default. |
| `note <target> "reason" [--origin shared\|overlay] [--commit]` | Adds, changes or removes the reason (comment) of a bundle key, a package or a system value. Preview by default. |
| `show [--changed\|--all] [--panel ID] [--format text\|md\|html] [--output FILE] [--ssh [USER@]HOST]` | Read-only Russian overview of all settings of this machine (or, with `--ssh`, another one) grouped by Settings panel, with explanations, readable values, defaults and a status (see *show*). |

Global flags, accepted before or after the command: `--profile DIR`, `--machine NAME`,
`--only <name,…>` (modules for `apply`/`status`, panels for `capture`), and the output flags
`--color auto|always|never`, `--plain`, `--lang en|ru` (see *Output*), and `--dry-run` (see *Dry run*).

Exit codes: 0 ok, 1 internal error, 2 invalid config, 3 drift, 4 unknown module, 5 refused
(secret finding or unsafe path), 6 root needed but not obtainable (*Elevation*), 7 partial: the run finished but an
action failed or was blocked (*Reconcile and failover*). The constants live in one place,
`ubuntu_tweaks/errors.py`, and top-level errors map to them. A command-line usage error
(unknown command or flag) is argparse's own exit 2. A name in `--only` that no module
declares is exit 4.

After any action that adds or changes extension code, the CLI says that a re-login is
needed: Shell on Wayland loads extension code only at login.

### Checkout

Request 17. The engine has one persistent location, **the stable checkout**: the tree the user keeps and that
`update` pulls in. Everything persistent points into it (module directories, submodules, extension links and
the schemas compiled in them, the `~/.local/bin/ubuntu-tweak` link, `engine_version`), never into a temporary
worktree that may be removed. `ubuntu_tweaks/checkout.py` resolves it, in order:

1. `$UBUNTU_TWEAKS_DIR` when set and non-empty (the name `scripts/bootstrap.sh` already uses for its clone
   directory; there is no second variable). `~` is expanded; a path that is not a directory is `ConfigError`
   (exit 2).
2. The main working tree of the git repository the running code belongs to: the parent of
   `git rev-parse --git-common-dir`, so a linked worktree (`<main>/.worktrees/...`, `~/.omp/wt/...`) resolves to
   the tree it was added from. Only when the running directory is that repository's top level and the common
   directory is a `.git` directory. This needs git 2.31+ (`--path-format=absolute`); with an older git the rule
   does not apply and rule 3 takes over.
3. The running checkout itself: not a git checkout, git unavailable, or a layout not understood above.

`Context.repo_root` is the stable checkout (every planner reads and links through it); `Context.running_root`
is the checkout whose code runs, informational only. When they differ `cli.main` prints one line to stderr
and the interactive plan header repeats it:

```
warning: running from <path>, a git worktree; links, submodules and extension code target the stable checkout <stable> (set UBUNTU_TWEAKS_DIR to change)
```

(`a copy` instead of `a git worktree` when the running directory is not a worktree.) A stable checkout is
**usable** when it has a `modules/` directory and an `ubuntu_tweaks/` package. A command that plans links,
submodule init, schema compiles or extension code declares it with `parser.set_defaults(plans_links=True)`
(`apply`, `init`, `update`; `cli.py` never names them); when the stable checkout is not usable such a command
ends with exit 2 naming the path, what is missing and `UBUNTU_TWEAKS_DIR=<checkout> ubuntu-tweak <command>`, with
or without `--commit` (the preview must not describe a plan against the wrong tree). Read-only commands
(`status`, `list`, `machine`, `show`, `bundles`, `capture`, `note`, `bundle-move`, `doctor`) keep working from the
running checkout (their `repo_root` is the running tree when the stable one is unusable) and print only the warning.
Covered by `tests/test_checkout.py`, which builds temporary git repositories with linked worktrees; the suite's
`tests/__init__.py` sets `UBUNTU_TWEAKS_DIR` to the checkout under test so that `cli.main` tests never resolve to
a developer's main checkout.

### Elevation

Request 15. Root actions (`[sudo]` in the plan, the apt install of `doctor --yes`) used to call `sudo` one at a
time, so the password prompt could land between two half-applied steps, and a run with no terminal ended in sudo's
raw message. `ubuntu_tweaks/elevation.py` is the one place that obtains root: `RootSession.require(count, kinds)`
runs the strategy below once per run (later calls are no-ops) and, for the duration of the run, makes
`process.elevate()` prefix root commands the way the strategy found to work.

- **When it asks.** `apply --commit`, `init --commit`, `update --commit` (through its `apply --commit` child),
  `doctor --yes`: only when the plan holds an unsatisfied root action (or there is an apt package to
  install) and `--no-sudo` is absent; after the preview is printed and before the first action of the plan runs.
  When a rebuilt plan (a profile that was just cloned, a staged pass) holds root actions the first one did not, the
  call is repeated and acquires then, still before those root actions run (but after the steps that need no root
  already ran: a failure then says so instead of "Nothing was changed"). A preview, `status`, `list` and every
  `--no-sudo` run never ask and never look at sudo.
- **Strategy**, the first row that applies wins:

  | # | State | What happens |
  |---|---|---|
  | 1 | the process is root (euid 0) | nothing to do |
  | 2 | `NoNewPrivs: 1` in `/proc/self/status` | `RootUnavailable` (`no-new-privs`): setuid helpers are neutered, so sudo and pkexec cannot work from here, NOPASSWD or not (a sandbox, a container, a restricted agent shell) |
  | 3 | neither `sudo` nor `pkexec` exists | `RootUnavailable` (`no-tool`) |
  | 4 | `sudo -n true` succeeds (NOPASSWD, or a ticket still valid) | go on, no prompt; the refresh thread below also runs, so a ticket about to expire cannot lapse mid-run |
  | 5 | a controlling terminal (`/dev/tty` opens) and `sudo` | one line says why (how many root actions, which kinds), then `sudo -v` once with its own password prompt; the refresh thread runs `sudo -n -v` every 60 s until the run ends, so the password is asked **once** however long the run is (a hung refresh never holds the exit for more than a second) |
  | 6 | `SUDO_ASKPASS` is set and `sudo` exists | `sudo -A -v` once; root commands run as `sudo -A --` (also when `sudo -n true` already works and there is no terminal, so a ticket that lapses is asked for again through it) |
  | 7 | `pkexec` exists and there is a graphical session (`DISPLAY` or `WAYLAND_DISPLAY`) or a terminal (pkexec starts its own text agent there) | `pkexec true` once to prove it works; root commands run through `pkexec` (polkit keeps no ticket we could refresh, so it may ask again per action; a dismissed dialog then fails that action, not exit 6) |
  | 8 | none of the above | `RootUnavailable` (`no-prompt`): nothing here can ask for a password; the message lists what is missing (sudo not passwordless, no terminal, no `SUDO_ASKPASS`, pkexec absent or without a terminal or session) |

  sudo or pkexec rejecting the request (wrong password after sudo's three tries, sudo's own prompt timeout, a user
  who may not use sudo: sudo answers 1 for all of them) is `RootUnavailable` (`rejected`). There is no retry loop:
  the run goes on without root: every user-level action still runs and the root actions are the **root remainder**
  (*Reconcile and failover*); `doctor --yes`, which has nothing else to do, stops.
- **The error.** `RootUnavailable` is an `UbuntuTweakError` with exit code **6** (`ExitCode.NO_ROOT`), no
  traceback. Its message names the cause and ends with the exact command to run from a normal terminal: the
  command that was run, with the `--profile` / `--machine` / `--only` / `--dotfiles-repo` the user gave (e.g.
  `ubuntu-tweak apply --commit`, `ubuntu-tweak apply --commit --only eng`); and, where skipping root is meaningful, the
  alternative `... --no-sudo` (root actions are skipped and listed; `doctor --yes` has none). In a terminal the
  `fix:` line repeats the command. `ExitCode.NO_ROOT` is also what `update --commit` returns when its `apply`
  child fails this way.
- **Tests** use a `Host` value (euid, controlling terminal, `SUDO_ASKPASS`, display, `NoNewPrivs`, the tools on
  `PATH`; `Host.detect()` reads the real ones) and `FakeProcess`, so no test needs real sudo.

### Repair in one command

Request 18. There is no `fix` command: `apply --commit` and `init --commit` are the repair, as in `rig`. Steps of
one `--commit` run, in order, with one `RootSession` for the whole run:

1. **Requirements** (what `doctor` checks: `python3-gi`, `dconf-cli`, `git`, `glib-compile-schemas`, the enabled
   modules' `[deps] apt`): the ones missing that no module installs itself are installed with
   `apt-get install -y` after root was obtained once. No question is asked: `--commit` is the consent and the
   preview already said `apply --commit would first install: sudo apt-get install -y <packages>`. Still missing
   afterwards ends the run with exit 3. `--no-sudo` skips the install, says so and goes on when the plan can still
   be built (it cannot without `python3-gi`: exit 3).
2. The plan runs as before (*Plan and actions*): root is already held, so the plan's own `[sudo]` actions never
   ask again; rebuilt plans (a profile cloned by this run, extension code that adds schemas) run in the same call.
3. The run ends with `N changed`, the re-login hint and the manual steps with their reasons; `status` afterwards
   is the check (exit 0 when nothing declared is left).

Without `--commit` (or with `--dry-run`) nothing is installed or applied and no sudo is asked; the preview lists
the apt packages the commit would install first. A run that cannot get root does not stop: it finishes everything
that needs no root, lists the root remainder with the command to run from a normal terminal and exits 6 (*Elevation*,
*Reconcile and failover*). Idempotent: a second run reports `0 changed`.

### Dry run

Request 19. Every command accepts `--dry-run`, before or after the command name. For a command that can change
something (`apply`, `init`, `update`, `doctor`, `capture`, `bundle-move`, `note`) it is the explicit spelling of the
default preview: the same output and the same exit code as the run without `--commit` / `--yes`, one extra line
`dry run: nothing was changed` on stderr, no sudo, no write anywhere except the session log (*Session log*), no
question. It excludes the command's commit word (`--commit`, `--yes` of `doctor`): both together are a usage
error, exit 2 (`ConfigError`) with the message `--dry-run and --commit exclude each other`. A read-only command
(`status`, `list`, `machine`, `bundles`, `show`) accepts it as a no-op with the same stderr line, so a script can
pass `--dry-run` to every command it runs. `cli.py` resolves it once for all commands: the flag is a global flag
and a command that has a commit word is declared by its own `--commit` / `--yes` argument.

### Modules

A module is a directory `modules/<name>/` with `module.toml` and optional settings files.
Adding a tweak is: add the code (a submodule, a local directory or a root installer), add
`modules/<name>/`. The CLI never names a module in code.

```toml
description = "macOS-style dock"
enabled = true                 # default; profile.toml / machine.toml override it

[source]
kind = "submodule"            # submodule | local | builtin | system   (ego: reserved, #20)
path = "gnome-extensions/hyperdock"

[extension]                    # submodule and local; builtin only with `enable`
conflicts = ["ubuntu-dock@ubuntu.com"]                       # disabled while enabled
retire = ["compiz-alike-magic-lamp-effect@hermes83.github.com"]  # dropped from enabled

[[settings]]
file = "dock.ini"              # keyfile: [/] then key=GVariant text
# schema defaults to the extension's metadata.json "settings-schema"; required for builtin
mode = "enforce"               # enforce: apply and drift-check every key
                               # seed: write only while the key is at its default

[[seeds]]
key = "folder-stacks"
provider = "screenshots-stack" # named function in ubuntu_tweaks/seeds.py

[[migrations]]
from_uuid = "dash2dock-lite@icedman.github.com"
from_path = "/org/gnome/shell/extensions/dash2dock-lite/"

[deps]
apt = ["gir1.2-gtk-4.0"]
```

Unknown keys, a missing `[source]`, a source without the fields its kind needs, or a path
that is absolute or contains `..` are invalid config (exit 2).

- **Source kinds**
  - `submodule`: a git submodule; uuid, schema id and schema dir are read from its
    `metadata.json`, never hardcoded, so the module keeps working across the hyperdock
    rename.
  - `local`: an extension directory inside this repo (`english-in-overview@ultra`).
  - `builtin`: no code, only settings for GNOME's own schemas (e.g. the app grid, the
    keyboard, the color scheme from #20).
  - `system`: root-level code installed outside `$HOME` (memguard's units and polkit rule, the
    apple-hid-battery livepatch). The manifest names its installer and a read-only checker,
    both command lines relative to the module directory:

    ```toml
    [source]
    kind = "system"
    install = "install.sh"
    check = "install.sh --check"   # exit 0 = installed files match; must be read-only
    repo = "https://example.org/tool.git"   # optional, system only: default git URL of the tool this module installs
    ```

    The action's `check()` runs the checker; its `run()` runs the installer through
    sudo/pkexec. A system module takes no `[extension]`, `[[settings]]`, `[[seeds]]` or
    `[[migrations]]`. An installer that cannot check itself gets a `--check` mode before its
    module is added.

    Checker protocol: exit 0 = installed and matching, 3 = drift (the module is planned), any
    other exit code, a launch failure or a 60-second timeout = the check crashed, which fails
    `apply` / `status` with the module's name and output instead of guessing (exit 1). Both
    commands run with the module directory as working directory and `argv[0]` resolved inside
    it. The planner (`planners/system_modules.py`) emits one action per enabled `system`
    module: kind `system-module`, phase SYSTEM, root, scope = the module name, preview line
    `run <module>: sudo <command>`. Disabled modules (module default, profile or machine
    overlay) emit nothing; `--no-sudo` skips them and says so like every root action.
    `memguard` checks with `install.sh --check` (the `--dry-run` comparison: every installed
    file byte-equal to the repo copy and its mode, timer enabled; `/etc/polkit-1/rules.d` is not
    readable by the desktop user, so that one file is vouched for by
    `/etc/memguard/installed.sha256` (mode, sha256, path per file), written by the root install;
    `MEMGUARD_ROOT=DIR` points the check, or a staged non-root install, at a test root; what only a
    real root can verify is listed in `docs/specs/memguard.md`, *Real-root verification*). `apple-hid-battery` checks the running
    kernel: module present, loaded at
    boot, livepatch enabled, or absent on kernels with the upstream fix (>= 7.3).
    A `system` module may also set `root = false`, `preview` and `repo` (the default git URL of the tool it
    installs, handed to the installer as the option `repo`: *Author-published tool modules*), read
    `UBUNTU_TWEAK_MODULE_CONFIG` and exit 10 from its installer; see *Init pipeline*.
  - `ego`: an extension from extensions.gnome.org by uuid and optional version. The
    manifest schema reserves it and validation rejects it with "not supported yet (#20)"
    until it is implemented. Source kinds are one class each behind one interface
    (`resolve()` → uuid, schema dir, files; `fetch()`), so adding `ego` touches no other
    code.
- **Settings files** use `dconf dump` keyfile syntax, so a preset is made with
  `dconf dump /org/gnome/shell/extensions/<x>/ > modules/<x>/<x>.ini` and trimmed.
- **Seeds** are values computed on the machine (the Screenshots folder is localized), written
  only while the key is at its default, never drift-checked. A seed belongs to the module's
  settings schema (the first `[[settings]]` entry's `schema`, else the extension's
  `metadata.json` `settings-schema`); its provider is a function in `ubuntu_tweaks/seeds.py`
  returning GVariant text plus the directories to create. Its check is "the key has a user
  value" (a value that a defaults database or override already supplies counts as "at its
  default", as Gio reports it), so it is never drift-checked. An unknown provider name is
  invalid config.
- **Migrations** run before settings: when `from_uuid` is still installed or `from_path`
  has user values and the module's current schema path has none, user values are copied
  to the new path, the old uuid is removed from `enabled-extensions`, and its symlink is
  removed if it points into this repo. The copy is raw GVariant text (`dconf dump` of the old
  path, `dconf load` into the new one: the old schema may be gone), limited to keys the new
  schema has; the old path is left as it is. The uuid is removed as a single-item edit of
  the *user value* of `enabled-extensions` (an override-provided value is never frozen). A
  symlink that does not point into this repo is not touched. A migration is one `migrate`
  action, satisfied when none of the three steps is pending.

### Init pipeline

`ubuntu-tweak init` makes a stock Ubuntu usable in this order (request 9); each step is a module with
`order = <step> x 10` and `phase = "base"` (steps 1-4) or its own phase, each is an action with
`check()` / `run()`, previewed by `init` / `apply` and executed only with `--commit`, and each is
switchable per machine with `[modules.<name>] enabled = false` in `machines/<host>/machine.toml`.

| Step | What | Module (order) |
|---|---|---|
| 1 | base tools + `zsh` installed **and** set as the login shell, first | `base-tools` (10), `zsh` (11) |
| 2 | clipboard tools (`wl-clipboard`, `xclip`, `xsel`) | `clipboard` (20) |
| 3 | `git` + `gh` installed and configured | `git-gh` (30) |
| 3b | your dotfiles repository (found through gh) cloned with the profile in it, its shell rc files linked (`dotfiles` is on by default and a no-op when there is none), then the generic rc files only for what neither has | `dotfiles` (35), `shell-rc` (36) |
| 4 | Homebrew for Linux and its follow-up actions | `brew` (40) |
| 5 | ssh server | `ssh-server` (50) |
| 6 | passwords-app: install, enable unit, native host, polkit action | `passwords-app` (60), `passwords-app-polkit` (61) |
| 7 | Firefox integration: extension, policy, auto-login with passwords-app credentials, phone-QR passkeys | `firefox` (70), `firefox-policy` (71), `firefox-autologin` (72), `firefox-passkey-qr` (73, opt-in) |
| 8 | dock | `hyperdock` (80) |
| 8b | Space previews the selected file in Files (Quick Look) | `quicklook` (85) |
| 9 | GNOME settings and profile files | the settings and files phases (no module order: they follow the install phases) |
| 10 | rig (opt-in: + tmux, omp, `ctmux`) | `rig` (100) |

`ctmux` is installed by `rig activate` inside step 10 (a first-class rig tool, `tools.items.ctmux`) and, for a profile
that wants it without rig, by the profile module `ctmux` (*Profile modules*), which runs the same `install.sh` of
ctmux-cli; both end in ctmux's own installer, whose `--check` keeps them idempotent.

Any other tool of a person's own is a module of their profile (*Profile modules*) and takes any `order` it
likes.

<a id="init-order-table"></a>
**Order of every module** (the golden `init` preview of an empty machine, `tests/golden/init_empty_machine.txt`,
covers all of them; `tests/test_init_pipeline.py` fails when this table and a `module.toml` disagree):

| Module | `order` | Phase of its install actions | Default |
|---|---|---|---|
| `base-tools` | 10 | `base` | on |
| `zsh` | 11 | `base` | on |
| `clipboard` | 20 | `base` | on |
| `git-gh` | 30 | `base` | on |
| `dotfiles` | 35 | `base` (so the profile arrives before the shell and brew steps; it was 110) | on |
| `shell-rc` | 36 | `base` | on |
| `brew` | 40 | `base` | on |
| `ssh-server` | 50 | `system` | on |
| `passwords-app` | 60 | `system` | on |
| `passwords-app-polkit` | 61 | `system` | on |
| `firefox` | 70 | `system` | on |
| `firefox-policy` | 71 | `system` | on |
| `firefox-autologin` | 72 | `system` | on |
| `firefox-passkey-qr` | 73 | `system` | off |
| `hyperdock` | 80 | submodule, link, toggle, settings (their own phases; the order is only the tie-break) | on |
| `quicklook` | 85 | `packages` | on |
| `gsconnect` | 90 | `packages` | off |
| `allowlockedremotedesktop` | 91 | local extension link, toggle | off |
| `autoheal` | 95 | `base` (a user timer, never root; *Reconcile and failover*, *Self-heal*) | off |
| `rig` | 100 | `system` | off |

Steps 5-8 and 10 belong to their own modules; this section defines the mechanism they plug into.

**Ordering.** `Plan.build` sorts by `(phase, order)`, both stable. `Phase.BASE` (5) precedes everything,
even the submodule init, because later steps need `git`, `curl` and a shell. A module's `module.toml`
may set:

- `order` (whole number 0-999, default 100): the tie-break inside a phase; every action of the module
  gets it unless the planner set its own (a `[[post]]` is `order + 0.5`, so it follows its module's
  install even when another planner creates that install). It is the init step times ten, so a module
  can be slotted between two steps.
- `phase` (`"base"`, `"packages"` or `"system"`): moves the module's install-time actions (phases `BASE`,
  `PACKAGES`, `SYSTEM`) there; submodule, migration, schema, link, toggle, settings and seed actions keep
  their phases because they depend on each other and on the extension code. `system` modules default to
  `SYSTEM`; the new tables below default to `PACKAGES`. `order` 100 is "unset": an action that carries
  another value keeps it.

`planners.build_actions` applies both after all planners ran (`apply_module_ordering`), so any planner's
actions, the system-module planner's included, follow the manifest.

**Manifest tables** (any source kind; a `system` module has its own installer and takes only `[[post]]` and `manual`):

```toml
order = 30
phase = "base"

[install]                      # distro packages the module is for
apt = ["git", "gh"]
[install.provides]             # package -> a command that counts as the package being there
gh = "gh"                      # a brew-installed gh satisfies `gh`

[[step]]                       # a built-in step by name, extra keys are its arguments
name = "brew-install"
url = "https://raw.githubusercontent.com/Homebrew/install/<commit>/install.sh"
sha256 = "..."

[[post]]                       # follow-up command after the install, with its own check
name = "formulae"
description = "brew install the formulae of packages.toml"   # the preview line; default: name + argv
needs_root = false
after = "install"              # default; or an earlier [[post]] name; posts run in declared order
unless_linked = [".bashrc"]    # optional: not planned when the profile links one of these files
run = ["/home/linuxbrew/.linuxbrew/bin/brew", "install", "{packages.brew}"]
check = ["/home/linuxbrew/.linuxbrew/bin/brew", "list", "--formula", "{packages.brew}"]

manual = "how the user completes the step a `system` installer reports as manual (exit 10)"   # top level
```

- **`[install] apt`** of every enabled module of one phase is **one** `package` action (one
  `apt-get install -y a b c`, names deduplicated), so the base tools and the clipboard utilities are a
  single `apt` run while each module stays switchable; its preview line ends with the contributing
  modules. A package counts as installed when `dpkg-query` says `ii` or its `provides` command is on
  `PATH`. `apt_update = true` of `packages.toml` also applies.
- **`[[step]]`** names are looked up in `ubuntu_tweaks/steps.py` (`STEPS`); an unknown name is invalid
  config (exit 2) with the known names. Steps: `login-shell`, `shell-rc`, `git-identity`, `gh-login`,
  `git-credential-helper`, `brew-prefix`, `brew-install` (arguments in the module docstring of `steps.py`).
- **`[[post]]`** `run` / `check` are argv lists (no shell); `argv[0]` is a program on `PATH` or an absolute
  path. A whole argument `{packages.apt|snap|flatpak|brew}` expands to that list of `packages.toml`; a post
  whose placeholder expands to nothing is not planned. `check` exits 0 = satisfied (a failing or missing
  check means "not satisfied"). An absolute `argv[0]` that does not exist yet (brew before its install ran,
  e.g. under `--no-sudo`) makes the post a manual step instead of a crash. `after` defaults to `"install"`
  (the module's install actions: its apt batch, its steps, its system installer); a post never runs while
  what it waits for is unsatisfied. `unless_linked = [".bashrc", ...]` (`$HOME`-relative) drops the post
  from the plan when the profile's `files/` links one of those paths: the profile's own file is then the
  one that does the job (see *rc files* below).
- **Manual steps.** An action whose `run()` raises `ManualStepRequired` is reported as
  `needs manual: <what>`, is not counted in `N changed`, does not fail the plan, and stays unsatisfied, so
  `status` lists it as Missing (exit 3). `apply` never prompts for it. A staged `apply` (it rebuilds the plan
  after code-installing actions) runs a manual step once and reports it once: a step reported in an earlier
  pass is dropped from the rebuilt plan (identity is the action's `key`, not its text, which can embed live
  state). Steps that need a package an unsatisfied step was to install (`login-shell` without zsh,
  `gh-login` / `git-credential-helper` without `gh`, `brew-install` without its prefix) are manual steps too,
  so `--no-sudo` never aborts the plan. `run()` raises it before any side effect (the system-module
  exit-10 case is the one exception, and is covered by that rule). A `gh auth login` that fails (a token the
  server rejects, an offline machine) is a manual step as well, never a failed plan: `init --commit` with an
  expired token still ends with brew installed and `gh-login` in the list.
- **The closing `Manual steps` block.** `apply --commit` and `init --commit` end with one block listing every
  manual step of the run, numbered, one line each (`  1. [gh-login] gh auth login (...)`), after `N changed`,
  the `--no-sudo` skip list and the re-login hint. A module's installer that exits 10 contributes its
  manifest `manual` text (how the user finishes it); the installer's own `needs manual: <what>` line is above
  in the output. Every manual step a module can end with is in the table *Manual steps* below.

**`system` modules, additions.** `[source] root = false` (default `true`) runs the installer as the user:
the action has no `[sudo]` marker, is not skipped by `--no-sudo`, and `install` runs directly (for modules
that install into `$HOME`, user systemd units or the Secret Service). `[source] preview = "<script> plan"`
is an optional read-only command (resolved and run like `check`): `apply` prints its stdout indented under
the action's `run <module>: ...` line so the installer's exact steps are visible before `--commit`; a
failing preview shows `(preview unavailable: exit N: <first stderr line>)` and never fails the plan. The
merged `[modules.<name>]` table of `profile.toml` / `machine.toml` without `enabled` is passed to `check`,
`preview` and `install` as JSON in `UBUNTU_TWEAK_MODULE_CONFIG` (unset when there is none; under sudo as
`sudo -- env UBUNTU_TWEAK_MODULE_CONFIG=<json> <argv>`, because sudo's `env_reset` drops a plain variable).
Because that puts the options in `ps` output and in sudo's log, **a module option is never a secret**.
An installer exit code **10** means "needs a manual step" (its own output says which): reported like a
`ManualStepRequired`, no post-check, the plan goes on, `status` keeps the module Missing. Any other non-zero
exit stays a failure.

**`doctor`.** `python3-gi`, `dconf-cli` and `libglib2.0-bin` stay hard requirements (the plan cannot be
built without them). `git` and a module's `[deps] apt` that an enabled module installs through
`[install] apt` of a module with `phase = "base"` (the only installs that run before the submodule init that
needs `git`) are reported `planned` (`init` / `apply` install them), not `MISSING`, so `doctor` does not
fail on tools the pipeline is about to install. `update` runs `git pull` and the submodule update before `apply` only in
a git checkout (`<repo>/.git` exists); a tree without it (an unpacked tarball) prints that it skips them and runs
`apply`, so it never needs `git` there. The `submodule` planner agrees: in a tree without `.git` a non-empty
submodule directory counts as present and an empty one is a manual step (never a `git` call).

### Extensions the system ships, and extra extensions of the laptop

`[extension] enable = ["<uuid>", ...]` is allowed on a `builtin` module (a submodule or local module's uuid is its
`metadata.json`'s, so `enable` is refused there): the toggle planner then enables those uuids in the user's
`enabled-extensions` (and removes them from `disabled-extensions`) while the module is enabled, exactly as for a code
module, and `status` does not list them as extensions no module declares. The code comes from the module's
`[install] apt`; the Shell reads new extension code at login, so a fresh install needs one re-login.

| Module | What | Default |
|---|---|---|
| `gsconnect` | apt `gnome-shell-extension-gsconnect` (71, GNOME 46-50), `python3-nautilus`, `sshfs`; enables `gsconnect@andyholmes.github.io`. Pairing keys are per machine (`~/.config/gsconnect`), a phone is paired again on a new computer. | off (the profile of a person who uses KDE Connect turns it on) |
| `allowlockedremotedesktop` | the 3 KB extension `allowlockedremotedesktop@kamens.us` by Jonathan Kamens (GPL-2.0-or-later, vendored in `gnome-extensions/` with its `COPYING`; upstream https://github.com/jikamens/allow-locked-remote-desktop) as a `local` module: it keeps GNOME Remote Desktop connections alive while the screen is locked. That weakens the lock screen for anybody who can reach the remote desktop port, so it is off unless a profile asks for it. | off |

Both exist because the laptop `home` had them enabled (found by reading it live on 23 Oct).

### Quick Look (`quicklook`)

User request of 23 Oct ("space для предпросмотра в навигаторе по файлам я еще делал"): Space in Files (Nautilus) previews the
selected file. No code of the user's own for it survives on this PC (not in any repository, branch, session or
backup that was searched), so the module installs the GNOME solution instead of rebuilding one: apt
`gnome-sushi` (the `org.gnome.NautilusPreviewer` D-Bus service Nautilus calls on Space) and `gstreamer1.0-libav`
(H.264/AAC/MP3 playback in the preview). `kind = "builtin"`, default phase (`packages`), order 85, on by default,
`[modules.quicklook] enabled = false` in `machine.toml` switches it off. It needs root only for the apt batch, which
`apply --commit` already runs under the one sudo prompt. The bus name is activated on demand from the service file
the package ships, so neither Files nor the session has to restart: press Space on a file after the apt run.

### Base modules

Steps 1-4. All are `kind = "builtin"`, `phase = "base"`, enabled by default.

| Module | Installs / does |
|---|---|
| `base-tools` | apt: `curl wget ca-certificates git gnupg openssh-client zsh build-essential procps file zip unzip 7zip xz-utils bzip2 jq libsecret-tools` (verified with `apt-cache policy` on Ubuntu 26.04: all exist; `p7zip-full` does not, `7zip` replaces it). `build-essential procps file curl git` are Homebrew's prerequisites; the archivers answer "zip is missing" on a stock install; `libsecret-tools` (`secret-tool`) is for the Secret Service scripts of the passwords-app and Firefox modules. |
| `clipboard` | apt: `wl-clipboard xclip xsel`: what `gh` looks for when it copies a one-time code (`! Failed to copy one-time code to clipboard: No clipboard utilities available`). |
| `zsh` | apt `zsh` (always the apt build: `chsh` needs a shell listed in `/etc/shells`, so a brew zsh on `PATH` does not replace it); step `login-shell` (`chsh -s /usr/bin/zsh <user>` through sudo; satisfied when the passwd shell is named `zsh`, a brew zsh included, read with `getent passwd <user>`; a manual step when `/usr/bin/zsh` is not there, e.g. under `--no-sudo`). |
| `shell-rc` | step `shell-rc` (own module since request 20, order 36, right after `dotfiles`): `~/.zprofile` and `~/.zshrc` that put brew's `shellenv` and `~/.local/bin` on `PATH`, **only while absent** (an existing file is never read, replaced or appended; `O_EXCL`; one that appears between preview and run is left alone) **and not provided** by the profile's `files/` or by the dotfiles repository (see *rc files* below, *rc files from the repository*): it is the pure fallback. |
| `git-gh` | apt `git gh` (`gh` 2.46.0 is in the 26.04 `universe` archive, decided with `apt-cache policy gh`); steps `git-identity`, `gh-login`, `git-credential-helper`. |
| `brew` | steps `brew-prefix` (root) and `brew-install`; posts `shellenv` (appends a guarded `[ -x brew ] && eval "$(brew shellenv)"` to `~/.bashrc` unless `~/.bashrc`, `~/.profile` or `~/.bash_profile` already has it, or the profile links one of them), `tools` (`brew install uv oven-sh/bun/bun` unless `uv` and `bun` are already on `PATH`: what the passwords-app and Firefox modules run on) and `formulae` (`brew install` of `packages.toml` `brew`). A post waits for the module's install (`after` defaults to `install`) and does not run (it is a manual step) while the actions it waits for are unsatisfied, e.g. `shellenv` without a brew install under `--no-sudo`. |

**git and gh.** Identity comes only from the profile, never invented:

```toml
[git]                          # profile.toml, machine.toml wins
name = "Ada Lovelace"
email = "ada@example.org"
token_ref = "example/gh-token"         # optional: the keychain item that holds a token; no default
host = "github.com"                    # optional
```

`git-identity` sets `git config --global user.name` / `user.email`. Per key, the first source that knows it wins
(request 20: an identity that is knowable is never left unset, and nothing is hardcoded in the engine):

1. `[git] name` / `email` of the profile (enforced: drift when the global value differs);
2. the value `git config --global` already holds: left as it is, nothing planned, no warning;
3. `user.name` / `user.email` of `<dotfiles checkout>/.gitconfig` (read with `git config -f`; the file is not linked);
4. `gh api user` when `gh` is logged in: the name (else the login) and the public e-mail (else
   `<id>+<login>@users.noreply.github.com`).

A planned value says where it came from (`from ~/.files/.gitconfig`, `from gh api user`). Only when no source
knows a key does the plan warn, and the warning says what to do: it is not an error, git works until the first
commit. `gh-login` takes the token **only** from the keychain
item that `[git] token_ref` names (no default; the `keychain` CLI, GNOME Keyring / Secret Service), at run time:
`keychain exec --no-prompt --caller ubuntu-tweaks --stdin <ref> -- gh auth login --hostname <host>
--with-token`; the value is never in argv, an environment variable, a file, the profile or this process,
and its output is masked by `keychain`. The caller name `ubuntu-tweaks` owns the items under `ubuntu-tweaks/`;
an item in any other namespace (such as `example/gh-token`) needs a grant in `~/.config/keychain-cli/policy.toml`. When the keychain is missing, locked,
denies access or has no item, the action is `needs manual: gh auth login (<reason>; store a token with
`keychain set <ref>`)` and `status` lists it Missing. A token that exists but is rejected, or an unreachable
host, is the same manual step (`gh auth login` failing never aborts the plan). `git-credential-helper` runs
`gh auth setup-git --hostname <host> --force`.
With no `[git] token_ref` no keychain item is ever guessed: the `gh-login` step is `needs manual: gh auth login
(no [git] token_ref set in profile.toml; run `gh auth login`, or set `[git] token_ref` to a keychain item holding
a token)` and `status` lists it Missing.

**Homebrew installer pin.** `brew-prefix` creates `/home/linuxbrew/.linuxbrew` owned by the user with
`sudo install -d` (the only root part; the installer then finds a writable prefix and needs no sudo).
`brew-install` downloads the installer script from a **commit-pinned** `Homebrew/install` URL, compares its
SHA-256 with the manifest's `sha256` and refuses (exit 1, nothing executed) on any difference, then runs
`env NONINTERACTIVE=1 /bin/bash <script>` from a private temporary directory it removes afterwards. Current
pin: commit `a2fe82ef3a9ec7a5c72a559c09676256d6f354f6`, sha256
`5f333bbe53bc490e51e7ccb1df8779b3dd6ee73a1a7379efda216edb08ccb148` (35651 bytes). To bump it: take the
new `Homebrew/install` commit, `curl -fsSL <raw url> | sha256sum`, read the diff of the installer against
the old pin, change `url` and `sha256` in `modules/brew/module.toml` and regenerate the `init` golden file.

**The engine's list and the profile's list.** The base modules above are the minimal bootstrap: what the
pipeline itself needs. Everything else a person wants (`tmux`, `ripgrep`, `htop`, editors, snaps, flatpaks,
brew formulae) is in the profile's `packages.toml` with `# reason` comments, shared with machine overlays
that add to it; both lists are unioned and installed by the same `apply`. `capture` proposes the profile
list from `apt-mark showmanual` and friends (see *capture*). Packages the base modules installed are marked
manual by apt, so a later `capture` proposes them again (harmless: the lists union); filtering them out by
module is not implemented.

**Manual verification (root and network paths).** `sudo -n` is unavailable on the development machine, so
the following are verified with fake-root unit tests and must be checked by hand once on a real
machine (as for `memguard`): `ubuntu-tweak init --commit` on a fresh VM installs the apt batch; `getent
passwd $USER` shows `/usr/bin/zsh` afterwards (log out and in); `ls -ld /home/linuxbrew/.linuxbrew` is owned
by the user and `/home/linuxbrew/.linuxbrew/bin/brew --version` works; a second `init --commit` reports
`0 changed`; with a token stored by `keychain set <your token_ref>` (the item `[git] token_ref` names), `gh auth status` is logged in
and `git config --global --get-all credential.https://github.com.helper` names `gh auth git-credential`;
without one, `status` lists `gh-login` as Missing and `apply --commit` still exits 0. Not yet observed on a
real fresh machine: the Homebrew installer running non-interactively against a pre-created prefix
(derived from reading the pinned script, not executed).

**rc files and the profile's `files/`.** The BASE phase runs before the FILES phase, so a file the profile links
would be created or appended to by BASE and then replaced (with a backup) by the link. The rule, one for all:
**BASE never creates or appends to an rc file that the profile's `files/` links** (shared or machine overlay).
`shell-rc` leaves out `~/.zprofile` / `~/.zshrc` when the profile links that file or the dotfiles repository
provides it (*rc files from the repository*), and the brew `shellenv` post
(`unless_linked = [".bashrc", ".profile", ".bash_profile"]`) is not planned when either links any of them.
The profile's own file is then the one that puts brew's `shellenv` and `~/.local/bin` on `PATH`. The preview
shows what remains, and `tests/test_planner_base.py` covers both. With `--only` the links of `files/` are not in
the plan (they carry no module scope), so BASE keeps creating the rc files then.

**Stale package lists.** The base apt batch runs `apt-get update` right before its install when the package
indexes in `/var/lib/apt/lists` are missing (a fresh image) or the newest `*_Packages*` file is older than two
days (the preview line ends `(after apt-get update)`); `apt_update = true` of `packages.toml` forces it. The
update is skipped when nothing is missing. **`login-shell`** takes a shell *name* (`zsh`); an argument with a
`/` is invalid config (exit 2). **`brew-prefix`**: a prefix that exists but is not owned by the user (and has no
brew) is a manual step with the exact `sudo chown -R <user>:<group> <prefix>` (nothing is changed); a prefix with
a working `brew` counts as done.

### Manual steps

What `init` / `apply` can end with as `needs manual` (the closing `Manual steps` block prints these, see
*Init pipeline*). Each is Missing in `status` until done; `apply` never prompts and never stores a secret.
After doing one, run `ubuntu-tweak apply --commit` again.

| Step | Module | Why only you | How you complete it |
|---|---|---|---|
| `gh auth login` | `git-gh` | the GitHub token must not live in the repo or profile | store a token once: `keychain set <your token_ref>` (GNOME keyring; the item `[git] token_ref` names, no default), or run `gh auth login`; an expired or rejected token and an offline machine land here too |
| git identity | `git-gh` | planned from the first source that knows it; only when none does: warned, not an error | set `[git] name` / `email` in `profile.toml`, or `git config --global user.name ...` |
| login shell, `brew-prefix` / `brew-install` under `--no-sudo` | `zsh`, `brew` | they need root | run without `--no-sudo`, or `chsh -s /usr/bin/zsh`; a root-owned existing prefix: `sudo chown -R <user>:<group> /home/linuxbrew/.linuxbrew` |
| `sudo` password | every `[sudo]` action | root actions ask on the terminal | answer the prompt of `sudo` once, up front (*Elevation*; the same password covers the whole run). Without a terminal (ssh without `-t`, an agent or a sandbox with `no_new_privs`) every user-level action still runs, the root actions are listed as the root remainder with the command to run from a normal terminal, and the exit code is 6; or use a temporary NOPASSWD sudoers file for the remote run and remove it afterwards (ticket #54) |
| `ssh-server` lock-out guard | `ssh-server` | `password_auth = false` with no key would lock you out | add a public key to `~/.ssh/authorized_keys` (`ssh-copy-id`) |
| FileVault volume, or the kernel module under Secure Boot | `disks` | an encrypted APFS volume needs a password no unit can type and the module never stores; a DKMS module needs a signing key enrolled with a reboot | mount it by hand (`apfs-fuse -r <password> <device> <dir>`); or `sudo mokutil --import /var/lib/shim-signed/mok/MOK.der`, reboot, confirm; or keep the default `driver = "fuse"` (*Disks*) |
| iCloud sign-in | `passwords-app` | Apple ID, password and 2FA code | `passwords-app signin` |
| polkit without the checkout | `passwords-app-polkit` | needs the clone made by `passwords-app` | keep `passwords-app` enabled |
| AMO signing key | `firefox` | the key belongs to the owner's AMO account | `secret-tool store` the items `purpose=amo-jwt-issuer` and `purpose=amo-jwt-secret` (the install output prints the commands) |
| signed extension missing | `firefox-policy` | nothing to force-install yet | do the AMO step above first |
| extension not active | `firefox-autologin` | Firefox must show the WebExtension-backend prompt | apply `firefox-policy`, restart Firefox, answer the prompt, check `about:policies` |
| Home Assistant token | `firefox-passkey-qr` (opt-in; needed only on a PC without Bluetooth) | the long-lived HA token belongs to the owner | `secret-tool store` it as `purpose ha-token` (the exact command is under *Firefox*): without it the bridge step waits for you (the other steps still run; `needs manual`, re-run `apply` after storing it) |
| gh login or clone | `dotfiles` | the repository is found through your gh login | `gh auth login`; set `[modules.dotfiles] repo` or pass `--dotfiles-repo OWNER/NAME`; move a directory that is in the way; resolve a diverged checkout |
| rig clone | `rig` (opt-in) | its repository has no default (private forge), then forge credentials, or a directory in the way | set `[modules.rig] repo` to the git URL of rig-cli; `gh auth login` / `fj auth login`; move the directory; `rig profile sync --take local\|remote` |
| missing `python3` | `rig`, `dotfiles` | their installers need it (it ships with Ubuntu) | `sudo apt-get install -y python3` |

### One command on a fresh machine

Request 13. `scripts/bootstrap.sh` (POSIX `sh`, at most 40 lines) takes a stock Ubuntu with neither `curl` nor
`git` to a running `init`: when `git`, `curl`, the CA bundle or `python3-gi` is missing it runs `sudo apt-get update`
and `sudo apt-get install -y git curl ca-certificates python3-gi` (also for the preview: nothing can be previewed
without them), clones this project's repository (`$UBUNTU_TWEAKS_REPO`, default `alex-mextner/ubuntu-tweaks`) with its submodules into `~/ubuntu-tweaks` (or
`$UBUNTU_TWEAKS_DIR`; an existing checkout is fast-forwarded and its submodules updated; that directory is the stable
checkout of *Checkout*), then runs
`bin/ubuntu-tweak init "$@"`. The commit is explicit: with no arguments it is the **preview**; `--yes` is the
only word that turns it into `init --commit` (apply and install without asking: `--commit` is the consent);
any other argument
(`--commit`, `--no-sudo`) goes to `init` unchanged. It is the one-liner of the README:
`curl -fsSL https://raw.githubusercontent.com/alex-mextner/ubuntu-tweaks/main/scripts/bootstrap.sh | sh -s -- --yes`
(once on `main`; with no `curl` at all, `wget -qO- <url> | sh -s -- --yes`, or `sudo apt-get install -y curl` first).
`sudo` still asks for the password on the terminal. The user's dotfiles repository (with the profile) is not cloned
by it: `init --commit` clones it through the `dotfiles` module (*Profile discovery*); the first preview names it
and says it is not cloned yet, and the *Manual steps* block lists the gh login it needs when gh is logged out.
`tests/test_bootstrap.py` runs the script with fake `sudo`, `apt-get`, `git` and `python3` on `PATH` and checks the argv.

### Modules at cutover

| Module | Kind | Notes |
|---|---|---|
| `hyperdock` | submodule `gnome-extensions/hyperdock` → `alex-mextner/hyperdock`, branch `mac-tahoe` | from today's `install.sh`: symlink, schemas, enable, Ubuntu Dock conflict, Magic Lamp retire, `dock.ini` (enforce), Screenshots stack seed, migration from `dash2dock-lite` |
| `english-in-overview` | local `gnome-extensions/english-in-overview@ultra` | symlink, enable |
| `memguard` | `system` | `modules/memguard/` already ships `install.sh`, the `swap-drain` units and the polkit rule; its manifest needs `install.sh --check`; per-machine (`enabled = false` on machines that do not want it). The desktop user is detected from `SUDO_USER` (else `PKEXEC_UID`), never a default name, and the installer writes it into the installed `/etc/memguard/memguard.toml`; the heavy-run options `heavy_gate`, `heavy_hook` and `heavy_locks` default to empty (off) |
| `apple-hid-battery` | `system` (livepatch kernel module) | only for machines with Apple HID: `enabled = false` by default, a machine overlay enables it |
| `ssh-server` | `system` (root) | openssh-server installed, enabled, running and listening; orders `ssh.service` after `tailscaled.service` when sshd listens on tailscale addresses (*SSH server*) |
| `disks` | `system` (root) | the disks the profile declares: driver, `/etc/fstab` block, mount point, systemd automount; plans nothing without `[modules.disks.mounts.*]`, so it is on by default and belongs to no init step (*Disks*) |
| `passwords-app` | `system`, user (`root = false`) | clone `~/dev/passwords-app`, run its `scripts/install.sh`, enable `passwords-appd.service`, verify D-Bus (*Passwords*) |
| `passwords-app-polkit` | `system` (root) | the one root step `install.sh` only prints: the polkit action for unlocking with the login password |
| `firefox` | `system`, user | build the passwords-app extension and have it signed on AMO (key from the Secret Service) (*Firefox*) |
| `firefox-policy` | `system` (root) | `/etc/firefox/policies/policies.json` force-installs the signed extension |
| `firefox-autologin` | `system`, user | start Firefox, wait for the extension, open the sign-in page once so passwords-app fills it |
| `firefox-passkey-qr` | `system`, user | the `firefox-passkey-qr` installer run with `--yes`; `enabled = false`, a machine enables it |
| `earlyoom` | `system` (root), rootless fallback | the package, `/etc/default/earlyoom` from typed `[modules.earlyoom]` options, the service; spec: `docs/specs/earlyoom.md` |

The init-pipeline modules (`base-tools`, `zsh`, `clipboard`, `git-gh`, `brew`; *Base modules*) are enabled by
default and switchable per machine. No other module is added until it has real content.

### Profile layout

`<profile>` = the `ubuntu/` directory of the user's dotfiles repository, found by *Profile discovery* (`--profile
DIR` and `$UBUNTU_TWEAK_PROFILE` override).

```
<profile>/
  README.md
  profile.toml              # shared config: modules, capture rules
  settings/**/*.ini         # settings bundles: dconf keyfiles named by purpose, shared by all machines
  files/                    # mirrors $HOME: files/.config/mimeapps.list -> ~/.config/mimeapps.list
  packages.toml             # apt / snap / flatpak names
  system.toml               # root-level: timezone, locale, console keyboard
  modules/<name>/           # the profile's own modules (module.toml + their scripts), see *Profile modules*
  machines/<machine>/       # overlay, one dir per machine
    machine.toml            # same schema as profile.toml; wins over it
    settings/**/*.ini       # key-level overlay bundles on top of the shared bundles
    files/                  # path-level overlay: the same relative path wins over shared files/
    packages.toml  system.toml   # same schema, merged
```

**Machine name** = the contents of `/etc/hostname`, or `--machine NAME`. It is a directory
name, so it must be a single path component (no `/`, not `.` or `..`). Detection is one
function, so everything downstream is testable with `--machine`. A machine without an overlay
directory uses the shared profile only; `machine` and `status` say so.

**Merge order**, last wins: module defaults (`modules/<m>/module.toml`) < shared profile <
machine overlay. Tables merge key by key and scalars replace. Lists replace in
`profile.toml`/`machine.toml` (a list is one value) and **union** in `packages.toml` and
`system.toml` (shared order first, then the overlay's new items), so a machine adds packages
without restating the shared ones. Every merged value remembers its layer (module, shared or
overlay), which is what `machine` prints.

This replaces the earlier per-user `~/.config/ubuntu-tweaks/config.toml`: user overrides
live in the profile, so they are versioned with the rest. The `[update]` table owned by #19
lives in `profile.toml` as well.

#### `settings/**/*.ini`: settings bundles

Any `settings/**/*.ini` file is a **bundle**: one file per purpose, holding keys from any dconf
path or schema, merged with the others at load (Home Assistant's `packages:`; it is not called
`packages` because `packages.toml` already means apt/snap/flatpak). The bundle's name is its path
under `settings/` without `.ini` (`dev-keyboard`, `laptop/power`);
`machines/<machine>/settings/**/*.ini` files are bundles of the overlay layer. The panel files
`capture` creates by default (`appearance.ini`, `keyboard.ini`, ...) are just bundles named after
the panel ids in *Panel coverage*.

`dconf dump` keyfile syntax (section = dconf path without the leading `/`, values = GVariant
text). Apply semantic = **enforce only the keys present**; absent keys are never touched or
reset. Values are compared as typed `GLib.Variant`s read through the schema (effective value =
user value, else default), exactly as in *Drift*. A key whose schema is not installed is skipped
with a warning, never guessed. A value outside the key's schema range or choices is invalid
config (exit 2) while planning (module presets too), never a failure in the middle of `--commit`
(`SettingsBackend.in_range`). Relocatable schemas (custom keybindings, tiling-assistant paths) go
through `Gio.Settings.new_with_path`.

`~/.config/glib-2.0/settings/keyfile` on a machine is a GSettings keyfile override that
changes *effective* values (power, idle-delay, enabled-extensions, favorite-apps). The engine
reads effective values through Gio, never freezes overrides into dconf (see *Plan and
actions* on `enabled-extensions`), and `capture` reports keys whose effective value comes
from the override rather than from the dconf user value. That file is itself a capturable
`files/` entry.

Details of the planner (`ubuntu_tweaks/planners/settings.py`, helpers in `setkey.py`):

- **Layers**, lowest first: module presets (an enabled module's `[[settings]]` files, section
  `[/]` meaning the module's schema at its own path), the shared bundles (`settings/**/*.ini`), the
  machine overlay's. A key set in several layers is planned **once**, with the highest layer's
  value: otherwise a satisfied lower layer could be dropped from the plan while a higher one
  is kept, or the reverse. `[modules.<m>.settings]` in `profile.toml` / `machine.toml`
  replaces the preset entry with that key name (a key the preset does not set is looked up in
  the schemas of the module's `[[settings]]` files, else in its `metadata.json` schema, first
  match; a name set in two preset directories is ambiguous and skipped with a warning); it
  keeps that entry's `mode`, beats every module's preset, and stays in the module layer, so
  bundles still win over it. A value there must be a string of GVariant text.
- **Preview**: each action reads `<path><key> = <value> (now <effective>) <- <source file>`, plus
  `  # <reason>` when the bundle has a comment above the key.
  A key that cannot be placed is not an action; it is a `skipped …` warning printed with the
  plan: no installed schema at that directory, or no such key in the schema. A value that does
  not parse as the key's type is invalid config (exit 2).
- **Relocatable schemas** have no path of their own, so a small table in `setkey.py` maps
  known path patterns to schemas: custom keybindings, per-app notification settings, app
  folders, Terminal and Ptyxis profiles.
- `enabled-extensions` and `disabled-extensions` in a bundle are skipped with a warning:
  the extension toggles own them as single-item edits of the user value.
- A module whose extension schema is not installed yet (its link and schema compilation run
  earlier in the same plan) has its keys planned as unsatisfied, typed only when run; the
  directory and key names then come from the extension's `schemas/*.gschema.xml`. Panel
  keys for such a schema wait for the next planning pass.

#### Bundle merge, header and reasons

- **Merge.** All active shared bundles are merged key by key; machine bundles override shared
  ones at the key level (the overlay key *shadows* the shared one; `bundles` lists the shadows).
  The same key in two shared bundles, or in two machine bundles, with **different** values is
  invalid config (exit 2) naming both files and lines (values compare as text); with identical
  values it is a *duplicate* warning (shown by `apply`, `status` and `bundles`) and planned once.
  Nothing is ever picked silently.
- **Header.** An optional `[ubuntu-tweak]` section is the bundle's header, not a dconf section, and
  never reaches a plan: `description = "..."` (bare text is accepted too), `machines = ["home"]`
  (the whole bundle applies only on the listed machine names) and `requires-module = "hyperdock"`
  (only while that module is enabled). An unknown header key, a wrong type or an unknown module is
  invalid config (exit 2). A bundle that does not apply on this machine takes part in nothing: no
  conflicts, no capture routing, no plan.
- **Reasons.** The full-line `#` comments directly above a key (no blank line between) are its
  reason; above a section header they are the section's; a comment separated by a blank line
  belongs to nobody. Every tool that edits a bundle keeps them (below). `apply` prints a key's
  reason, on one line and truncated to 80 characters, after the source of its `set-key` (`... <-
  settings/dev-keyboard.ini  # why`), `status` shows it for Modified keys, `capture` shows it for
  `~` and `=` entries.

```ini
[ubuntu-tweak]
description = "Keys for working on the dev keyboard"
machines = ["home", "work"]

# Super+arrows are taken by the tiler, so workspaces move on Alt.
[org/gnome/desktop/wm/keybindings]
# the OLED panel: never a white flash
switch-to-workspace-left=['<Super><Alt>Left']
```

The commands below are the only tools that rewrite a bundle besides `capture --commit`; each is a
preview unless given `--commit`, computes the whole result before writing anything and writes each
file atomically (a profile file that is a symlink is refused, exit 5):

- `ubuntu-tweak bundles` lists the bundles (name, origin, key count, description, whether each
  applies on this machine and why not) and the shadowed, duplicated and conflicting keys (exit 2
  when any conflict); `bundles --show NAME` prints the merged result of one name: the shared
  bundle with the overlay's keys on top, each key with its reason and the file and line it comes
  from (and the value it overrides).
- `ubuntu-tweak bundle-move <bundle> <key-or-glob>...` moves every key of the active bundles
  matching a dconf key path or glob into `<bundle>` (created when missing), with the key's
  comment block. A key stays in its layer (shared to `settings/`, overlay to
  `machines/<machine>/settings/`), so a move never changes which machines see it. A section that
  empties is dropped and its own comment goes along; a key the destination already holds with the
  same value is merged (the other copy goes, its reason kept when the destination has none); a
  pattern that matches nothing is invalid (exit 2). A move never changes where a key applies: it is
  refused (exit 2) between bundles whose `machines` / `requires-module` headers differ, and a new
  destination inherits the source's `machines` and `requires-module` (not its description). No
  comment is lost and no key is duplicated. A capture that changes a key held identically in several
  bundles of one layer rewrites all of them, so the profile never ends up in conflict.
- `ubuntu-tweak note <bundle>:<dconf key path> "reason"` (also `packages:<apt|snap|flatpak>:<name>`
  and `system:<key>`, e.g. `system:keyboard.layout`) adds, replaces or, with an empty text, removes
  a reason without touching anything else; the target must exist (exit 2). `--origin
  shared|overlay` chooses the layer when both hold it. A package's reason is a trailing comment
  (an array with several names per line is spread to one per line first, which is refused when it
  has comments, so none is lost); a bundle or system key's reason is the comment block above it.

#### `files/`

A mirror of `$HOME`. Each *leaf file* under `files/` becomes a symlink in `$HOME` pointing at
the profile copy (`ln -sfnT`). Directories stay real directories in `$HOME`; only leaves are
linked, so apps can add siblings: `files/.config/foo/bar.conf` → `~/.config/foo/bar.conf`. A
machine overlay path wins over the shared file at the same path. `~/.config` keeps symlinks;
the real file lives in the profile.

- An existing real file at the destination with identical content is adopted silently
  (replaced by the symlink, still backed up). With different content, the `apply` plan shows
  `backup+replace` with a diff summary; never silently.
- An app that rewrites the symlink into a real file (atomic rename: gtk bookmarks,
  `mimeapps.list`) is reported by `status` as `replaced`; `capture` re-adopts it after
  showing the diff.
- Capturable files are an explicit allow-list only (see *capture*), never a recursive grab of
  `~/.config`.
- Destination states and plan kinds: nothing there → `link`; a real file or a symlink that
  points elsewhere → `link-replace`, shown as `adopt` (identical content) or `backup+replace`
  (with sizes and `+added/-removed` line counts; binary or large files are not diffed); a
  real directory, a non-regular file, or a regular file where a parent directory should be →
  shown as `REFUSED` and `run()` raises (exit 5), nothing is touched. The destination is
  re-checked against `$HOME` right before it is replaced. The original always goes through
  `ubuntu_tweaks/backup.py` first.
- Refused before any plan exists (exit 5): a path containing `..`, and a destination whose
  nearest existing parent resolves outside `$HOME` (for example a symlinked `~/.config`).
- Extras: dangling symlinks into the profile (their file was removed from it). Found in
  `$HOME` itself (its direct entries only), all of `~/.config` and the directory of every declared
  link, each walked recursively (at most ten levels, symlinked directories not entered,
  directories whose name contains `cache` and per-app stores such as `node_modules`, `.git`,
  `IndexedDB`, `Local Storage` skipped), so an orphan at any depth below those is found.
  Reported, never removed.

#### `profile.toml` / `machine.toml`

```toml
[profile]
description = "alex personal Ubuntu settings"

[modules.hyperdock]             # per-module override
enabled = true
[modules.hyperdock.settings]
icon-size = "0.15"              # GVariant text, overrides the module's preset

[modules.memguard]              # per machine: set in machines/<machine>/machine.toml
enabled = false

[update]                        # owned by #19
channel = "release"

[capture]                       # rules evaluated by `capture`
exclude = ["/org/gnome/shell/app-picker-layout"]            # never captured
machine = ["/org/gnome/settings-daemon/plugins/power/**"]   # captured into machines/<machine>/
include_files = [".config/mimeapps.list", ".config/gtk-3.0/bookmarks"]   # file allow-list
machine_files = [".config/monitors.xml"]                      # adopted into machines/<machine>/files/
include = []                                                  # lifts the `state` and `auto` skips

[capture.bundles]               # where captured keys go (first matching entry wins)
"dev-keyboard" = ["org/gnome/desktop/wm/keybindings/**", "org/gnome/shell/extensions/tiling-assistant/**"]
"laptop/power" = ["org/gnome/settings-daemon/plugins/power/**"]
```

Disabling a module stops managing it; it does not uninstall. `ubuntu-tweak` never edits the
repo's files on the user's behalf.

#### `system.toml` (root, applied through sudo/pkexec at `--commit`)

```toml
timezone = "Europe/Belgrade"           # timedatectl
locale   = "en_US.UTF-8"               # localectl
[keyboard]                             # /etc/default/keyboard
layout = "us,ru"
options = "grp_led:scroll"             # model and variant are accepted too
```

Checks are read-only (`timedatectl show`, `localectl status`, parsing
`/etc/default/keyboard`). The plan marks root actions `[sudo]`; `--no-sudo` skips them and
says so.

`locale` sets `LANG` and passes the other `LC_*` assignments of the current system locale
through (`localectl set-locale` replaces the whole set); the locale must already be generated.
`run()` takes one `localectl status` snapshot and uses it both for the readable check and for the
preserved `LC_*` arguments. Output without a parseable `System Locale:` block (as well as a failing
command) is *unknown*: the action refuses with an error and does not call `set-locale`, because
that would drop the machine's other `LC_*`. A block with no `NAME=value` pair is unknown too, except
`System Locale: n/a`, localectl's way to say that no locale is set.
Keyboard values containing quotes, `$`, backticks, backslashes or control characters are
invalid config. Only the keyboard fields
listed are compared and written; the other lines of `/etc/default/keyboard` are kept, and
the new file is installed with `install -m 644` through sudo/pkexec. A failing check command
counts as "not satisfied". Timezone, locale and keyboard are three `system` actions with
`needs_root`.

#### `packages.toml`

```toml
apt = ["tmux", "ripgrep"]
snap = ["firefox"]
snap_classic = ["code"]     # classic-confinement snaps: `snap install --classic` (docs/specs/packages-snap-classic.md)
brew = ["gh"]                # optional: Homebrew formulae (Linuxbrew), installed by the brew module
flatpak = ["org.gnome.Extensions"]
flatpak_remote = "flathub"   # optional: the one remote flatpaks are installed from (default flathub)
apt_update = false           # optional: run `apt-get update` before the apt batch (default false)
```

`apply` installs the missing ones and never removes. `capture` proposes
`apt-mark showmanual` minus the installer baseline (`/var/log/installer/initial-status.gz`
when present, else minus the packages the `ubuntu-desktop` metapackage pulls in), so only
deliberate installs show up.

A comment above a name or after it on its line (`"tmux",  # shell tools`) is that name's reason,
and `capture --commit` keeps all of them: it appends new names to the array and leaves the order
and the comments alone. `ubuntu-tweak note packages:apt:<name> "reason"` adds or changes one.

`brew = ["ripgrep"]` lists Homebrew formulae: the `brew` module's `formulae` post installs the missing
ones with `brew install` after the Homebrew install itself (see *Base modules*); the lists union like the
others and a name starting with `-` is invalid config. There is no `brew` package action: the formulae
are a `[[post]]` of the module, so a machine without the `brew` module never plans them. `capture` does not
scan Homebrew yet ([#48](https://github.com/alex-mextner/ubuntu-tweaks/issues/48)); the capture writer keeps
an existing `brew` array and its comments.

One `package` action per manager that has names, listing what is missing right now. apt and
snap run as one batch through sudo (`apt-get install -y …`, `snap install …`); flatpak runs
as the user from one remote, `flatpak_remote` (default `flathub`; a bare remote name, which must
already be configured; the overlay value replaces the shared one). Installed means: `dpkg-query`
status `ii`, a `snap list` row, a `flatpak list` row. The apt lists are refreshed only with
`apt_update = true` (boolean, default false; the overlay value replaces the shared one): then
`apt-get update` runs through sudo right before the apt batch, only when an apt package is missing,
and the plan line says `(after apt-get update)`. A manager that is not on the machine means "not
installed". A `flatpak_remote` that is empty, starts with `-` or contains whitespace, and a
non-boolean `apt_update`, are invalid config (exit 2).

### Profile discovery

Request 14: the engine names nobody. `ubuntu_tweaks/identity.py` (pure; every command goes through an injected
`Process`, tests pass a fake `gh`) finds the dotfiles repository and the profile in it, first hit wins:

1. **An explicit repository**: `--dotfiles-repo OWNER/NAME` (a global flag) or `$UBUNTU_TWEAK_DOTFILES_REPO`;
   the flag wins and is exported to the `dotfiles` module. The module's own `[modules.dotfiles] repo` (slug, git
   URL or absolute path) reaches only the module.
2. **A checkout on disk that holds a profile**: the one the `dotfiles` module recorded, then `~/.files`,
   `~/dotfiles`, `~/.dotfiles`, `~/dot-files`. No network, no `gh`.
3. **The cache** of an earlier `gh` answer, `~/.local/state/ubuntu-tweaks/dotfiles.json` (`$XDG_STATE_HOME`),
   valid for one hour, so `gh` is not asked on every command; `--refresh` (a global flag) ignores it. It is the
   only file discovery writes; a read-only state directory only costs the cache.
4. **`gh`**: `gh api user -q .login` is the authenticated user, then `gh repo list <login> --json
   name,nameWithOwner,isPrivate,isArchived,defaultBranchRef --limit 300` is filtered to the user's own repository
   named, in this order, `.files`, `dotfiles`, `.dotfiles`, `dot-files` (case-insensitive, archived ones skipped;
   a full 300-entry listing may hide a match, so each missing name is then asked with `gh repo view`). When
   several exist, the first by that order is used and the header says so (`note: several dotfiles
   repositories found (...): using ...`), never silently.

`gh` missing, not logged in, or unreachable is never a prompt and never a crash: the header and the `dotfiles`
step say `needs manual: not logged in to GitHub: run `gh auth login`` (or that gh is not installed, or that
GitHub could not be asked), nothing is cached, and the run goes on with the modules only. A found repository
and "none found" are cached. With no repository the header says `no dotfiles repository for <login> (looked
for .files, dotfiles, .dotfiles, dot-files); only the modules apply` and how to create one.

`gh` is never waited on for long: each `gh` call is capped at 15 s (a module check is killed at 60 s), an
unreachable GitHub is cached for 60 s (one slow command, not every command), and the cache is keyed by gh's
`hosts.yml` and by the token variables `GH_TOKEN` / `GITHUB_TOKEN` / `GH_ENTERPRISE_TOKEN` (hashed, never
stored), so a new `gh auth login` or another account is never answered from the old cache.

**The profile inside the repository** is the first of `ubuntu/`, `ubuntu-tweaks/` (a directory with a
`profile.toml` or any other profile entry) and the repository root (with a `profile.toml`); `[modules.dotfiles]
path` names it instead (relative, no `..`; `.` is the root). **The clone target** is `~/.files` for a repository
named `.files`, else `~/<repository name>`; `[modules.dotfiles] dir` (`~/...` or absolute) overrides it, and the
module records the checkout it used so the next command finds it (step 2). A repository found but not cloned
yet makes the profile path `<clone target>/ubuntu`; the header says `found your dotfiles repository
<owner/name>; it is not cloned yet` and that `init --commit` clones it. `Profile.resolve` takes the discovered
location as its default, after `--profile` and `$UBUNTU_TWEAK_PROFILE`.

### Profile modules

Request 14: a tool that belongs to one person and is not one of the *Author-published tool modules* (a password
manager, a terminal wrapper, a browser tweak) is not engine code. The profile carries it as a module of its own, `<profile>/modules/<name>/module.toml`, with the
same manifest and the same kinds as the engine's (*Modules*): `system`, `builtin` and `local` work as there, a
`local` extension `path` is relative to the module's directory, a `submodule` is refused (it would be a
submodule of the engine checkout). Rules:

- Discovery reads the engine's `modules/` first, then the profile's `modules/`; a profile module with the name
  of an engine module **replaces** it. Everything that lists or plans modules (`list`, `machine`, `show`,
  `doctor`, `apply`, `status`, `init`, `capture`, bundles' `requires-module`) goes through the one
  `Context.manifests()`; `list` labels the source `profile module <name>/<installer>`. A profile that does not
  exist supplies none; a broken manifest is invalid config (exit 2) naming its file.
- Enabled state, options and per-machine switches are the same as for any module: `[modules.<name>]
  enabled`, extra keys reach the installer as JSON in `UBUNTU_TWEAK_MODULE_CONFIG`.
- **The engine never runs profile module code outside the normal plan.** `list`, `machine`, `show` and `capture`
  read manifests only. `apply`/`status` run the module's read-only `check` (and `preview`) exactly like an
  engine module's, print the install command in the plan, and run the installer only with `--commit`, through
  `sudo` when the manifest says `root = true`. Profile code comes only from the profile the user pointed at or
  that discovery found in their own repository.

Tests: `tests/test_profile_modules.py`.

### Author-published tool modules

`passwords-app`, `passwords-app-polkit`, `firefox`, `firefox-policy`, `firefox-autologin`, `firefox-passkey-qr`
and `rig` integrate tools published by the project author: passwords-app (the iCloud Keychain password manager
and its Firefox extension), firefox-passkey-qr, rig and ctmux (installed by rig). They are ordinary modules
(*Modules*); the engine does not hide them, and it does not force them on another user:

- **Default source.** The manifest key `[source] repo = "<url>"` (system modules only) is the one place of a
  module's default URL. The engine hands it to the installer as the option `repo` of
  `UBUNTU_TWEAK_MODULE_CONFIG`; the profile overrides it with `[modules.<name>] repo = "..."` (the profile
  wins):

  ```toml
  # modules/passwords-app/module.toml
  [source]
  kind = "system"
  install = "passwords-app install"
  check = "passwords-app check"
  repo = "https://github.com/alex-mextner/passwords-app.git"

  # profile.toml or machine.toml: use your own fork instead
  [modules.passwords-app]
  repo = "https://example.org/me/passwords-app.git"
  ```

  | Module | Tool | Default `repo` |
  |---|---|---|
  | `passwords-app` | passwords-app | `https://github.com/alex-mextner/passwords-app.git` (public; an `https://` URL only) |
  | `firefox-passkey-qr` | firefox-passkey-qr | `https://github.com/alex-mextner/firefox-passkey-qr.git` (public; an `https://` URL only) |
  | `rig` | rig | **none**: its forge is private. The module is `needs manual: set [modules.rig] repo ...` until the profile sets it |
  | `passwords-app-polkit`, `firefox`, `firefox-policy`, `firefox-autologin` | work on the passwords-app checkout and its extension | none (they clone nothing) |

- **No repo, existing checkout.** A module that has no `repo` and finds its checkout on disk needs nothing: it
  never clones.
- **Another user** switches any of them off with `[modules.<name>] enabled = false`, or replaces one with a
  profile module of the same name (*Profile modules*): a profile module wins over an engine module.
- **The identifiers belong to the tools**: the Passwords extension id and bus name, the polkit action namespace
  and the upstream references are those tools' own, not engine configuration.
  `tests/identity_allowlist.txt` lists each file that carries them as a *tool reference*.

### The rig module

Init step 10 (request 9; the name *Optional modules: rig, dotfiles* in requests 9 and 11 means this section and
*The dotfiles module*). `kind = "system"` with `root = false` (the installer runs as the user, no sudo, see
*Service modules*), `enabled = false` by default, and switched on per machine in the profile:
`machines/<host>/machine.toml`

```toml
[modules.rig]
enabled = true
repo = "<git URL of rig-cli>"                  # required: the module has no default (*Author-published tool modules*)
profile_url = "<git URL of your rig profile>"  # optional; omit when the profile is already set up
```

`modules/rig/install.sh` takes `--plan` (read-only, the `preview`), `--check` (read-only, exit 0 or 3; exit 10
when python3 is missing) and exit 10 for a needed manual step; options arrive as JSON in
`UBUNTU_TWEAK_MODULE_CONFIG` and are never secrets.

| Module (order) | `install.sh` does | `--check` is satisfied when |
|---|---|---|
| `rig` (100) | clones the repository of the option `repo` to `~/dev/rig-cli` (`dir`), fast-forwards a tree without local changes, symlinks `~/.local/bin/rig`, runs `rig profile sync` (or `rig profile init <profile_url>` when none is set up; `profile_sync = false` skips), then `rig activate commit` (`activate = false` skips) | checkout and link are right, the rig profile is in sync (when one exists), `rig activate --json` has no config switch left to write and `rig status` reports no drift |

- **rig does the activating.** `rig activate commit` (rig-cli ticket 745, "rig profile carries tmux and omp
  config; rig activate bootstraps a machine") syncs the profile (config, `~/.tmux.conf` user region, omp
  `config.yml`, masked `mcp.json`, `sysmon.json`; secrets never travel), probes the machine, writes the missing
  `enabled` switches (harness, tmux, harness wrappers, the first-class `ctmux` tool) and applies the
  machine-level plan, listing what the machine cannot support. `ctmux` (github.com/alex-mextner/ctmux-cli) is
  cloned and installed by rig on macOS and Ubuntu (ctmux's own `install.sh` owns the OS gate and `--check`).
  `[deps] apt = ["python3-yaml"]` is what rig reads its config with.
- **Manual steps (exit 10):** no `repo` (`needs manual: set [modules.rig] repo ...`), a clone that fails (forge
  credentials: `fj auth login` / `gh auth login`), a directory or file where the checkout or the symlink should
  go (never replaced), a profile conflict (`rig profile sync --take local|remote`). Anything else non-zero is a
  failure.
- Needs the Base slice's manifest (`[source] root = false`, `preview`, exit 10, `UBUNTU_TWEAK_MODULE_CONFIG`).
  Tests: `modules/rig/tests/test_install.py` (a local git repo and a stub `rig`; throwaway `HOME`).
- **Manual real-run checklist** (root and the forge are not available to tests): `ubuntu-tweak apply` shows
  `run rig: modules/rig/install.sh` with the plan under it; with `[modules.rig] enabled = true` and `repo` set,
  `apply --commit` clones, links, syncs, activates; `rig activate` afterwards lists no config change; `apply`
  again reports `0 changed`; `ubuntu-tweak status` shows rig neither Missing nor Modified; `rig profile status`
  is in sync.

### The `dotfiles` module

Init step 3b (request 9, generalized by request 14, moved before the shell and brew steps by request 20, `order` 35
and `phase = "base"` instead of 110 and `system`; `apply --commit` therefore clones the profile right after the
tools it needs and before `shell-rc` and `brew`). `kind = "system"` with `root = false` (the installer runs as
the user, no sudo, see *Service modules*), **enabled by default** and a machine opts out with `[modules.dotfiles]
enabled = false` in its `machine.toml`. It is a no-op with a clear message when there is nothing to find. Options
(`[modules.dotfiles]` in `profile.toml` / `machine.toml`, JSON in `UBUNTU_TWEAK_MODULE_CONFIG`, never secrets):

```toml
[modules.dotfiles]
repo = "alice/dotfiles"     # optional: OWNER/NAME, a git URL or an absolute path; default: found through gh
dir  = "~/src/dotfiles"     # optional: the checkout; default ~/.files for a repository named .files, else ~/<name>
path = "ubuntu"             # optional: the profile inside the repository; default: ubuntu/, ubuntu-tweaks/, the root
```

`--dotfiles-repo OWNER/NAME` reaches it as `$UBUNTU_TWEAK_DOTFILES_REPO` and wins over `repo`. `modules/dotfiles/
install.sh` takes `--plan` (read-only, the `preview`), `--check` (read-only, exit 0 or 3; exit 10 when python3 is
missing) and exit 10 for a needed manual step. It asks `ubuntu_tweaks/identity.py` (`python3 -m
ubuntu_tweaks.identity resolve`, *Profile discovery*) which repository to use and where, so the engine and the
module always agree.

**rc files from the repository (request 20).** The installer above links nothing; the *plan* does, through
`ubuntu_tweaks/dotfiles_rc.py`. When the dotfiles checkout carries a shell rc file at its root, one of
`.zprofile`, `.zshrc`, `.profile`, and the profile's `files/` does not link that name, the plan links
`~/<name>` to the checkout's file (kinds `dotfiles-link` / `dotfiles-link-replace`, scope `dotfiles`; the
repository's own `installer.sh` installs them the same way, by symlinks from `$HOME`). A real file already
there is copied to `backup/<UTC timestamp>/` first (invariant 5). The profile wins (a name its `files/` links is
never taken from the repository), `--only` drops the links, and `[modules.dotfiles] enabled = false` switches
them off. This is what makes `shell-rc` a pure fallback (*Base modules*). Other files of the repository
(`.gitconfig`, `.tmux.conf`, `.vimrc`, `aliases.zsh`, ...) are not linked: they are the author's and may not suit
this machine (a mac `gpg.program`); only the git identity is read from `.gitconfig` (*git and gh*).

| Situation | `install.sh` | `--check` |
|---|---|---|
| a repository is known and its checkout is absent | `gh repo clone <owner/name> <dir>` when `gh` is logged in, else `git clone https://github.com/<owner/name>` (a public repository); a URL or a path is `git clone`d as given; the checkout is recorded for the next command | drift: the checkout is missing |
| a checkout exists | fetches and fast-forwards it only when it has no local changes | exit 0 when it is a git checkout rooted there, its `origin` is the repository asked for (`same_repo`: URL spellings and case are ignored, two named hosts must agree; skipped when none is known), and it is not behind and not diverged (from the last-fetched state; local changes alone are not drift) |
| gh finds no repository of that name | prints `no dotfiles repository found for <login> (looked for .files, dotfiles, .dotfiles, dot-files): nothing to install; ...`, exit 0 | exit 0 |
| gh is missing, logged out or unreachable and no checkout is on disk | exit 10: `needs manual: not logged in to GitHub: run `gh auth login``; never a prompt | drift (exit 3) with the same text |
| the checkout is another repository (`origin`), a regular file, a directory that is not a checkout, behind with local changes, diverged | exit 10, nothing is changed | drift |

- **It never touches non-profile files itself.** The installer writes nothing in `$HOME` but the checkout and the
  one state file `~/.local/state/ubuntu-tweaks/dotfiles.json` (*Profile discovery*), links nothing, and
  fast-forwards only a tree without local changes; tests assert a `.zshrc` in the repository stays byte-identical
  and `$HOME` holds only the checkout. The shell rc files are linked by the plan (below), not by the installer.
- **The profile arrives in the same run, before the shell and brew steps.** The plan of `init`/`apply` is built
  before the clone, so it holds the modules only and its header says the repository is not cloned yet. When the
  profile was missing, `apply --commit` clones it **first**: the BASE-phase actions that sort before the `dotfiles`
  module (the tool installs: `git`, `gh`), then the module's action, and no other action runs before it. Then it looks for the profile again (`identity.locate`), re-resolves the
  configuration, prints `profile: <dir> (cloned by this run)` and rebuilds the plan with it, so a profile that
  disables a module is honoured before that module runs, and one `init --commit` on a fresh machine applies the
  settings too. No other system module triggers it. If the clone failed, the run ends as before, with the
  manual step.
- **`[modules.dotfiles] dir` wins.** A configured `dir` that holds a git checkout without a profile in it is
  still the module's directory: it is fast-forwarded, never cloned over.
- Tests: `modules/dotfiles/tests/test_install.py` (a local git repository, a stub `gh` executable, throwaway
  `HOME` and state directory), `tests/test_apply_commands.py` (the same-run profile).
- **Manual real-run checklist** (the network is not available to tests): with `gh auth login` done and a
  repository `<login>/.files` holding `ubuntu/profile.toml`, on a machine without `~/.files`: `ubuntu-tweak
  apply` names the repository and says it is not cloned; `apply --commit` leaves `~/.files` with the profile
  and applies it in the same run; `apply` again reports `0 changed`; with `gh` logged out the header and the
  closing manual block say `gh auth login`.

### Panel coverage

Every GNOME Settings panel gets an explicit disposition. `S` = shared, `M` = per-machine
overlay, `X` = never captured (secret, identity, hardware), `F` = file adapter, `R` = root
adapter. The panel file ids are the first column; `ubuntu_tweaks/panels.toml` holds the same
table plus the ordered dconf path globs (`*` is one path segment, `**` any number; **first
matching rule wins**, specific before general) and a unit test fails when this table and
`panels.toml` disagree.

| Panel (file id) | dconf paths and files (see `panels.toml` for the exact globs) | Disp. |
|---|---|---|
| `network` | `/system/proxy/**`; `/org/gnome/nm-applet/**` and NetworkManager connections are `identity`, never captured | `S/M` |
| `bluetooth` | `/org/gnome/bluetooth/**`, `/org/blueman/**`: pairings are hardware | `X` |
| `background` | `/org/gnome/desktop/background/**`, the screensaver picture and colours; an absolute path outside `/usr/share` is flagged and goes to the machine overlay | `S` |
| `appearance` | `/org/gnome/desktop/interface/**` (theme, color-scheme, accent, fonts, cursor, icons) except the keys other panels claim; `user-theme` | `S` |
| `notifications` | `/org/gnome/desktop/notifications/**` including per-app children | `S` |
| `search` | `/org/gnome/desktop/search-providers/**`, Tracker | `S` |
| `multitasking` | `/org/gnome/mutter/**`, `/org/gnome/desktop/wm/preferences/**`, `enable-hot-corners`, shell overrides and switchers | `S` |
| `apps` | `~/.config/mimeapps.list` (file adapter) and the preferences of GNOME core apps: Files, Ptyxis, Papers, File Roller, Clocks, Calendar, GTK file chooser | `F` (+`S` keys) |
| `privacy` | `/org/gnome/desktop/privacy/**`, `/org/gnome/desktop/screensaver/**` (lock), `/org/gnome/system/location/**`, lockdown | `S` |
| `online-accounts` | `/org/gnome/online-accounts/**`: tokens and identities | `X` |
| `sharing` | `/org/gnome/desktop/remote-desktop/**`, file sharing; TLS keys, certificates and credentials are `secret` | `S` (credentials `X`) |
| `sound` | `/org/gnome/desktop/sound/**`, the startup sound; keys named `*-device*`, `default-sink*`, `default-source*` go to the machine overlay | `S/M` |
| `power` | `/org/gnome/settings-daemon/plugins/power/**`, `/org/gnome/desktop/session/**`, `show-battery-percentage` (`last-selected-power-profile` is `state`, never captured) | `M` |
| `displays` | night light and the colour plugin (`/org/gnome/settings-daemon/plugins/color/**`); `night-light-last-coordinates` is `state` (derived from the location, rewritten by the daemon, never captured); `~/.config/monitors.xml` (file adapter, machine overlay) | `S/M/F` |
| `mouse-touchpad` | `/org/gnome/desktop/peripherals/{mouse,touchpad,trackball}/**` | `M` |
| `keyboard` | input sources, `wm/keybindings`, media-keys (custom bindings), `mutter/keybindings`, `shell/keybindings`, global shortcuts, tiling-assistant `tile-*`/`restore-window`/`center-window`/… bindings, IBus, peripherals keyboard | `S` |
| `printers` | `/org/gnome/cups/**`: queues are machine and network specific | `X` |
| `removable-media` | `/org/gnome/desktop/media-handling/**` | `S` |
| `color` | `/org/gnome/color-manager/**`: ICC profiles belong to the display | `X` |
| `region-language` | `/org/gnome/system/locale/**`; the system `locale` is a root adapter (`system.toml`) | `S/R` |
| `accessibility` | `/org/gnome/desktop/a11y/**`, `toolkit-accessibility`, `text-scaling-factor` | `S` |
| `users` | `/org/freedesktop/accounts/**`: accounts | `X` |
| `date-time` | `/org/gnome/desktop/datetime/**`, `interface/clock-*` (including `clock-format`), `/org/gnome/shell/world-clocks/**`; the `timezone` is a root adapter in the machine overlay's `system.toml` | `S/R` |
| `about` | hostname and device name are not in dconf; `/etc/hostname` is never captured | `X` |
| `ubuntu-desktop` | Ubuntu Dock (`dash-to-dock`), `ding`, tiling-assistant behaviour, app grid folders, `favorite-apps` | `S` |
| `wellbeing` | `/org/gnome/desktop/break-reminders/**`, `/org/gnome/desktop/screen-time-limits/**` | `S` |
| `shell-extensions` | enabled/disabled lists and every other extension schema: handled by *modules*, not by panel files (`excluded-by-rule`) | modules |

Decisions where the contract listed a key twice: `clock-format` belongs to `date-time` (where
GNOME Settings shows it); tiling-assistant's bindings go to `keyboard` and its behaviour keys
to `ubuntu-desktop`; the Dock lives in `ubuntu-desktop`, not `appearance`. The Settings
*Wellbeing* panel has its own id (`wellbeing`); keys of installed third-party and further GNOME
apps (Resources, Deja Dup, Seahorse, Passwords, Characters, Disks, Calculator, Showtime, Simple
Scan, Logs) belong to `apps`, so `unmapped` only lists schemas no rule knows (ticket #28).

### Per-machine

- The machine key is the hostname; `machines/<machine>/` is the overlay described above.
- Default `machine` rules ship with the engine (`ubuntu_tweaks/capture/defaults.toml`): power
  plugin keys including `battery-*`, `peripherals/touchpad`, `peripherals/mouse`,
  sound device keys, absolute
  filesystem paths outside `/usr/share` (a string leaf of the value that starts with a real
  filesystem root such as `/home` or `/opt`, or a `file:///` URI to one; a dconf object path
  like `/org/gnome/.../custom0/` is not a file),
  `~/.config/monitors.xml`. A panel whose disposition is `M` (`power`, `mouse-touchpad`) also
  routes its keys to the overlay. An explicit `machine` match protects a key from the `state`
  globs, a panel `M` does not.
- `ubuntu-tweak machine` prints the detected name, whether the overlay exists, and which
  module setting, bundle and linked file comes from the overlay and which from the shared
  profile. A bundle name present in both layers is reported as `overlay over shared`; a linked file
  present in both is reported as `overlay (shadows shared)`.
- Modules are per-machine too: `[modules.memguard] enabled = false` in a machine overlay.

### capture

`ubuntu-tweak capture` is the read-only inventory of the live system, printed as the exact
diff the profile would receive. `--dry-run` is the explicit no-write mode: the same as the
default preview, and it excludes `--commit` (exit 2, *Dry run*); it also refuses `--report FILE`, which writes a file (exit 2). `--summary`
prints only the SUMMARY block and the totals line and cannot be combined with `--commit` (exit 2):
nothing is written that was not shown in full. With `--commit` it writes into the profile working tree and
does not run `git commit`: the user reviews `git diff` in `.files` (the second preview) and
commits through the repo's PR flow. `--report FILE.md` writes the same report as markdown
(full values; the terminal clips a cell at 80 characters). `-v` also lists entries equal to
the profile, already-linked files and every cache-state candidate. `--only` takes panel ids
(and `shell-extensions`, to audit the extension skip rules) and the section names `files`,
`packages`, `system`; a section not named is not computed and an unknown name is invalid
config (exit 2).

The report opens with the **SUMMARY** block (also printed alone by `--summary`): the keys in scope
accounted as captured + skipped + unmapped + at default; captured keys per destination bundle
(new / differ / same) and per Settings panel (captured, skipped, keyfile overrides); the skipped
classes with counts; the keyfile overrides (per panel: the keys whose effective value comes from
`glib-2.0/settings/keyfile`); files, packages, system and secret-finding counts; the unmapped
schemas. Then, in this order:

1. **Settings → bundles**: `settings/<bundle>.ini` (shared) and `machines/<machine>/settings/<bundle>.ini`
   (machine), grouped by destination bundle (default name = panel id; see the routing order
   below): every key that has a user value differing from the schema default (a user value equal
   to the default, as Gio computes it including system overrides, is only counted and is never
   captured or written: the profile holds what the user changed). Marks: `+` new in the
   profile, `~` differs from the profile value (the old value and the reason comment above the
   key are shown), `=` already in the profile (hidden unless `-v`). Values are read through
   the schema (typed), written as GVariant text. **Keyfile overrides**:
   `~/.config/glib-2.0/settings/keyfile` is parsed through the schema; a key it sets to a value
   different from the dconf value (or the default) is listed in its own table with the keyfile
   value, the dconf-side value and whether the override is the effective value in the capturing
   process. Overrides are never written into a bundle (that would freeze them into dconf);
   a captured key that an override also touches carries the `override` note. The keyfile
   itself is an allow-listed file (section 2).
2. **Files → `files/…`**: only allow-list candidates (`include_files` plus the built-ins
   `mimeapps.list`, `user-dirs.dirs`, `gtk-3.0/bookmarks`, `gtk-3.0/settings.ini`,
   `gtk-4.0/settings.ini`, `glib-2.0/settings/keyfile`, `monitors.xml` (machine overlay),
   `autostart/*.desktop`, hand-written dotfiles the user chose; globs relative to `$HOME`, an
   absolute path or `..` is refused with exit 5), plus a `candidates` listing of everything
   else in `~/.config` and the `$HOME` dotfiles with a class and a reason (below), so the user
   can promote an entry by adding it to `include_files`. Nothing outside the allow-list is
   ever adopted. Each adopted file shows size, destination, the `backup+symlink` action and
   `new` / `identical` / `differs (+a -b lines)` against the profile copy; a file that is
   already a symlink into the profile is `linked` (its profile copy is still scanned for
   secrets), a dangling link into the profile, a symlink elsewhere, a file over 512 KiB, a
   non-UTF-8 or an unreadable file is listed as *not adopted* with the reason.
3. **Packages** (a `packages.toml` proposal: `apt-mark showmanual` minus the installer
   baseline, minus `required`/`important` packages and the `[packages] ignore` globs; snaps
   without base and seeded ones; flatpak apps, listed per installation: `--system` always, `--user`
   only when `$XDG_DATA_HOME/flatpak` exists, with flatpak's cache redirected to a temporary
   directory, because a plain `flatpak list` creates `~/.local/share/flatpak`) and **System** (a `system.toml` proposal:
   timezone into the machine overlay, locale and console keyboard shared).
4. **Skipped**, with a reason class: `secret`, `identity`, `hardware`, `state` (volatile runtime keys an app or
   the daemon rewrites by itself, never captured: window geometry `window-*`, recents, app-picker-layout,
   `last-*` / `*-last-*` / `last-selected-*` markers, `prompt-check`, timestamps, and
   `night-light-last-coordinates`, which is derived from the location and would put a home position into the
   profile; the `[state_keep]` globs exempt real preferences, a profile `[capture] include` or an explicit
   `machine` match lifts a key back), `auto` (keys the system or an app
   registered by itself: notification app ids and lists, Ptyxis profile uuids, Clocks UI state,
   the Clocks mirror in `world-clocks`, portal global shortcuts, the input-source MRU order, the
   Shell's seeded app-grid folders, an extension's version marker; the section lists each key with
   its reason and says how to promote it), `no-schema`,
   `excluded-by-rule`; counts per class plus each key and its reason, never its value. The
   **`unmapped`** bucket lists user-valued keys that no panel rule claims (nothing is captured
   for them), so the user sees what the table misses. Keys equal to the schema default are
   only counted.
5. A totals line; exit 0. `--commit` refuses with exit 5 (`refused: secret finding`) when the
   output scan finds anything, and writes nothing.

Every dconf key that has a user value is accounted for exactly once (captured, skipped,
unmapped or at default); the totals line carries the number of user keys in scope (with
`--only`, only the selected panels' keys are counted) so this is
checkable. Decision order for one key: profile `exclude` → built-in `secret`/`identity`
exclusion globs → the panel rule (a `skip` rule gives its own class) → `state` (unless an
explicit `machine` rule or the profile's `include` claims the key) → `auto` (same exemptions) →
no schema → value secret scan → no panel (`unmapped`) → target (machine when a machine rule, an
`M` panel or an absolute path in the value says so) → **destination bundle**: (1) the bundle that
already holds the key in the profile for this machine (it stays there, in its layer: a key the
user grouped is never moved, and a key held under another panel's overlay file is marked `=` or
`~`, never `+`: ticket #37), else (2) the first `[capture.bundles]` entry whose glob matches, else
(3) the panel's own bundle. A key the profile already holds is never skipped as `state` or `auto`
(it is the user's choice). A new key that a shared bundle *not applying here* (another machine's
`machines` header, a disabled `requires-module`) already holds goes to the machine overlay, not the
shared layer, so the other machine does not meet a conflict. A new key whose destination bundle
exists but does not apply on this
machine (`machines`, `requires-module`) is invalid config (exit 2). The preview shows the
destination bundle of every key. The `state` globs never apply under `[state_keep]` (keybindings, privacy,
accessibility, interface, peripherals, window-manager preferences, mutter and tiling-assistant
keys: names such as `activate-window-menu`, `dwell-time` or `remember-recent-files` look like
state but are preferences). `auto` is skipped like `state` unless a `machine` rule or the profile's
`include` claims the key. A value in the keyfile-override
table is masked when its key is excluded by the profile, a built-in `secret`/`identity`/
`hardware` rule or a panel rule of such a class, or when it matches a secret pattern.

`system.toml` items are compared with the layer they are written to: the timezone always goes
to the machine overlay; locale and keyboard go to the shared file unless the overlay already
sets that key (a shared write would never take effect), in which case they replace it there.

**Secret scan.** Patterns: PEM blocks, API tokens (`ghp_`/`gho_`/…, `github_pat_`, `sk-`,
`AKIA`, `xox*`, `glpat-`, `AIza`, `npm_`), JWTs, e-mail addresses, credentials in URLs, long
hex (40+) and mixed-case base64 (40+, at most two `/`, so a long file path is not a token)
runs → class `secret`; private LAN addresses → class
`identity`. The scan runs on every dconf value, every adopted file and, at `--commit`, on the
exact text about to be written (the edited bundles, `packages.toml`, `system.toml`). A
hit in a dconf value skips the key; a hit in an adopted file or in the output refuses the
commit. A finding carries only its kind and line number, so reports and errors never contain
the matched text. E-mail matches are ignored for the keys listed in `[secrets]
email_exempt_keys` (`enabled-extensions`, `disabled-extensions`: extension uuids look like
addresses). A full-line comment that the file already had before the commit (a reason the user wrote
and reviewed in git) is not scanned again; every value and every new line is.

**Candidate classes** (decided from name, size, extension and the *names* of keys inside small
text files; values are scanned but never printed): `secret-bearing` (credential-looking file
name, a credential-store or browser-profile directory, or a secret pattern inside),
`cache-state` (binary, over 64 KiB, empty, log/db/lock/history/backup names, cache
directories), `mixed` (credential-looking key names without a secret value, or a directory
with 60+ files: state and config interleaved), `portable-config` (everything else: small
text). Directories named in `secret_dirs`, `cache_dirs` or `mixed_dirs` (`~/.local`, which holds
the keyrings) are one row and are never read.

**`--commit`** checks everything first and writes only then: each captured key goes into its
destination bundle **in place** (a changed value is rewritten where it stands, a new key is
appended to the end of its section, a missing file or section is created; every comment, the key
order, the spacing and the unrelated keys of the file stay as they are; keys the live system no
longer has stay) and the file is written atomically; an adopted file is copied into `files/` (or
`machines/<machine>/files/`), the original is copied to the backup directory (invariant 5) and
replaced atomically by an absolute symlink to the profile copy, so a failure before the last step
leaves the original untouched; `packages.toml` gets the proposed names appended to their array
(existing order and comments untouched) and `system.toml` the proposed values replaced or added in
place, both edited by `toml_edit.py` and re-parsed and compared with the intended data before
anything is written (ticket #29). Before anything is written the commit refuses (exit 5, nothing
written) a directory that resolves outside `$HOME` or the profile, a destination that is a symlink
or a directory, and an adopted file whose bytes differ from the ones scanned for the preview: what
is written is exactly what was shown. Each file is written atomically; a failure part-way stops at
that file and a rerun finishes the rest. The command never runs git.

Selection is deterministic rules (engine defaults plus `[capture]` in the profile), not an
interactive picker, so a preview is reproducible and reviewable and the rules live in the
repo. The profile's `[capture]` takes `exclude` (globs, `excluded-by-rule`, checked first),
`machine` (globs), `include` (globs that lift the `state` and `auto` skips for the keys they
match), `bundles` (a table, bundle name → list of globs: where new keys go), `include_files` and
`machine_files` (`$HOME`-relative globs); dconf globs are written with or without the leading `/`
(`org/gnome/x/**` is `/org/gnome/x/**`); an unknown key is invalid config. A profile cannot weaken
the built-in `secret`/`identity`/`hardware` exclusions: they are evaluated before anything the
profile adds, and `include` never lifts them.

### show

`ubuntu-tweak show` answers the first sentence of request 9: a readable Russian overview of **all
settings of a machine** with explanations and current values. It is read-only (the only file it
ever writes is `--output`) and has no `--commit`.

```
ubuntu-tweak show [--changed | --all] [--panel ID] [--format text|md|html] [--output FILE]
                  [--ssh [USER@]HOST [--ssh-option KEY=VALUE ...]]
```

- **Grouping.** By GNOME Settings panel, in `panels.toml` order, with the Russian panel titles of
  `ubuntu_tweaks/show/descriptions_ru.toml`. Panel membership is the capture rules'
  (`capture.rules.Rules.panel_rule`): `show` has no table of its own. Keys no rule claims go to the
  last section, `other` (Прочее). Panels with nothing to show are listed in one line, not as sections.
- **Per key**: the dconf path, a Russian explanation, the current effective value in a readable form,
  the default, and a status: `изменено вами` (user value differs from the default, or the key has no
  schema to compare with), `задано файлом-переопределением` (no user value, but
  `~/.config/glib-2.0/settings/keyfile` sets it; the override then *is* the default shown) or
  `по умолчанию` (shown only with `--all`, also for a user value equal to the default).
  `--changed` (default) lists changed and overridden keys; `--all` adds every key of every
  fixed-path schema (instances of relocatable schemas only when they have a user value).
  `--panel ID` limits to one panel id (or `other`); an unknown id is invalid config (exit 2).
- **Explanation.** The hand-written text of `descriptions_ru.toml` (`[keys."<schema>"]`, `[[rule]]`
  families such as window sizes, per-application notification settings, the retired dash2dock-lite).
  A key without a hand-written text falls back to the schema's own `<summary>` marked
  `(описание из схемы, без перевода)`, and a key with no schema on the machine says so. The file
  is validated at load (a wrong type is exit 2) and a test requires Cyrillic text in every entry.
- **Readable values** (`show/values.py`, golden-tested): booleans `Вкл`/`Выкл`; durations as
  `5 мин`, `1 ч 5 мин` (a key's `unit` is `seconds|minutes|ms|percent|pixels|size|unix|bytes`, `zero`
  names the value 0: `никогда`); lists as one bullet per item; enum values as `nick — meaning`
  (`[enums."<schema>"."<key>"]`); colours `#rrggbb` (also `#rrggbbaa`, GDK 12-digit and `rgb()`) with a
  swatch in HTML; keybindings as `Super + ←`, `Ctrl + Alt + T`, `не назначено`, `отключено`; window
  sizes `1200 × 800 пикс.`; Unix timestamps as UTC.
- **Masking.** A key is shown as `скрыто` (nothing of its value is kept in the report, not even the
  raw text) when the capture rules class it secret/identity/hardware (`Rules.hard_exclude`, or a
  `skip` panel rule of those classes: gsconnect pairing, RDP key/cert, Wi-Fi identities, accounts)
  or when the capture secret scanner (`scan_value`) finds a token, key block, e-mail or LAN address
  in the value. A hidden key shows only its key name (`…/tls-key`), because its directory can carry
  an identity (a device id, a connection uuid). Four or more instances of one relocatable key fold
  into one row of `instance: value` lines (hidden instances: `скрыто`); the counts in the summary
  are of the rows shown, after folding.
- **Outside dconf**, as extra sections: *Файлы настроек* (default applications from
  `mimeapps.list`, monitors from `monitors.xml` without serial numbers, autostart entries by name
  and enabled/disabled state, never their commands), *Время, язык и клавиатура*
  (`timedatectl show`, `localectl status`), *Пакеты* (counts only: apt total, apt manual, snap) and
  *Модули ubuntu-tweaks* (`list` data; local runs only). A missing tool or file is `нет данных`.
- **Formats.** `text` (default) and `md` go to stdout unless `--output` is given; `html` is one
  self-contained file written to `--output`, default `/tmp/ubuntu-tweak-show-<machine>.html`
  (mode 0600): inline CSS only, no JavaScript, no external request, a table of contents of
  anchor links, one `<details>` per panel and extra section, light and dark through
  `prefers-color-scheme`, all text HTML-escaped.
- **`--ssh [USER@]HOST`** shows another machine without installing or copying anything to it. The
  probes run there through `ssh -o BatchMode=yes` (keys and agent, never a password prompt;
  `--ssh-option KEY=VALUE` adds `-o KEY=VALUE`, repeatable): `dconf dump /` (which reads the user's
  database without a session bus), `cat` of the override keyfile, `mimeapps.list`, `monitors.xml`,
  `timedatectl show`, `localectl status`, `apt-mark showmanual`, `dpkg-query -W`, `snap list`. Schemas,
  descriptions and masking rules are the *local* machine's, so a key whose schema only exists on
  the remote shows as raw GVariant text; the module section is omitted. The report label is the
  `--machine` name when given, else the ssh host. A target that starts with `-` or contains
  whitespace is invalid config.
- **Code**: `ubuntu_tweaks/commands/show.py` (wiring), `ubuntu_tweaks/show/` (`collect`, `values`,
  `descriptions` + `descriptions_ru.toml`, `catalog`, `system`, `ssh`, `model`, `render`,
  `html_page`). Tests: `tests/test_show.py` (value formats, descriptions file, a `FakeBackend`
  scenario with masking, folding and overrides, the three renderers, the extras with a fake
  runner, the ssh argv) plus a manual live read-only run on the machine.

### Plan and actions

`apply` builds an ordered list of actions, prints it, and with `--commit` runs it. Every
action has `check()` (already satisfied?) and `run()`; the plan holds only actions whose
check fails, so the preview is exactly what `--commit` will do. After an action runs, its
`check()` must hold; a plan that does not converge fails instead of reporting `changed`.

Order: **base (init steps 1-4)** → submodules → migrations → schemas → **packages** → **system (root)** →
symlinks (extensions) → **files (links)** → enable/disable → settings (module presets, then shared
bundles, then machine overlay bundles) → seeds. Inside a phase actions are ordered by `order` (lower
first, default 100, stable otherwise); see *Init pipeline*.

| Action (`kind`) | Check | Run |
|---|---|---|
| `submodule` | submodule is initialized (`<path>/.git` exists) | `git submodule update --init --recursive -- <path>`; a submodule checked out at another commit (dev work) is reported by `status` as an extra, never reset — only `update` moves it |
| `migrate` | see *Migrations* | copy values, retire old uuid |
| `compile-schemas` | `gschemas.compiled` newer than every `*.gschema.xml` (satisfied when the extension has no schema) | `glib-compile-schemas` |
| `package` | apt / snap / flatpak name is installed | install the missing names; never remove |
| `login-shell` (root) | the passwd entry's shell is named like the wanted one (`getent passwd <user>`) | `chsh -s /usr/bin/<shell> <user>` through sudo |
| `shell-rc` | the file exists (a dangling symlink counts), or the profile or the dotfiles checkout provides it | create it with `O_EXCL`, only while absent; planned only when neither the profile's `files/` nor the dotfiles checkout carries that file |
| `git-config` | `git config --global --get <key>` equals the profile value | `git config --global <key> <value>` |
| `git-credential` | the global `credential.https://<host>.helper` list names `gh auth git-credential` | `gh auth setup-git --hostname <host> --force` |
| `gh-login` | `gh auth status --hostname <host>` exits 0 | `keychain exec --stdin <ref> -- gh auth login --with-token`; no token: `needs manual` |
| `brew-prefix` (root) | the prefix is a directory owned by the user, or `bin/brew` exists | `install -d -o <user> -g <group> <prefix>` through sudo |
| `brew-install` | `<prefix>/bin/brew` exists | download the pinned installer, refuse it unless its SHA-256 matches, run it with `NONINTERACTIVE=1` |
| `post` | the `[[post]]` entry's `check` argv exits 0 | its `run` argv (through sudo when `needs_root`) |
| `system` (root) | `timedatectl`, `localectl` or `/etc/default/keyboard` matches | the same through sudo |
| `system-module` (root) | a `system` module's checker exits 0 | its installer through sudo |
| `link` | `~/.local/share/gnome-shell/extensions/<uuid>` (uuid from `metadata.json`) or `~/<path>` is a symlink to the source | `ln -sfnT` |
| `link-replace` | same check; the destination exists as a real file/directory or a symlink elsewhere | copy it to `~/.local/state/ubuntu-tweaks/backup/<UTC timestamp>/<absolute path>` (one helper, `backup.py`, shared by extension links, profile files and `capture --commit`), remove it, then `ln -sfnT`; the plan line says `backup+replace` and whether the content is identical |
| `enable` | uuid is in the user value of `enabled-extensions` and not in `disabled-extensions` | add / remove the one item |
| `disable` | a conflict is not in `enabled-extensions` and is in `disabled-extensions`; a retired uuid is not in `enabled-extensions` | remove / add the one item |
| `set-key` | effective value (user value, else schema default) equals the wanted typed value; the preview line ends with the bundle's reason comment when it has one | write it |
| `seed-key` | key has a user value | write the provider's value |

Root actions are marked `[sudo]` in the plan; `--no-sudo` skips them and reports them as
skipped.

Every action also has `why()`: one sentence on why this kind of action exists (default
`ui.kind_why(kind)`; an action overrides it when the reason depends on its state). The interactive
preview and `status` show it; the plain text does not. `Plan.execute` takes optional `on_start` and
`on_done` callbacks (the progress lines of `apply --commit`).

Writes go through `Gio.Settings` from `python3-gi` (installed on every Ubuntu desktop;
`doctor` checks it), with the extension's own schema dir via
`Gio.SettingsSchemaSource.new_from_directory`. The rest is stdlib: `tomllib`, `argparse`,
`subprocess` for git and apt.

`enabled-extensions` and `disabled-extensions` are edited as single-item additions and
removals of the **user value** (`get_user_value`), never by writing back the effective
value: on Ubuntu the effective value comes from the session's override and writing it back
would freeze it (the reason `install.sh` used `dconf` directly). `enable` adds the uuid to
`enabled-extensions` and drops it from `disabled-extensions`; for each `[extension]
conflicts` uuid `disable` drops it from `enabled-extensions` and adds it to
`disabled-extensions` (the Shell lets `disabled-extensions` win over a system default);
`retire` only drops the uuid from `enabled-extensions`. A user value that is unset counts
as an empty list, so the first single-item write creates it.

Planner discovery: every `ubuntu_tweaks/planners/<name>.py` exposes `build(ctx, backend)`,
returning all candidate actions (satisfied or not; `Plan.build` filters), and optionally
`extras(ctx, backend)`, returning human-readable lines for the *Extra* drift section.
`apply` and `status` discover all planners, so adding a planner is dropping a file. Actions
of a submodule module resolve the extension's `metadata.json` lazily (at `check()`/`run()`
time): before the submodule is initialized their `check()` is false and their description
says the uuid is known after the submodule init, so one `apply --commit` converges.

### Drift

`status` compares declared state with the machine, using the same `check()` as `apply`, and
classifies each pending action by its `kind`: `set-key` and `link-replace` are **Modified**,
every other kind is **Missing**. It prints a `machine:` line (the name, and whether an
overlay directory exists or only the shared profile applies) and exits 3 when anything is
Missing or Modified.

- **Missing** (declared, not on disk): a symlink, an uninitialized submodule, stale
  schemas, an enabled uuid, a conflict still enabled, an unwritten seed, a package.
  `apply --commit` fixes it.
- **Modified** (declared, different on disk): a key whose value differs, a linked file or
  extension directory replaced by a real file or pointing elsewhere (`link-replace`).
  Values are compared as typed `GLib.Variant`s read through the schema, so `5`,
  `uint32 5` and an unset key at its default compare correctly; a raw `dconf read`
  string diff would report every default-valued key. `apply --commit` overwrites it (after
  a backup, for files); the user's own changes belong in the profile (`capture` brings
  them there).
- **Extra** (on disk, not declared), reported only, never changed, never an exit-3 reason:
  extensions enabled by the user that no module declares, symlinks into this repo for
  uuids no module declares (e.g. the old dock uuid before the migration ran), and a
  submodule checked out at another commit than the umbrella records (dev work: only
  `update` moves it, `apply` never resets it). Extras come from each planner's optional
  `extras()`; `status` prints them only when `--only` is not given.
- **Manual** (shown by `status`, never an exit-3 reason): the planners' warnings, things `apply` cannot
  decide or do for you (a missing git identity). The explained layout lists them in their own section
  (*Output*).

### Reconcile and failover

Request 21. `apply --commit` and `init --commit` are a **level-triggered reconcile**: observe the actual state of every
declared item (`check()`), act on the difference, observe again (`check()` after `run()`), and repeat until the
plan is empty or the bounded number of passes is used. The loop never trusts what it did, only what it observes, so a
run is idempotent (a second one reports `0 changed`) and safe after a crash.

**One failure never aborts the run.** `Plan.execute` catches the failure of an action, classifies it and goes on:

| Class | Cause | What the engine does |
|---|---|---|
| transient | `Transient` raised by the action, or a `CommandError` whose output names a network / lock / busy cause (`could not resolve`, `temporary failure`, `timed out`, `connection reset|refused`, `could not get lock`, `dpkg frontend lock`, `try again`, `HTTP 429/502/503/504`), or an installer exit **75** (`EX_TEMPFAIL`) | retried with exponential backoff and full jitter (`RetryPolicy`: 3 attempts, base 2 s, cap 30 s); after the last attempt it is handled as permanent and the report says it was transient |
| permanent | any other `ActionFailed` / `UbuntuTweakError` / `OSError`, or a `run()` that left `check()` false | not retried; the fallbacks are tried, then it is a **failed** item |
| needs-human | `ManualStepRequired`, installer exit 10 | reported in the `Manual steps` block, not a failure |
| root-unavailable | root is needed and `RootSession.require` raised `RootUnavailable` | the run does **not** stop: every user-level action still runs, the root actions are deferred and listed as the **root remainder** with the cause and the exact command |

**Dependencies.** An action that cannot succeed because something it needs failed is **blocked**, not tried: its line
says `blocked by <kind> <what>`. Two sources: the module manifest key `requires = ["module", ...]` (every action of
a module is blocked when any action of a required module failed or was blocked), and the extension chain inside one
module (a failed `submodule`, `compile-schemas`, `link` or `link-replace` blocks the later `enable` and settings
actions of the same module: they cannot work without the code). Everything independent still runs.

**Fallbacks.** A module of source kind `system` may declare alternatives to its installer:
`[[fallback]] install = "install-user.sh"` (a program inside the module, like `[source] install`), optional
`root` (default: same as `[source]`), optional `description`. When the primary installer fails permanently, or
needs root and root is unavailable (or `--no-sudo` was not given but sudo failed), the fallbacks run in order, each
verified by the module's own `check`; the first that makes `check` pass wins and the line says which strategy
was used (`via fallback install-user.sh`). `--dry-run` lists the declared fallbacks under the action. Package
managers: `packages.toml` may list `[fallback]` managers per manager (`apt = ["brew", "flatpak"]`): the names the
primary manager could not install are offered to the next manager in order; a name a manager does not know is skipped,
not failed.

**Run journal.** `<state>/ubuntu-tweaks/run-journal.json` (next to the session logs) is written before the first
action and after every action: run id, start time, the commit command, and per action its key, kind, text and status
(`pending`, `done`, `failed`, `blocked`, `deferred`, `manual`) with the cause. A run that finds an unfinished journal
(crash, Ctrl-C, power loss) says so first (`previous run was interrupted: N done, M pending`) and simply continues:
convergence needs no replay because every action is re-observed. A finished journal is kept as the record of the
last run; `status` prints its failed / blocked / deferred items under **Last run**.

**Summary and exit code.** The run ends with `N changed` and one list per class (converged count, failed with the
cause and the next command, blocked with the blocker, root remainder, needs-human). Exit codes: `0` everything
declared and runnable converged (needs-human steps and root actions skipped by an explicit `--no-sudo` do not
change it: the user or a human owns them), `7` (`ExitCode.PARTIAL`) at least one action failed or was blocked, `6` root was
needed, could not be obtained and root actions remain, `3` the plan still holds an item after the last pass,
`1` internal error. When several apply the order is 7, 6, 3. Everything is in the session log (*Session log*).

**Self-heal.** Because the loop is level-triggered, a re-run puts back what was applied before and drifted (an
extension the shell disabled, a replaced link, a stopped service). Unattended re-runs are the opt-in module
`autoheal` (off by default; `[modules.autoheal] enabled = true` in the profile or machine overlay): a user systemd
timer runs `ubuntu-tweak apply --commit --no-sudo` at login and daily. It never asks for root and never prompts;
root actions are left for the next interactive `apply --commit`. `apply --commit` both enables the units (the login
run) and (re)starts the timer at once (`systemctl --user restart autoheal.timer`: a running timer keeps its old
calendar until restarted; never the service, which would apply inside the apply that installs it), and the check
requires the timer to be active, so it assumes a reachable user systemd manager (as the `is-enabled` check already
did): a timer that is only enabled would not fire until the next login ([#95](https://github.com/alex-mextner/ubuntu-tweaks/issues/95)).

### Coverage

Request 22. Every configuration location of the machine falls into one class; the engine has one scanner and one
adapter interface, and `ubuntu-tweak coverage` shows the table:

| Class | Meaning |
|---|---|
| `tracked` | declared in the profile (bundles, files, packages, system, modules) and reconciled by `apply`, or owned by a module (`[[coverage]] class = "tracked"`) |
| `excluded` | deliberately not tracked, with a written `reason` (cache, state, generated, machine-bound) |
| `secret` | credentials: lives in the keychain / `passwords-app`, never in the profile; the scanner reports that it exists, never its value |
| `untracked` | a finding: configuration nobody declared. `coverage` exits 3 while any without a `ticket` remains (*Ticketed untracked*) |

**Items.** An adapter *discovers* items: `Item(adapter, id, kind, detail)`, `id` is a stable string with a scheme:
`dconf:/org/gnome/...` (a key whose user value differs from the schema default), `file:~/.config/<entry>` (a
top-level entry of `~/.config`, `~/.local/share`, `~/.local/bin`, and the dotfiles of `$HOME`), `etc:/etc/...`
(a changed conffile, `/etc/default`, system units, polkit, sudoers.d, apt sources: read without root where
the file allows it, else reported as `needs root to read`), `apt:<name>` (manually installed packages), `snap:`,
`flatpak:`, `brew:`, `pipx:`, `uv:`, `npm:`, `bun:`, `cargo:`, `unit:<name>` (enabled systemd user units and
timers), `autostart:<name>`, `extension:<uuid>`, `cron:`, `ssh:`, `git:`.

**Adapter interface** (`ubuntu_tweaks/adapters/<name>.py`, found like planners: dropping a file registers it):

```python
NAME = "files"                                   # the id scheme this adapter owns
def discover(ctx: Context) -> Iterable[Item]     # what exists on the machine, read-only
def classify(ctx: Context, item: Item, rules: Rules) -> Finding | None
                                                 # None: not mine / no opinion; default rules apply
def capture(ctx: Context, items: Sequence[Item]) -> Iterable[Proposal]
                                                 # live -> profile text: only non-default values, with reasons,
                                                 # grouped by purpose ("packages"-style bundles)
def plan(ctx: Context, backend: SettingsBackend) -> Sequence[Action]
                                                 # reconcile: actions with check()/run(), failover alternatives
```
Only `discover` is required; an adapter that is purely declarative needs none of the rest. `Finding(item, coverage,
reason, owner, ticket)`; `owner` names the module or profile file that tracks it, `ticket` the issue an `untracked`
item waits on.

**Declarative entries.** No Python is needed to cover an app: a module's `module.toml` or the profile's
`coverage.toml` holds `[[coverage]]` tables:

```toml
[[coverage]]
match = ["file:~/.config/Code/Cache*", "file:~/.config/Code/logs"]   # globs against item ids
class = "excluded"                    # tracked | excluded | secret | untracked (with a ticket)
reason = "Electron cache and logs"    # required for every class but tracked
```
and `[[coverage.adapter]] adapter = "files" path = "~/.config/foo" format = "ini|json|toml|yaml|file|dir"` entries that
make the generic `files` adapter reconcile the path (link or copy, key-level merge for the structured formats). Rules of
the engine (`ubuntu_tweaks/coverage_defaults.toml`) < modules < profile < machine overlay; the first matching rule
of the highest layer wins. The profile layer is `coverage.toml` followed by every `coverage.d/*.toml` (sorted by
name, other files ignored), and the machine overlay has the same two (`machines/<host>/coverage.toml`,
`machines/<host>/coverage.d/*.toml`), all with the same `[[coverage]]` syntax: several authors add rules in
separate files without merge conflicts, and inside a layer the first matching rule in load order wins (`coverage.toml`
first, then `coverage.d` by name). `Finding.owner` names the file (`coverage.d/10-editors.toml`). Items the profile
already declares are `tracked` without any rule: a bundle key, a linked file, a `packages.toml` name, a module's
own files.

**Commands.** `ubuntu-tweak coverage [--json] [--class untracked|tracked|excluded|secret] [--dry-run]` scans and
prints the table with a count per class, exit 3 while an `untracked` item **without a ticket** remains (nothing else
changes). The counts line reads `Coverage: 5 untracked (2 ticketed), 3 secret, 120 excluded, 90 tracked`: the
`(M ticketed)` note appears only when some untracked items carry a ticket, and the `(run ...)` hint only while an open
one remains. `status` ends with the same summary line (its JSON `coverage` object also has `ticketed`). `capture`
proposes the untracked items grouped by purpose (it writes only with `--commit`, as always). `apply` / `init`
reconcile everything tracked. The profile repository's CI runs `ubuntu-tweak coverage --check-profile` (static, below)
and `ubuntu-tweak coverage`, and fails when an untracked item without a ticket appears.

**Ticketed untracked.** A finding somebody has already taken on stays `untracked` (it is still not configuration the
profile reconciles) but does not fail the gate. A `[[coverage]]` rule of class `untracked` takes a REQUIRED `ticket`
and a REQUIRED `reason`:

```toml
[[coverage]]
match = ["file:~/.config/Code", "dconf:/org/gnome/shell/extensions/dash-to-dock/*"]
class = "untracked"
ticket = "#42"                          # "#N" (this repository) or "owner/repo#N"
reason = "VS Code settings: decide what goes into files/ and what is sync state"
```

It is accepted in a module's `module.toml`, the profile's `coverage.toml` and the machine overlay's `coverage.toml`,
and follows the usual layering. The finding keeps `Finding.coverage = untracked` and carries `Finding.ticket`; the
table has a `TICKET` column (plain and interactive; present when any shown finding has a ticket), the finding's
`reason` comes first in the row with the detail after it in parentheses, `capture` and `coverage --propose` leave
ticketed items alone (a human is deciding them), and `--json` has `"ticket": "#42"` (empty for every other finding) and
`counts.ticketed`. A declaration still wins: an item the profile tracks is `tracked` whatever such a rule says. An
`untracked` rule without a `ticket` or a `reason`, a ticket that is not `#N` / `owner/repo#N`, or a `ticket` on
another class is invalid config (exit 2).

**`coverage --check-profile [--online] [--json]`** is the profile CI's static gate: it scans nothing, writes nothing
and needs no PyGObject. It reads the engine rules, every module's `[[coverage]]` (enabled or not), `coverage.toml`,
`coverage.d/*.toml` and the machine overlays', reports every problem found (not just the first) and exits 0 clean, 2 invalid. It checks: each
file and manifest parses; an `untracked` rule has a valid `ticket` and a `reason`; an `excluded` or `secret` rule has a
`reason`; only `untracked` rules have a `ticket`; every `match` glob can match some item id (its literal head must lead
into `dconf:/`, `file:~/`, `etc:/`, `apt:`, `snap:`, `flatpak:`, `brew:`, `pipx:`, `uv:`, `npm:`, `bun:`, `cargo:`,
`unit:`, `autostart:`, `extension:`, `cron:`, `ssh:` or `git:`; `~/.config/x` and `file:/home/me/x` are flagged); no
pattern is listed twice in one layer (the later one never applies) and no layer restates a lower layer's pattern with
the same class, reason and ticket as the nearest lower layer that lists it (redundant; another class, or the same class with its own reason or ticket, is a real override: a profile `secret` rule keeps the class and replaces the engine's generic reason with the real restore pointer). Only the rules
that apply on this machine can duplicate or override each other: a disabled module's rules and the overlays of other
machines (`machines/*/`) are still checked for syntax and patterns, so the CI runner's hostname does not matter. With
`--online` each distinct ticket is asked of `gh issue
view` (`#N` in the profile's checkout, `owner/repo#N` with `--repo`): a missing or closed ticket is a problem, a ticket
gh cannot answer for another reason is a problem too, and without `gh` the check is skipped with a note. `--online`
without `--check-profile` is invalid config (exit 2).

### Output

Request 14. Everything the CLI prints in an interactive terminal is **colored and explained**; the same
commands piped or with `--plain` print the compact, byte-stable text that scripts and the golden tests
read. All of it lives in one stdlib-only module, `ubuntu_tweaks/ui.py`: colors, badges, aligned text,
wrapping and the sentences that status, apply and init share or translate (the wording table `ui._TEXT`,
the per-kind reasons `ui._KIND_WHY`, the capture class help `ui.CAPTURE_CLASSES`). A command never
hard-codes an escape sequence; the one-off description of a single command's table (`list`, `machine`,
`bundles`, `doctor`, `update`) stays next to its renderer, in English, until ticket #58 moves it.

**Gating.** Two questions, asked of the stream being written (stdout, or stderr for errors):

| Question | Yes when |
|---|---|
| May ANSI escapes be written? (`ui.color_enabled`) | `--color always`; or `--color auto` (default) on a tty, unless `NO_COLOR` is non-empty ([no-color.org](https://no-color.org)) or `TERM=dumb`; `CLICOLOR_FORCE` (not `0`) turns it on for a pipe, `NO_COLOR` still wins. `--color never` and `--plain` always say no. |
| Explained layout? (`ui.rich`) | the stream is a tty (so `NO_COLOR` on a terminal still gets the explanation, uncolored) or `--color always`; never with `--plain`, and never because of `CLICOLOR_FORCE` alone (it only colors, so a script with it exported still reads the compact text). |

Global flags (before or after the command): `--color auto|always|never`, `--plain` (compact layout, no
escapes: the machine-readable text), `--lang en|ru` (or `UBUNTU_TWEAK_LANG`). A child `ubuntu-tweak`
(`update --commit` runs `apply` in a fresh interpreter) receives the same flags (`ui.cli_flags`).

**Colors have fixed meanings** and are never the only signal: green `✓ ok` = satisfied; yellow `! apply`
= needs action, `… manual` = only you can finish it; red `✗ error` = failure or blocking drift; cyan
`+ extra` / `i` / `→` = information, a command to run, an item that is present but not declared; dim =
explanation; bold = heading. Symbols fall back to ASCII (`v ! x ~ +`) on a stream that cannot encode
them. Widths come from `shutil.get_terminal_size`, clamped to 40..120 columns; every explanation is
wrapped with a hanging indent.

**`status`** (the model of the others) prints, in the explained layout: a header (machine, profile, engine
version = `git describe` of the checkout; when the profile is missing, the header also says what profile
discovery did, in yellow: `looked for .files, dotfiles, .dotfiles, dot-files via gh`, then `found <repo>` /
`none` / `needs manual: gh auth login`, see *Profile discovery*, and the same lines head the `apply` / `init`
preview); a one-line verdict with the next command (`✗ 4 missing, 1
modified, 5 extra — run: ubuntu-tweak apply (preview), then ubuntu-tweak apply --commit`, or `✓ in
sync`); then **Missing / Modified / Extra / Manual**, each with a one-sentence definition of what it
means and what fixes it (an empty section is one line unless `--explain`), items grouped by module as
`badge + what` plus a dim `why:` line (once per action kind in a group; the reason comment of a key is
already in the description); a legend; the next-step commands; with `--explain` / `-v` the exit-code
table. **Manual** holds the planners' warnings (things only the user decides, e.g. a missing git
identity); it never changes the exit code. The classification and the exit codes (*Drift*) are the same in
both layouts. `status --json` prints the same facts (`missing`, `modified`, `extra`, `manual`, `in_sync`,
`exit_code`, per item `kind`/`module`/`description`/`why`/`needs_root`); `extra` is `null` under `--only`
(not checked, not empty) and `why` is always English. `--explain` forces the explained layout on a pipe
(`--plain` still wins).

Elsewhere: `apply` preview = *What will change* with a summary line, per action a badge, `[sudo]` (or
`[sudo, skipped: --no-sudo]`), the description, a dim `why:` (`Action.why()`, default from the kind) and
the module, a footer saying plainly that nothing changed and which command applies it; `apply --commit`
prints `[3/21] → <what>` before and `[3/21] ✓ done` after each action, then a *Summary* (changed / need
you / skipped), the re-login hint, the skipped root actions and the *Manual steps* block with how to
finish each. `init` numbers its two steps (`Step 1/2: check required tools`, `Step 2/2: apply the plan`)
and ends with the same Manual steps block; `doctor` is a table of ✓/…/✗ rows with why each tool is
needed; `update`, `list`, `machine`, `bundles`, `capture` (summary block colored, plus a legend of the
skip classes), `show --format text`, `note`, `bundle-move` use the same colors and one-sentence
explanations; errors are a red `✗ error` line with a cyan `fix:` hint where the exit code has a standard
one. `--lang ru` translates the `status`, `apply` and `init` texts and every per-kind reason (the
two tables are checked for the same keys by `tests/test_ui.py`); everything else stays English.
Covered by `tests/test_ui.py` (gating, helper goldens plain and ANSI, wrapping at 60/80/120 columns,
the `status` layout against `tests/golden/status_explained.txt`, ANSI-stripped equals uncolored).

### Session log

Request 16. Every run of `ubuntu-tweak` (every command, `--help` and usage errors, a handled error, an unexpected
exception, Ctrl-C) writes everything the user saw to one log file, so an agent (or you) can read what happened
afterwards without asking for a paste. One module, `ubuntu_tweaks/sessionlog.py`, does it as a **tee at the CLI
entry**: `cli.main` opens the session before it parses anything, replaces `sys.stdout` / `sys.stderr` with tees and
closes the session after the exit code is known. No command logs anything itself. There is no opt-out: every run
is logged (the file is private, see below), `--dry-run` and `--plain` runs included; the log directory is the only
place a dry run may write.

- **Where.** `<state>/ubuntu-tweaks/logs/<UTC timestamp>-<command>.log`, `<state>` = `$XDG_STATE_HOME` else
  `~/.local/state` (the rule of `backup` and the dotfiles cache). The timestamp is `20261022T183012Z`;
  `<command>` is the subcommand that was run, `none` when parsing failed before one was known (a usage error,
  `--help`); the file is created before the arguments are parsed, as `...-none.log`, and renamed once the command
  is known. A name that exists gets `-2`, `-3`, ... before `.log`. `latest.log` in the same directory is a
  relative symlink to the newest log (replaced atomically, so a reader never sees it missing). The directory is
  mode `0700`, the files `0600`.
- **Content.** A `# session` header (`# argv:` as run, `# started:` UTC, `# host:`, `# cwd:`, `# engine:`,
  `# python:`), then the session text, then a footer: `# end: <UTC time>, <seconds> s, exit <code>`, one line for how
  the run did not end normally (`# interrupted (Ctrl-C)` with exit 130, `# terminated (SIGTERM)` with exit 143, best
  effort, `# exception: <type>` for an unexpected exception: its traceback is in the body, printed to stderr as
  always), and the facts the run learned (`# profile: <dir>`, `# machine: <name>`). `--help` and usage errors
  (`SystemExit`) are logged with their exit code and then exit as before. The file is flushed on every complete line, so
  it can be read while the run is still going; a header without a footer means the process is still running or was
  killed with SIGKILL.
- **The session text** is what the user saw, in the order it was written, as plain text: ANSI escape sequences
  (CSI/SGR and OSC) are removed, a carriage-return redraw (a progress line rewritten in place) is collapsed to its
  last state, and every **stderr** line is prefixed `! ` (stdout lines carry no prefix), so one stream of text
  tells which stream said what. Terminal escapes and colors are never in the log, whatever `--color` says.
- **Child output.** `Process.execute` and `process.run(capture=False)` (apt, git, the module installers, the child
  `apply --commit` of `update`) used to let the child write to the terminal directly, which no tee can see. They now
  give the child pipes for stdout and stderr and copy the bytes as they arrive (raw `os.read` chunks, not
  line-buffered, so a prompt without a newline appears at once) to `sys.stdout` / `sys.stderr`, so the user sees the
  same text and the tee logs it. stdin stays the terminal: a child may still read an answer. A tool that
  checks `isatty` prints its plain, progress-less output because its stdout is a pipe now (a child `ubuntu-tweak`
  is told with `--color always` when the parent shows the explained, colored layout, so `update` keeps it;
  `ui.cli_flags`); a Python child gets `PYTHONUNBUFFERED=1` so its prompts are not held back. `Process.query`
  and `run(capture=True)` return their output to the caller and show nothing, so they log nothing. After the child
  has exited, the copy waits at most two seconds for output still in the pipes: a daemon the child left behind
  can hold the pipe open and must not hang the run (such a daemon gets `SIGPIPE` on its next write once this
  process has exited; daemons detach their output). SIGTERM sent to `ubuntu-tweak` ends it with exit 143 without
  killing a running child (which may die of that `SIGPIPE` on its next write); Ctrl-C reaches the child through the
  terminal too, and a child that has not exited a quarter of a second later is killed. **Limit:** a child that is
  not Python and does not flush its stdio buffers shows a prompt that has no newline only when the buffer fills,
  because its stdout is a pipe; such a prompt is neither shown nor logged until then. debconf's dialog frontends,
  which want a terminal, fall back to a plainer one. The two streams of a child are read by two threads: their
  relative order is not guaranteed, the order within each stream is.
- **Prompts and answers.** `sys.stdin` is wrapped too: what this process reads from the terminal (`input()`,
  `readline`, iteration, `read`) is logged as `> <typed text>`, the prompt that was the unfinished stdout line in
  front of it (`> Install foo? [y/N] y`). To make `input()` go through that path the wrapper has no file
  descriptor (`fileno()` raises `io.UnsupportedOperation`; `isatty()` still answers for the real terminal), which
  costs `input()` its readline line editing. **Not captured:** the password prompt of `sudo` / `pkexec` (they open
  `/dev/tty` themselves; the password never passes through this process, so it cannot be logged), and anything you
  type into a *child's own* prompt (an installer asking `[Y/n]`): that input goes from the terminal straight to the
  child; only the child's printed question appears in the log.
- **Masking.** Secrets never reach the log. Every line written to the log (never the terminal) goes through
  `capture.secrets.mask_secrets`, which reuses the *capture* value patterns for PEM blocks (kept per stream), API
  tokens, JWTs, URL credentials (only the `user:password@` part is replaced, scheme and host stay) and long base64
  runs and replaces each match by `***`. E-mail addresses, LAN IP addresses and 40-hex git object ids are **not**
  masked: they are not credentials, and the log exists to be analyzed (a commit id or a host in an error message
  is the evidence). In the `argv` header the value after a flag whose name contains `token`, `password` or
  `secret` (`--token X` and `--token=X`; the value may start with `-`) is `***` too, and so is what is typed at a
  prompt that, or the line before it, or an unfinished stderr line (where `getpass` writes), contains one of those
  words (a secret need not look like one). A line is held back until its newline arrives; one of more than 256 KiB
  is logged in pieces, cut after a space where there is one so a token is not split. A PEM block whose `END` never comes is masked
  for at most 200 lines.
- **Never breaks the command.** When the log cannot be created (no writable state directory, no `$HOME`) or a
  write fails (disk full, permissions), logging is switched off for the rest of the run and one warning line goes
  to stderr once; the command carries on and keeps its exit code.
- **Retention.** The newest 50 logs are kept (`sessionlog.KEEP_LOGS`): after the new log exists, older `*.log`
  files in the directory are deleted (never `latest.log`, never the file it points at, never any other file).
- **Nested runs.** `update --commit` runs `apply --commit` in a child `ubuntu-tweak`. The outer run exports
  `UBUNTU_TWEAK_SESSION_LOG=<its log>` for its lifetime; a process that finds it set and its directory writable
  writes no log of its own (its output already flows through the outer run's pipes into the outer log).
- **Test seam.** `UBUNTU_TWEAK_LOG_DIR` names the log directory itself (replacing the path above); `tests/__init__.py`
  points it at a temporary directory so no test writes into the real state directory. It is not an opt-out for
  users: unset it and every run is logged.

Covered by `tests/test_sessionlog.py` (escape stripping, `\r` collapsing, stream marks, masking, header and
footer, modes, symlink, retention, the failure / Ctrl-C / usage-error / unexpected-exception logs, the nested-run
rule, prompts and typed answers, child output through the pipes with unchanged errors, a failing log write) and
`tests/test_process.py`.

### Service modules

Request 9 adds modules that install and run services. They are `kind = "system"` modules whose
installer and checker are **one Python script** in the module directory
(`ubuntu_tweaks/module_script.py` is the shared scaffolding):

```toml
[source]
kind = "system"
install = "ssh-server install"
check = "ssh-server check"
```

| Script command | Contract |
|---|---|
| `check` | Read-only, needs no root. Exit 0 = every fact holds, 3 = drift (the unmet facts are printed, one line each), anything else = the checker broke (the checker protocol in *Source kinds*). |
| `status` | The same facts, all of them, one line each: `ok`, `missing` or `manual` (unmet and not fixable by `install`, e.g. a signing key that does not exist). Exit codes as `check`. Used by hand and by tests; the engine does not call it. |
| `plan` | Optional, read-only: what `install` is going to do, one line each (`firefox-passkey-qr` prints its installer's `--dry-run` listing). Named in the manifest as `preview`; the engine prints it indented under the action line of the `apply` preview. |
| `install` | Makes the facts true. Exit 0 = done, 1 = failed (stderr says why), 10 = blocked on a human step: it prints `needs manual: <what>` and changed nothing it could not finish. The engine runs `check` again afterwards (*Plan and actions*), so an exit 0 that did not converge is an error, never a success. |

Options come from `[modules.<name>]` in `profile.toml` / `machine.toml` (merged as everywhere):
the engine passes the table minus `enabled` as JSON in `UBUNTU_TWEAK_MODULE_CONFIG` to both
commands (unset = no options); a script rejects an unknown option or a wrong type with exit 1.
A root script finds the desktop user through `SUDO_USER` / `PKEXEC_UID`, never `$HOME`.

A module that must act as the desktop user (it writes `$HOME`, talks to the user's systemd or
Secret Service) sets `root = false` in `[source]`: its action is not `[sudo]` and `install` runs
unprivileged. What such a module needs from root is a **separate root module** (`passwords-app-polkit`,
`firefox-policy`), so the preview marks exactly which steps ask for a password.

**What the scripts expect from the engine** (all four are manifest/planner features of `kind = "system"`,
implemented with the system-module planner, not by the scripts): `[source] root = false`; the
`UBUNTU_TWEAK_MODULE_CONFIG` environment variable, passed through `sudo` as `sudo -- env VAR=… <cmd>`
(sudo's `env_reset` drops a plain variable); `[source] preview = "<script> plan"`; and exit 10 of `install`
treated as a skipped action (its last line `needs manual: <what>` is printed, the post-run convergence check
does not run, `status` still lists the module as Missing). Until the engine has them, a script still works
when run by hand (`<script> status|plan|install`); an option the engine cannot deliver is simply unset, and
exit 10 is reported as a failed action rather than a skipped one. Credentials never go through this
channel: a script reads them from the desktop user's Secret Service at run time
(`secret-tool lookup application ubuntu-tweaks purpose <name>`; names in the module's section), keeps them
in memory and hands them to the one child process that needs them through its environment, never an
argument, a file of this repository or the output. A missing credential is `needs manual`, never success.

Tests never touch the machine: the scripts take an injectable `Process` (the one runner,
`ubuntu_tweaks/process.py`) and a filesystem root, and run against a stateful fake machine
(`modules/*/tests`).

#### SSH server (`ssh-server`, root)

Facts, in the order `status` prints them:

1. the `openssh-server` package is installed (`dpkg-query`);
2. only when the profile sets `password_auth` (below): `/etc/ssh/sshd_config.d/00-ubuntu-tweaks.conf`
   has the expected text, `sshd_config` includes `sshd_config.d/*.conf`, and sshd's **effective**
   value (the first one it reads, `Include` processed inline) is the wanted one: a
   `PasswordAuthentication` above the `Include` line would win over the drop-in, which is a `manual` fact;
3. only when sshd listens on tailscale addresses (a `ListenAddress` inside `100.64.0.0/10` or
   `fd7a:115c:a1e0::/48`) and a `tailscaled.service` unit exists:
   `/etc/systemd/system/ssh.service.d/10-ubuntu-tweaks-tailscale.conf` has the expected text;
4. `ssh.socket` is neither enabled nor active: with socket activation (the Ubuntu 24.04+ default)
   systemd, not sshd, holds the listening socket and ignores `sshd_config`'s `Port`/`ListenAddress`, so
   the module runs sshd as a plain service (`systemctl disable --now ssh.socket`, the procedure Ubuntu
   documents for a non-default port);
5. `ssh.service` is enabled;
6. ... and active;
7. systemd has loaded the current unit files (`systemctl show -p NeedDaemonReload` is `no`): a
   `daemon-reload` that failed after a drop-in was written is drift again, not forgotten;
8. sshd accepts connections on every `(address, port)` its configuration names: the `Port` lines
   (default 22) and the `ListenAddress` lines, read from `/etc/ssh/sshd_config` and its `Include`
   chain (a `Match` block ends a file's global part), compared with `ss -H -ltn`; a wildcard
   socket covers a named address of its own family (`*` covers both, `0.0.0.0` only IPv4, `::` only IPv6: sshd sets `IPV6_V6ONLY`), a host
   *name* is matched by port only (`ss -n` shows numbers).

The checker reads the same files sshd does. A file that exists but the desktop user cannot read (a
`0600` `50-cloud-init.conf`) makes `check` fail with exit 1 and the file's name rather than guess a
different configuration than the root install sees; make it readable (mode 644).

**The module does not change `Port`, `AllowUsers`, `ListenAddress` or any other sshd setting that is
already there**: a machine's own `sshd_config.d` is its business (this machine's
`99-moshi-tailscale.conf` keeps working as it is). The stock Ubuntu `sshd_config` is already a sane
default (port 22, every address, `Include` of the drop-in directory), so with no configuration the
module writes none; it installs the package and enables and starts the service.

*The boot failure it fixes.* With `ListenAddress 100.74.26.95` and Port 2223 in a drop-in, sshd
starts before `tailscaled` has created the address, exits 255 (`Cannot assign requested address`) and
`ssh.service` stays failed, because the unit says `RestartPreventExitStatus=255`. The fix is a
systemd drop-in, not a change to sshd:

```ini
# /etc/systemd/system/ssh.service.d/10-ubuntu-tweaks-tailscale.conf
[Unit]
After=tailscaled.service
Wants=tailscaled.service
StartLimitIntervalSec=120
StartLimitBurst=10

[Service]
Restart=on-failure
RestartSec=5
RestartPreventExitStatus=
```

`After=`/`Wants=` fix the order; the retry covers the short gap between `tailscaled` being up and the
address being assigned (at most 10 starts in 2 minutes, so a real configuration error does not
loop). *Decision*: not `net.ipv4.ip_nonlocal_bind` / `net.ipv6.ip_nonlocal_bind`: they would be
system-wide for every process, while the problem is one unit's ordering. The drop-in has its own
file name (`10-ubuntu-tweaks-tailscale.conf`, not `override.conf`) so the user's own overrides are
never replaced.

*Password policy, only when asked.* `[modules.ssh-server] password_auth = false` writes
`/etc/ssh/sshd_config.d/00-ubuntu-tweaks.conf` with `PasswordAuthentication no` and
`KbdInteractiveAuthentication no` (both: PAM keyboard-interactive would otherwise still take
a password); `true` writes `PasswordAuthentication yes`. `00-` is read first and sshd keeps the first
value it sees, so the profile wins over other drop-ins. Never locking the user out: `false` is refused
(exit 10, `needs manual`) while the desktop user has no usable public key in `~/.ssh/authorized_keys`
(a line with a key type and a base64 body, not a `command=` / `restrict` / `from=` key; `AuthorizedKeysFile` and `AllowUsers` are not consulted; the guard applies only when the file would be written, so repairs of an already converged policy still run), while
the installer cannot tell which user to protect (a root shell without `sudo`), while `sshd_config` does not
`Include` the drop-in directory (also when the file is already in place), or while a setting above the
`Include` line overrides the drop-in (the file just written is removed again). Without the option an
existing file is left as it is (disabling a module never uninstalls).

*Install order and validation.* As root: `apt-get install -y openssh-server` when missing; write the
password-policy drop-in (rolled back when `sshd -t` rejects it); `sshd -t` **before** anything is
restarted or the unit drop-in is written, so a rejected run leaves no file behind and the next run still
reloads systemd; the unit drop-in, then `systemctl daemon-reload` when systemd says it is needed;
`disable --now ssh.socket` when socket activation is on; `enable ssh.service`; `reset-failed` + `start`
when it is not active; otherwise `restart` when the socket was just switched off (the running sshd
was started by the socket and holds systemd's listener, so it must re-exec to bind `sshd_config`'s
addresses) or `reload` when only sshd's own config changed. A `reload` that fails removes the policy file
it just wrote, so `check` never reports a policy that sshd did not take. The unit drop-in needs no
restart of a healthy sshd; open sessions survive either way (`KillMode=process`).
`ChallengeResponseAuthentication` is read as its alias `KbdInteractiveAuthentication` when looking for a
shadowing line.

*memguard's polkit rule.* `docs/specs/memguard.md` makes the polkit rule `50-memguard-swap-drain.rules`
refuse `systemctl start ssh.service` for the **desktop user** (it grants only `swap-drain.service`; the
rest falls through to the default policy). That is unchanged and not in the way: the module's mutating
commands run as **root** through `sudo` (or `pkexec`), and polkit's `manage-units` check does not
apply to uid 0; `check`/`status` use only read-only `systemctl is-enabled` / `is-active`, which need no
authorization. The module never asks polkit for anything, and memguard's own guard needs no
exception. (`sshd` is on memguard's protected list, so it is never frozen or killed either.)

Real-root verification (what fake-root tests cannot prove; run once on a real machine, as root where
marked):

- In a terminal (the root step asks for a password): `ubuntu-tweak apply --only ssh-server` (preview
  lists the drop-in and the service start), then `--commit`.
- `systemctl status ssh.service` is `active (running)`; `systemctl is-enabled ssh.service` is `enabled`.
- `ss -ltn` shows sshd on every address and port in `sshd_config`.
- `ubuntu-tweak status --only ssh-server` exits 0; a second `apply --commit` reports `0 changed`.
- Reboot: `ssh.service` is `active` after boot (`journalctl -b -u ssh.service` has no `Cannot assign
  requested address`), and `ssh -p 2223 <tailscale-ip>` works from another tailnet host.
- With `password_auth = false` and a key in `~/.ssh/authorized_keys`: a password login is refused, the
  key login works; with no key the run stops with `needs manual` and the file does not exist.
- `systemctl is-enabled ssh.socket` is `disabled` and `systemctl is-active ssh.socket` is `inactive`
  (sshd runs as a service, so `Port`/`ListenAddress` apply); an unreadable drop-in makes `status` fail with
  its name.
- As the desktop user, `systemctl start --no-ask-password ssh.service` is still refused
  (memguard's rule is intact).

#### Disks (`disks`, root)

Request 23. A disk belongs to a machine, so it is declared in `machines/<host>/machine.toml` (the shared `profile.toml` works
too); one table per disk, named by you:

```toml
[modules.disks.mounts.mac-backups]
uuid = "fcbf521d-af6b-463d-9959-d25f686b3379"   # exactly one of uuid | label | partuuid (blkid / lsblk -f)
fstype = "apfs"                                  # ext2/3/4 xfs btrfs f2fs exfat vfat ntfs hfsplus udf iso9660 apfs
description = "MacBook backups (USB, APFS)"      # the comment above the fstab line
# optional, with their defaults:
mountpoint = "/mnt/mac-backups"   # default /mnt/<name>; allowed: below /mnt, /media/<you>, or "~/x" in the desktop user's home. Refused: those directories themselves, other homes, /var /root /etc /boot /usr /opt /srv /tmp /proc /sys /dev /run /snap, /
read_only = true                  # true for ntfs, apfs, hfsplus, udf, iso9660 (a foreign OS owns the disk); false for the rest
driver = "auto"                   # apfs: auto | fuse | kernel; ntfs: ntfs3 | ntfs-3g
owner = "alex"                    # uid/gid for filesystems without POSIX owners (ntfs exfat vfat hfsplus udf iso9660 apfs); default the desktop user
automount = true                  # x-systemd.automount: mounted on first access; false mounts at boot (still nofail)
device_timeout = "10s"            # x-systemd.device-timeout: how long a missing disk delays anything
idle_timeout = "5min"             # x-systemd.idle-timeout (unmount when idle); default unset
volume = 0                        # apfs container volume number
options = ["noatime"]             # extra fstab options (not ro/rw/uid/gid/nofail/x-systemd.*); "noexec" is yours to add; "suid" / "dev" opt out of the hardening below
```

Every string value (description, label, owner, ...) is free of control, format and line-separator characters (newline, tab,
U+0085, U+2028, bidi controls, ...): the values end up in a line-oriented file and a description such as
`x\n/dev/sda1 / ext4 ...` must never become a second fstab line (`ConfigError`, exit 2, naming the key).

Nothing declared, nothing planned: the module is on by default and belongs to no init step. The declaration is parsed by
`ubuntu_tweaks/disks.py` (pure; every error names its key and exits 2), the module is `modules/disks/disks` (the *Service
modules* contract: `check`, `status`, `plan`, `install`; options through `UBUNTU_TWEAK_MODULE_CONFIG`).

Facts, in the order `status` prints them (per disk unless noted):

1. the driver: `apfs-fuse` built and installed (`/usr/local/bin/apfs-fuse`: a regular file owned by root, not writable by group or
   others, stamped with the pinned commit and `root-build`, so a binary an older version built as the desktop user is rebuilt by root) and `fuse3`;
   or `apfs-dkms` (the `apfs` kernel module loadable); or `ntfs-3g`. Other filesystems need nothing;
2. the device `/dev/disk/by-uuid/<uuid>` (by-label, by-partuuid): informational, **never drift**: an unplugged disk is not a
   failure (the entry is `nofail`, so the machine boots without it);
3. the managed block of `/etc/fstab`: `# BEGIN ubuntu-tweaks disks` ... `# END ubuntu-tweaks disks`, **text-equal to the generated
   block, comments included** (a changed `description`, `options` or driver is drift and is rewritten; a hand-edited comment
   inside the block too), e.g. `/dev/disk/by-uuid/fcbf521d-... /mnt/mac-backups fuse.apfs-fuse
   ro,allow_other,uid=1000,gid=1000,nosuid,nodev,nofail,x-systemd.automount,x-systemd.device-timeout=10s 0 0`; the block lists
   no disk the profile does not declare (a removed declaration removes its line, and the block when it was the last); no other
   line of the file uses the same device or mount point (a `manual` fact naming the line numbers);
   **hardening**: every entry carries `nosuid,nodev` (a disk another machine wrote can hold setuid files and device nodes that
   a root-mounted tree would honour); `noexec` is not added (working checkouts and `node_modules/.bin` need `exec`), declare
   `options = ["noexec"]` for a disk that only stores data, `options = ["suid"]` / `["dev"]` to opt out on purpose;
4. the mount point exists, is a real directory and no component of it below `/mnt`, `/media/<user>` or the home is a symbolic
   link (a root mount through a link lands wherever it points; a link there is a `manual` fact and a `needs manual` line of
   `plan`); `/mnt/...` stays root's, directories created inside the user's home are the user's;
5. systemd knows the generated mount unit (`LoadState=loaded`) and its automount unit is active (`mount` unit when
   `automount = false` and the disk is attached);
6. when something is mounted there: it is read-only exactly when declared (a `manual` fact: the module never remounts or
   unmounts a busy filesystem).

`install`, as root, in this order: refuse (`needs manual`, exit 10, **nothing installed, created, stopped or written**) when a
mount point of *any* declared disk has a symbolic link on its path, when the home directory a mount point lives in does not
exist (root never creates it), or when a disk the profile no longer asks for (declaration removed, mount point moved,
`automount` switched off) is **still mounted** (stopping its automount unit detaches the filesystem; this module never
unmounts: `umount` it, then run again); `plan` prints the same three as `needs manual:` lines, never as the `mkdir` or
`systemctl stop` they would otherwise be; build or install the driver; create the mount points one directory at a time
through descriptors (`mkdir` and `open` relative to the parent with `O_NOFOLLOW`, `fchown` of the directory just opened
for what lands in the user's home), so a link swapped in after the check is refused, never followed; **a mount point that
already exists is only `lstat`ed, never opened** (opening the directory of an active automount unit would mount the disk,
and fail with it unplugged); verify the candidate `/etc/fstab` with `findmnt --verify` (refused, nothing written, when it
has **more** errors than the current file: errors the file already had are not the module's to block on); stop the
automount unit of every line the block in the file has and the new one does not, because systemd keeps a running unit
after its fstab line is gone (done **before** the file changes, so a failed stop leaves the old block and the next run
stops it; asked once more right before the stop, because a driver build can take minutes and a disk mounted meanwhile is
refused, not detached); write the file atomically after saving the original once as `/etc/fstab.pre-ubuntu-tweaks`;
`systemctl daemon-reload`; `systemctl start <automount>` and, when the disk is attached, `systemctl start <mount>`, so the
mount works at once and the next `check` is green. It never formats, repairs, unmounts, remounts or writes to a disk, and
never touches a line of `/etc/fstab` outside its block.
A mount point inside another declared mount point is refused when the declarations are parsed (creating it would open the other disk's automount point). `plan` stops at the `needs manual:` lines, as `install` does. The first refusal covers the state before anything runs; the busy re-check right before the stop can still follow a driver build and mount-point creation, and says only that the fstab was not changed. A link planted in the user's home *after* `install` is reported by `check`, not prevented: the home is the desktop user's own tree, so use `/mnt` where that matters.
`/media/<user>/...` is allowed next to `/mnt/...` and the home, but udisks2 manages that directory for removable media too:
prefer `/mnt` for a disk that is meant to stay.

**Drivers and the APFS decision.** Nothing in Ubuntu 26.04 mounts APFS out of the box:

| | `apfs-fuse` (default) | `apfs-dkms` (`driver = "kernel"`) |
|---|---|---|
| what | userspace, `sgan81/apfs-fuse`, last commit 2023-03-12, **read-only**, no packages: the module builds the pinned commit with `cmake` **as root in a fresh `0700` directory under `/var/tmp`** (the source is content-pinned and the checkout is verified with `git rev-parse HEAD` before anything is built; nothing a user can write is ever copied into `/usr/local/bin`, where systemd runs it as root) and installs `apfs-fuse` and `apfsutil` there (each build result is read once through a no-follow descriptor and written by root); GCC 15 needs `-include cstdint -include vector -include cstring` and cmake 4 needs `CMAKE_POLICY_VERSION_MINIMUM=3.5` | Ubuntu's `apfs-dkms` 0.3.18 (universe; `linux-apfs-rw`), kernel module, **experimental write support** |
| builds on this machine | yes: container `ubuntu:26.04`, GCC 15.2, mounts and reads the real disk | yes: `make -C /lib/modules/7.0.0-38-generic/build M=...` produced `apfs.ko`, vermagic `7.0.0-38-generic` (also upstream v0.3.22) |
| Secure Boot | no kernel module, nothing to sign | SecureBoot is **enabled** here and only Canonical's CA is enrolled: an unsigned DKMS module is refused by the kernel. The module counts the kernel driver as working only when the kernel can load it: `apfs` is listed in `/proc/filesystems` (`loaded`), or the module is installed and, under lockdown (`/sys/kernel/security/lockdown` `[integrity]` / `[confidentiality]` or `sig_enforce`), signed by a Canonical key or by a key `mokutil --list-enrolled` lists. `modinfo -F sig_key` is compared with the **Subject Key Identifier** and the **Serial Number** fields of the listing only (hex without colons, case or leading zeros; a short serial is read from its `(0x..)` form), never with other hex such as an Authority Key Identifier or a modulus. **Unsigned, signed by a key that is not enrolled, or a listing that cannot be read are `blocked`**: `auto` stays on FUSE, an explicit `driver = "kernel"` is a `manual` fact (and a `needs manual` plan line) with the remedy. `install` always tries `modprobe apfs` when it is not loaded (the proof), and a refusal is `needs manual` with the MOK enrollment (`mokutil --import`, reboot) on the first and on every later apply, before anything is written |
| failure mode | a crash ends one process | a bug is a kernel oops; the write support "may corrupt your data" (upstream README) |

`driver = "auto"` (the default) takes the kernel module only when it is loaded or usable by the rule above, or when the
declaration says `read_only = false`; otherwise FUSE, so `auto` never switches a working FUSE entry to a module Secure Boot
would reject. **Read-only is the default for APFS** because the disks are
Apple's volumes: nothing here needs to change them, and a write bug would destroy the only copy of a backup. Read-write needs
`read_only = false`, which selects the kernel module (`mount ... rw,readwrite`); declaring it with `driver = "fuse"` is
invalid config (`apfs-fuse cannot write`). A FileVault-encrypted volume is detected with `apfsutil` (`FileVault: Yes`) and
ends as `needs manual` (exit 10) after the entry is written: the module never stores or asks for a password.

`apfs-fuse` shows the volume root as the directory `root` (and a `private-dir`): the `backups` volume of `ubuntu-pc` is
`/mnt/mac-backups/root/...`; the kernel module shows the volume root itself.

**Capture.** The `disk` adapter of the coverage scanner (`ubuntu_tweaks/adapters/disks.py`) lists the block devices that
carry a filesystem (`lsblk`), the mounts and the `/etc/fstab` lines; the system's own (`/`, `/boot`, `/boot/efi`, swap, snap
loops) are excluded with that reason, the ones declared in `[modules.disks.mounts.*]` are tracked, the rest are untracked and
`capture` proposes a declaration for the machine overlay: the filesystem and driver defaults of the table above, read-only for
every foreign filesystem, the label as `description`. A Windows partition (`ntfs`, `Windows 11`) is proposed read-only and
never mounted by force.

**Real-root verification.** `modules/disks/tests` (a stateful fake machine and a scratch `/etc`; one test runs the real
`findmnt --verify` over the generated lines and asserts no parse error and no error about an unplugged disk) and
`tests/test_disks_lib.py` exercise everything above; both suites pass as a normal user and as root (fixed non-root uid in the
fixtures). A real build as root, `findmnt --verify` and a `mount` through the generated fstab line were run as root in a
container with the disk attached read-only (`--device /dev/sda2:/dev/sda2:r`): `ro,nosuid,nodev,...,allow_other` fuse mount, a
write gives `Read-only file system`, a second install changes nothing, a changed description is drift then applied, an empty
declaration stops the automount and restores the original fstab. The real `systemd-fstab-generator` turned the block into
`mnt-mac\x2dbackups.mount` and `.automount` (run on the workstation). A container cannot show what needs the machine's own
systemd and udev; these are checked after the user's `apply --commit`: the first access to `/mnt/mac-backups` mounting it,
the mount coming back after a reboot and after re-plugging the disk (`systemctl status mnt-mac\x2dbackups.automount`,
`ls /mnt/mac-backups/root`, `findmnt /mnt/mac-backups`).

#### Passwords (`passwords-app`, `passwords-app-polkit`)

Request 9: *passwords-app is installed and configured first; every automatic login takes its passwords from
it.* The app is `alex-mextner/passwords-app` (iCloud Keychain sync into the GNOME Keyring, session-bus API
`io.github.alex_mextner.Passwords`, native-messaging host for browsers). The module and its identifiers are
the tool's own (*Author-published tool modules*).

`passwords-app` (user level) facts: `git` and `uv` on `PATH` (its installer needs both); the checkout
(`checkout` option, default `~/dev/passwords-app`, cloned from `repo` when missing: the manifest's
`[source] repo`, `https://github.com/alex-mextner/passwords-app.git`, unless the profile sets
`[modules.passwords-app] repo`; an existing checkout is **never pulled or reset**, it is the owner's working
copy); installed from the checkout's current commit (the installed commit
is recorded in `~/.local/state/ubuntu-tweaks/passwords-app.rev`, so a moved `HEAD` re-runs the installer);
the user unit file; the Firefox native-messaging manifest names an executable host and
`passwords-app@alex-mextner.github.io`; `passwords-appd.service` enabled and active; the bus name is owned
and `busctl --user introspect` answers (the owner check, `busctl list --acquired`, comes first because
`introspect` on an absent name would start the daemon: `check` never does); and iCloud **signed in**
(the `state` field of `GetStatus`, nothing else of the reply is read, the Apple ID is never shown).
Install: clone when missing, run the repository's own `scripts/install.sh` (which also enables and
restarts the unit; the module enables and starts it again when that did not happen), record the commit,
verify the bus. Signing in needs the owner's Apple ID, password and 2FA code, so it is the one `manual`
fact: the install ends with `needs manual: ... run passwords-app signin`.

`passwords-app-polkit` (root) copies the checkout's `data/polkit/*.policy` to
`/usr/share/polkit-1/actions/` when it differs (mode 644); this is the `pkexec install ...` line the app's
installer prints, run through the module's `sudo` so the preview shows it. No checkout is a `needs manual`
(`enable the passwords-app module first`). Orders: `passwords-app` (manifest `order = 60`) → `passwords-app-polkit` (61) → `firefox` (70).

Recorded on this machine (2026-10-22): the unit is enabled and active and the bus name is owned, so the
module reports the checkout-commit stamp missing and the polkit action not installed.

Real-root verification: `sudo ubuntu-tweak apply --only passwords-app-polkit --commit`, then
`pkcheck --action-id io.github.alex_mextner.Passwords.unlock --process $$ -u` asks for the login password;
as the user, `ubuntu-tweak apply --only passwords-app --commit` converges and `status --only passwords-app`
is 0 after `passwords-app signin`.

#### Firefox (`firefox`, `firefox-policy`, `firefox-autologin`, `firefox-passkey-qr`)

Request 9: *passwords are filled in Firefox, and Firefox signs in by itself at init.*

**Facts about this machine** (verified 2026-10-22): Firefox is the **snap** (158.0, `mozilla` publisher). The
snap declares the plug `etc-firefox` (`system-files`, `read: /etc/firefox`) and it is connected, so Firefox
reads `/etc/firefox/policies/policies.json` (Mozilla's documented Linux location; the Mozilla deb reads it
without a plug). The extension id is `passwords-app@alex-mextner.github.io`. Release Firefox **refuses
unsigned add-ons**, `passwords-app` ships no signed build and its snap-era native host reaches the daemon
through the xdg-desktop-portal (the first start shows *Allow Firefox to start WebExtension backend?*, which
only the owner can answer).

*Signing* (`firefox`, user level). Facts: the extension sources (`extension/package.json` version) and a
**Mozilla-signed** `passwords-app.xpi` of the checkout's version (a `META-INF/*.rsa|*.sig` entry) staged in
`~/.local/state/ubuntu-tweaks/firefox/` as one canonical file plus `passwords-app.version` (the extension
version it was built from, so a checkout that moves back is rebuilt, never mixed up). Install: take the AMO API credentials from the Secret Service
(`purpose amo-jwt-issuer` and `amo-jwt-secret`; created once by the owner with `secret-tool store`, the
message names the exact commands); `bun install --frozen-lockfile`; `bun run build`;
`web-ext sign --channel=unlisted --source-dir extension/dist/firefox`, with the credentials only in
`WEB_EXT_API_KEY` / `WEB_EXT_API_SECRET` for that one process; stage the signed file. Without the key it
stops with **`needs manual: AMO signing key ...`**, builds nothing and the fact stays Missing: the module
degrades to a clearly reported gap, never a fake success. AMO needs a new `version` per upload, so a
changed extension without a version bump fails with AMO's own message (exit 1).

*Force-install* (`firefox-policy`, root). `/etc/firefox/extensions/<id>.xpi` (a copy of the staged file,
mode 644: a sandboxed Firefox cannot read the user's `~/.local`; copied **before** the policy names it) and in
`/etc/firefox/policies/policies.json`:

```json
{
  "policies": {
    "ExtensionSettings": {
      "passwords-app@alex-mextner.github.io": {
        "installation_mode": "force_installed",
        "install_url": "file:///etc/firefox/extensions/passwords-app@alex-mextner.github.io.xpi"
      }
    }
  }
}
```

A root script must not trust the desktop user's tree: the staged file is read only as a regular file owned by
that user (no final symlink, at most 32 MiB) and only when it is a zip with a Mozilla signature entry; the
polkit module likewise accepts only `*.policy` files that declare actions in
`io.github.alex_mextner.Passwords.*`, named `io.github.alex_mextner.Passwords*.policy` and without any `org.freedesktop.policykit.exec` rule (a policy can grant `pkexec` rights), see `read_owned_file`.

The file is **merged**: other policies and other `ExtensionSettings` entries stay; a file that is not a JSON
object is refused, never overwritten. `ExtensionSettings` is used instead of the older
`Extensions.Install` because it also keeps the add-on installed and updates it when the file changes. With
the snap the module also connects `firefox:etc-firefox` when it is not connected. The option
`[modules.firefox-policy] credentialsd_helper = true` adds the Mozilla-signed `credentialsd-helper` add-on of
`firefox-passkey-qr` (pinned release URL) so that installer's approval prompt does not appear.

*What "Firefox authorizes automatically" means.* Two readings, both named:

1. **Autofill on any login page**: the extension asks passwords-app for the credentials of the page's site
   and fills them. This is what the modules deliver once the extension is active.
2. **A Firefox Account / Sync login with no human**: not possible. It needs the account password and a 2FA
   code, and storing them anywhere (a file, the profile, this repository) is exactly what this project
   never does; the only copy is in passwords-app/the keyring, filled by the extension on a page the user
   (or the module) opened.

`firefox-autologin` (user) implements (1) for init: facts `firefox` on `PATH`; the extension **active** in a
profile (`extensions.json` of the snap profile `~/snap/firefox/common/.mozilla/firefox/*/` or of the deb
`~/.mozilla/firefox/*/`); and the marker `~/.local/state/ubuntu-tweaks/firefox-signin-opened` (written once).
Install: needs a graphical session (`WAYLAND_DISPLAY` / `DISPLAY`), starts Firefox when the extension is not
active yet, waits up to `wait_seconds` (default 90) for the policy to activate it, then opens
`https://accounts.firefox.com/signin` (option `url`, https only) and writes the marker. If the extension does
not become active in time it ends with `needs manual: ...` (apply `firefox-policy`, restart Firefox, answer the
WebExtension-backend prompt, look at `about:policies`) and writes **no** marker. `status` always prints the
line that says autofill, not stored credentials. Whether a credential for that site exists in passwords-app is
not checked: the module never reads one.

Orders (the init pipeline's `order` key, steps 5-7): `ssh-server` 50, `passwords-app` 60, `passwords-app-polkit` 61, `firefox` 70, `firefox-policy` 71, `firefox-autologin` 72, `firefox-passkey-qr` 73. An install only re-runs a third-party installer when something is missing (`passwords-app` skips `install.sh` while its commit stamp matches, so a pending iCloud sign-in does not restart the daemon on every apply). State of all service modules lives under `~/.local/state/ubuntu-tweaks/` whatever `XDG_STATE_HOME` says: a root script never sees the user's variables. The passkey installer's `check` is trusted only for exit 0 or 3 (anything else is a broken checker, exit 1), and the `status` fallback must recognise every required row.

*Phone-QR passkeys* (`firefox-passkey-qr`, user level, **off by default**: it replaces the Firefox snap by
Mozilla's deb, so a machine opts in with `[modules.firefox-passkey-qr] enabled = true` in its
`machine.toml`). It runs the installer of <https://github.com/alex-mextner/firefox-passkey-qr> (checkout
`~/dev/firefox-passkey-qr`, cloned when missing from `repo`: the manifest's `[source] repo` unless the profile
sets one; options `checkout`, `repo`, `ha_url`, `remove_snap`). Read
from its source: five steps (it asks first; `--yes` answers): (1) apt build/runtime packages, (2) Mozilla's Firefox deb
with the apt pin and a copy of the snap profile, optionally removing the snap, (3) the Bluetooth step,
(4) `credentialsd` and a patched `xdg-desktop-portal` built from source into `/usr/local`, (5) the
native-messaging link and the Mozilla-signed `credentialsd-helper` add-on. **Step 3 is the installer's own
logic**: a local adapter (`/sys/class/bluetooth/hci*`) or an existing virtual one → nothing to do; none but a
reachable Home Assistant (`$HA_URL`, `homeassistant.local`, tailscale peers on :8123) → it installs
`ha-ble-vhci` as a user service with `/dev/vhci` access; neither → it asks for a USB dongle. The module
never decides this. Verified by running the real installer's `--dry-run --yes install` here: with the
machine's `hci0` the step prints `local adapter(s) found: hci0 - nothing to do`; with an empty
`PASSKEY_QR_BLUETOOTH_DIR` and `HA_URL` set it lists the bridge commands.

- *One command on a new machine*: enable the module in the profile (`profile.toml` for every machine, or one
  `machines/<host>/machine.toml`), store the Home Assistant token if the PC has no Bluetooth, run bootstrap:

  ```toml
  [modules.firefox-passkey-qr]
  enabled = true
  # ha_url = "http://100.x.y.z:8123"   # only when the lookup (homeassistant.local, tailscale peers) must not be used
  # remove_snap = true                  # also remove the Firefox snap once the deb works (default: keep it)
  ```

  ```sh
  secret-tool store --label='Home Assistant token' application ubuntu-tweaks purpose ha-token   # once, PC without Bluetooth
  sh bootstrap.sh --yes      # the module's step runs `passkey-qr --yes install`
  ```

  The installer's package, build and install steps were run on a stock Ubuntu 26.04.1 root in an unprivileged
  `bwrap` (no systemd session); the session-bound steps were verified on `ubuntu-pc`.
- *Preview*: the module's `plan` is the installer's `--dry-run` output (every command, `sudo` marked, the
  token never printed), indented under the action.
- *Run*: only through the installer's own bypass: `passkey-qr --yes [--remove-snap] install`
  (<https://github.com/alex-mextner/firefox-passkey-qr/pull/2>). `--yes` never prompts and never closes,
  starts or opens a browser; **the snap is kept unless `--remove-snap`** (profile option `remove_snap`,
  default false). What only a person can do does not stop the other steps: it is skipped, listed at the end
  and the installer exits **3** (reserved: its failures exit 1), which the module reports as `needs manual`
  pointing at the lines the installer printed (the fact stays Missing, the next `apply` continues; any other
  non-zero exit stays an error). What waits for a person: Home Assistant's token (the Bluetooth step only), a running Firefox (the
  profile copy and the switch of the start profile), no Bluetooth and no Home Assistant at all (a USB dongle), a
  second Firefox install registered in `installs.ini`, and a session that has to log out and in once for the
  new D-Bus activation files. Firefox is not restarted: the policy is in place and the extension installs
  at its next start. With an older checkout that has no `--yes`, the module reports `needs manual: run
  <installer> install interactively`; it never runs the installer without `--yes` and never fakes success.
  The feature is detected from the installer's `--help`, not from a version number.
- *Check*: the installer's `check` (exit 0/3, read-only, no log file): Firefox deb, a Bluetooth adapter, the
  build dependencies, `credentialsd`, libdex and the patched portal installed, the portal answering with the
  Credential interface, the native-messaging link, the portal accepting the helper's app id, the Firefox
  policy entry for the add-on; an installer without `check` is judged from its `status` rows. The row
  "Firefox restarted: extension active" is context only: it needs a started Firefox.
- *Policy entry*: the installer writes the add-on into `/etc/firefox/policies/policies.json` itself
  (`normal_installed`, merged with the other entries) and treats a `force_installed` entry for the same URL as
  already satisfied, so `[modules.firefox-policy] credentialsd_helper = true` and this module never fight over
  the file.
- *Secrets*: Home Assistant's long-lived token comes from the Secret Service (`purpose ha-token`) into the
  installer's `HA_TOKEN` for that run only; the installer itself stores it in
  `~/.config/ha-ble-vhci/token` (mode 600), which is outside every repository and profile, and its URL is the
  non-secret option `ha_url`. The installer asks for `sudo` itself: run `apply --commit` in a terminal.
- *Order*: after `firefox-autologin`; the snap profile (with the extension and the marker) is copied by the
  installer, so nothing is opened twice.

Real-root and real-session verification (not possible in tests): after `apply --commit` of the Firefox modules,
`about:policies` lists the `ExtensionSettings` entry, `about:addons` shows *Passwords* as installed by the
organization, a login page is filled, `ubuntu-tweak status --only firefox,firefox-policy,firefox-autologin`
exits 0; the first start shows the WebExtension-backend prompt (answer *Allow*). For the passkey module: on a
machine without Bluetooth and with Home Assistant, `bluetoothctl list` shows a virtual `hciN` and
`webauthn.io` offers the phone/QR option.

### Install

```sh
gh auth login                                          # once: the dotfiles repository is found through your gh login
git clone --recurse-submodules https://github.com/alex-mextner/ubuntu-tweaks ~/ubuntu-tweaks
~/ubuntu-tweaks/bin/ubuntu-tweak init            # preview: doctor, submodules, CLI link, apply plan
~/ubuntu-tweaks/bin/ubuntu-tweak init --commit   # do it
```

`install.sh` stays as a two-line wrapper, `exec bin/ubuntu-tweak init --commit "$@"`: the old
script always installed, so the old instructions keep working and keep meaning "install".
`init --commit` clones the user's dotfiles repository itself (module `dotfiles`, *Profile discovery*); when
gh finds none, `init` says so and applies the modules only. `scripts/bootstrap.sh` clones the engine to
`$UBUNTU_TWEAKS_DIR` (default `~/ubuntu-tweaks`) from `$UBUNTU_TWEAKS_REPO` (default: this project's repository).

### Testing

- Unit (`python3 -m unittest`, TDD): profile resolution and config merge precedence,
  manifest parsing and validation, plan filtering, preview rendering and root skipping,
  keyfile round trip, plan building from a fake settings backend, drift classification,
  migrations, seed providers.
- Round trip (`tests/test_round_trip.py`): `capture --commit` of a fake machine into an empty
  profile, then the settings and files planners against an empty fake machine must return the
  same keys as `set-key` actions (including relocatable schemas and machine-overlay keys) and
  the captured files as `link` actions; applying them converges to the captured keys.
- Bundles (`tests/test_bundles.py`, `tests/test_keyfile_doc.py`, `tests/test_toml_edit.py`, no
  PyGObject needed): discovery of nested bundles in both layers, header validation, `machines` and
  `requires-module` filtering, conflict (exit 2, both files and lines) versus duplicate warning,
  shadows, comment-preserving edits, `bundles`, `bundle-move` and `note`. Capture tests
  (`tests/test_capture.py`) cover routing order (the #37 case included), `[capture.bundles]`,
  `auto` and `include`, changed-only, the SUMMARY block, `--dry-run` / `--summary` exclusivity and a
  `capture --commit` into a profile of hand-written, commented bundles that must come out
  byte-identical except for the changed values.
- Integration: `scripts/integration.sh` runs `dbus-run-session` with a scratch
  `DCONF_PROFILE` and a temporary `HOME`/`XDG_*` (including `XDG_RUNTIME_DIR`) against a scratch
  copy of the engine with a fixture module (so no network or root is needed): a module settings
  preset, two shared bundles (a header, a reason comment, a key from another schema) and a machine
  overlay bundle that wins over them, a shared and a machine file
  to link and the `english-in-overview` extension are applied with `apply --commit`; `status`
  exits 0, a second `apply --commit` reports `0 changed`, and a `capture` preview reads the
  result back without writing. The scratch environment is exported **before**
  `dbus-run-session` starts, because the dconf service is activated by the bus daemon and
  inherits the daemon's environment: variables set only inside the session would let it write
  the user's real dconf database. The script refuses to run unless it sees the scratch
  environment and a session bus other than its parent's, and skips itself (loudly, exit 0) when the
  `org.gnome.shell` or `org.gnome.desktop.interface` schema is not installed (CI installs
  `gnome-shell-common` and `gsettings-desktop-schemas`). Every test that writes through Gio/dconf
  (`tests/gio_scenario.py`, `tests/gio_planner_scenario.py`) calls `tests/scratch_guard.py`
  first and exits 2 on anything but a fully scratch HOME/XDG/DCONF_PROFILE and a private bus.
  Wired into CI
  ([#18](https://github.com/alex-mextner/ubuntu-tweaks/issues/18) for the rest of the
  hardening). Tests that need PyGObject run under the distro interpreter, because
  `python3-gi` is an apt package.
- Init pipeline and base modules (`tests/test_manifest_pipeline.py`, `tests/test_planner_base.py`,
  `tests/test_init_pipeline.py`, `tests/test_system_modules.py`, `tests/test_actions.py`; no PyGObject
  needed except `test_init_pipeline.py`): `order` / `phase` / `[install]` / `[[step]]` / `[[post]]` /
  `root` / `preview` validation; the one apt batch and its `provides`; login shell, rc files (never
  overwritten, `O_EXCL`), git identity, `gh` login through the keychain (no token in argv, manual step
  on every keychain failure), brew prefix and installer (tampered script refused, never run); posts and
  their placeholders; `(phase, order)` plan ordering; exit 10 as a manual step; the `init` preview on an
  empty machine as a golden file (`tests/golden/init_empty_machine.txt`; regenerate with
  `UPDATE_GOLDEN=1` and review the diff); `doctor` reporting `planned`. Everything goes through
  `FakeProcess` and a fake root directory (`StepEnv.root`); nothing touches the real system.
- Elevation and the repair run (`tests/test_elevation.py`, `tests/test_root_wiring.py`; `FakeProcess` and a `Host`
  value, never the real sudo): already root, NOPASSWD, the terminal path asking once for N root actions and
  refreshing the ticket, askpass, pkexec, and the typed errors (`no-new-privs`, `no-tool`, `no-prompt`, wrong
  password) with exit 6 and the command in the message; `--no-sudo`, previews, `--dry-run` and plans without root
  actions never ask; `apply` and `init` acquire before the first root action and install the missing packages in
  the same run without a question (`tests/test_apply_commands.py`); `--dry-run` for every command, with `--commit`
  a usage error (`tests/test_dry_run.py`).
- Dotfiles rc files, git identity and staged settings (`tests/test_dotfiles_links.py`, `tests/test_planner_base.py`
  `RcFileTests` / `GitTests`, `tests/test_planner_settings.py` `PendingExtensionSchemaTests`): the repository's
  `.zprofile` / `.zshrc` / `.profile` become `dotfiles-link` actions (a real file is `dotfiles-link-replace` with a
  backup), the profile's `files/` wins, `--only` and a disabled module drop them, `shell-rc` plans only what neither
  provides; the identity comes from the profile, git's own value, the dotfiles `.gitconfig` or `gh api user` in that
  order and warns, with what to do, only when nothing knows it; settings of a submodule that is not checked out are
  `staged`, a checked-out extension without the schema is still `skipped`; the `init` golden file shows the new
  order (`dotfiles` before `shell-rc` before `brew`).
- Profile discovery and profile modules (`tests/test_identity.py`, `tests/test_profile_modules.py`,
  `modules/dotfiles/tests/test_install.py`; no PyGObject needed): a fake `gh` (found, none, several, forks and
  archived skipped, logged out, missing, offline, a listing as long as the limit), the cache (one hour, one
  minute for an unreachable GitHub, a new gh account), checkouts of another repository refused, the clone
  target and the profile directory rules, `--dotfiles-repo` / `--refresh`; a profile module discovered,
  overriding an engine module of the same name, planned like any other and run only at `--commit`; the
  `dotfiles` installer against a throwaway `HOME`, a local git repository and a stub `gh` executable.
  `tests/test_no_identity.py` greps the engine tree for the author's identifiers and fails on any new hit
  that `tests/identity_allowlist.txt` does not allow: that file lists, one `path | reason` line each, the
  project references (the repository URL, a bundled upstream), the tool references of the author-published
  modules (default repository URL, extension id, bus name) and verbatim requests, and an entry whose file has
  no hit any more fails too.

## Decisions

- **`rig` as the model for the command surface (request 18); what stays different.** `rig`'s `init` / `apply` /
  `doctor` / `--dry-run` were read (`~/dev/rig-cli`, `riglib/cli.py`, README). Aligned: `init` and `apply` are the
  only commands that set a machine up, `apply` previews by default and one run converges (idempotent, `0 changed`),
  `doctor --yes` installs dependencies, `--only`, exit 0 ok / 1 internal / 2 invalid config / 3 drift / 4 unknown
  item. Kept different, because a decision of the user is recorded (preview-by-default with an explicit
  `--commit`, requests 7 and 8, invariant 1) or the project has its own meaning: (1) the commit word is `--commit`
  on `apply`, `init`, `update`, `capture`, `note`, `bundle-move` (rig: the positional `apply commit`, and `--commit`
  only on `config set`); (2) `--dry-run` together with `--commit` is a usage error, exit 2 (rig: `--dry-run`
  silently wins over `commit`), as ordered in request 19; (3) rig's `apply --yes` means "commit"; here `--yes` is
  only `doctor`'s commit word, and `init` / `apply` need no `--yes` at all (`--commit` is the consent and the apt
  install is part of it); (4) rig's `init` only scaffolds and its `apply` never installs dependencies: here
  `init --commit` / `apply --commit` install the missing apt packages too (request 18); (5) exit codes 5 and 6 differ
  (rig: missing target / not a repo; here: refused / no root); (6) no `--plan` / `--notes` / `-v` on `apply` yet.
- **Python, stdlib plus `python3-gi`**, not bash: the plan/preview and the typed dconf
  diff are the core of the tool, and bash cannot compare GVariants. Not a plugin of a personal dev
  tool: this repo is public, and its users have no reason to install one.
- **The engine is `ubuntu-tweaks`, the profile is the `ubuntu/` directory of the user's dotfiles
  repository**: the CLI, the plan model and the modules already live here; a second tool would duplicate
  them. Personal data goes to the user's own repository only, so the public engine stays shareable and
  names nobody (request 14).
- **TOML manifests per module**, not one central file: a module is added or removed as
  one directory, and `tomllib` is in the stdlib.
- **No lockfile**: drift is recomputed from the modules against the machine each time, as
  other reconcilers do. The submodule commit recorded in git is the version lock.
- **The repo's preset wins on `apply`** (enforce), the user's taste lives in the profile:
  otherwise every run would have to guess which side changed.
- **Only the keys a bundle lists are enforced**: absent keys are never reset, so a
  profile can be adopted bundle by bundle.
- **Extras are never removed**: the tool only undoes what it declared. `packages.toml`
  installs and never removes for the same reason.
- **Leaves, not directories, are linked** under `$HOME`: apps keep creating siblings in
  their own config directories.
- **`capture` is rules, not a picker**: reproducible, reviewable, and the rules are versioned.
- **Bundles, not one file per panel**: grouping by purpose is how a person thinks about
  settings ("everything for my dev keyboard"), and Home Assistant's `packages:` shows that merging
  many small files at load costs little. The merge is explicit and checked (a key in two bundles
  with different values is an error, never a silent pick), and `bundle-move` makes regrouping one
  command, so the layout stays the user's choice rather than the tool's.
- **Reasons are comments, edited in place**: a profile is read by a person in `git diff`; a reason
  stored anywhere else (a sidecar file, a key) would drift away from its key. The engine therefore
  edits keyfiles and the two TOML files with line-oriented editors that change the least text and
  verify the result, instead of regenerating files from parsed data.
- **`auto` is a class, not silence**: keys an app or the Shell registers by itself (ids, ordering
  lists) are not choices; dropping them quietly would hide what capture decides, so they get their
  own counted section and a one-line way back (`[capture] include`).
- **Auto-apply is off until #19**: `apply` enforces and must not overwrite live tweaks the
  user has not seen.
- **The hyperdock uuid is read from `metadata.json`**: the umbrella works before and after
  hyperdock#3; the migration covers the switch.
- **Dev clone remotes**: in `~/xp/dash2dock-lite`, `origin` is `alex-mextner/hyperdock` (so recorded reviews
  and `gh ship` see the right repository), `upstream` is icedman, and `fork` is kept as an alias of `origin`.
  Tickets for hyperdock are filed with `--repo alex-mextner/hyperdock`.
- **Engine base list vs profile list**: the engine ships only the minimal bootstrap its own pipeline
  needs (`modules/base-tools`, `clipboard`, `git-gh`, `zsh`, `brew`); a person's other tools are
  `apt` / `snap` / `flatpak` / `brew` in the profile's `packages.toml`, with `# reason` comments. Both are
  installed by the same apply and unioned, so the public engine never knows the personal list.
- **Named steps, not code per module**: what a command line cannot express (a passwd lookup, a file made
  only while absent, a download checked against a pinned hash) is a built-in step looked up by name from
  `[[step]]`, like seed providers; everything else is a `[[post]]` command with its own check.
- **`gh` from the Ubuntu archive**: 26.04 has `gh` 2.46.0 in `universe`; no third-party apt repository or
  signing key is added just for it. A newer brew `gh` already on `PATH` satisfies the package.
- **A missing credential is a manual step, not a failure**: `apply` never prompts and never stores a
  secret; it reports `needs manual: ...` and `status` keeps the action Missing.

## Cutover checklist

- `.gitmodules`: `gnome-extensions/dash2dock-lite@icedman.github.com` →
  `gnome-extensions/hyperdock`, URL `https://github.com/alex-mextner/hyperdock.git`.
- `dconf/dock.ini` → `modules/hyperdock/dock.ini`; `install.sh` logic → modules +
  actions; `install.sh` → wrapper.
- README: install via `ubuntu-tweak init`, commands table, module list; links to
  `alex-mextner/dash2dock-lite` here and in `docs/specs/mac-dock.md` → `alex-mextner/hyperdock`.
- On this machine: `ubuntu-tweak apply --commit` replaces the old symlink
  `dash2dock-lite@icedman.github.com` → new submodule path, then `status` exits 0.

## Out of scope here (tickets)

- Login-time or scheduled auto-apply and release-channel auto-update: [#19](https://github.com/alex-mextner/ubuntu-tweaks/issues/19)
  (opt-in user-level systemd timer, default off).
- `ego` source kind and auto dark mode: [#20](https://github.com/alex-mextner/ubuntu-tweaks/issues/20).
- Tests, checks, hooks and CI beyond what the profile work needs: [#18](https://github.com/alex-mextner/ubuntu-tweaks/issues/18).
- Window overview as its own repo: [#17](https://github.com/alex-mextner/ubuntu-tweaks/issues/17).
- Secrets sync (keyring, SSH keys, tokens): never in the profile. A separate mechanism
  (sops/age, keychain) if wanted later.
- The work items of the profile sync itself (engine actions, capture, system modules, profile
  scaffold): [#25](https://github.com/alex-mextner/ubuntu-tweaks/issues/25).
