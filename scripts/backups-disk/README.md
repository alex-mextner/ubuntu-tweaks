# backups-disk scripts

Owner-run (sudo) helpers that move cold bulk data from the home directory to the 1 TB USB disk "backups" and keep
every original path working through bind mounts (fstab, `nofail`). Reference for the layout: the header of
`move-to-backups.sh`.

| file | purpose |
| --- | --- |
| `move-to-backups.sh` | `prep` / `move` / `status` / `cleanup`. **Idempotent**: finished items are skipped, an interrupted item continues where it stopped. |
| `relocate-chihuahua.sh` | one-off (already run): moved two projects on the disk under `xp/garage-band/projects`. Sources `move-to-backups.sh` for its helpers. |
| `test-move-to-backups.sh` | stub tests (no root, no mounts, no signals): `.cache` holder rule, follow-up rsync passes, watchdog guard. |

## Where the live copies are

The scripts are run from `<home>/move-to-backups.sh` and `<home>/relocate-chihuahua.sh` (plain files, **not**
symlinks into this repo). Reason: this repo lives under `<home>/xp`, which is itself a bind mount of the USB disk;
a symlink there would break `sudo <home>/move-to-backups.sh` whenever the disk is missing (boot, unplugged) and would
put the running script inside the tree `move` is about to move. So the repo copy is the reviewed source and the home
copy is deployed from it:

    cp scripts/backups-disk/move-to-backups.sh scripts/backups-disk/relocate-chihuahua.sh <home>/
    cmp scripts/backups-disk/move-to-backups.sh <home>/move-to-backups.sh   # drift check, silent when equal

Edit the repo copy, run `test-move-to-backups.sh`, then deploy.
