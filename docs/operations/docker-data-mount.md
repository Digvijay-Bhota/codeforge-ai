# Docker started before its data was mounted

This applies to a host whose Docker data lives on a separate, later-mounted
filesystem. The development host keeps it on `/mnt/devdata`:

- `/etc/docker/daemon.json` sets `"data-root": "/mnt/devdata/docker"`;
- `/var/lib/containerd` (containerd's default root; there is no
  `/etc/containerd/config.toml`) is a bind mount of `/mnt/devdata/containerd`;
- both `/etc/fstab` entries use `nofail`, and neither `docker.service` nor
  `containerd.service` declares a dependency on those mounts.

Why that allows the race: with `nofail`, a mount is "only wanted, not
required, by local-fs.target ... Moreover the mount unit is not ordered
before these target units" (`systemd.mount(5)`). So nothing in the boot
order makes the daemons wait for `/mnt/devdata`; whether they see the data
depends on timing.

## Symptom

After a reboot Docker shows an empty world: `docker ps -a` lists nothing,
`docker volume ls` is empty, and `docker images` fails with `blob ... not
found`. `docker info` still reports the configured data root.

On 2026-10-22 systemd started containerd 13.8 s and Docker 19.5 s after boot,
but mounted `/mnt/devdata` at 22.3 s and `/var/lib/containerd` at 22.4 s. Both
daemons opened the empty directories underneath the mount points. The data on
the disk was untouched. It was only hidden.

**Do not** prune, remove or recreate anything, and do not run `docker compose
up`: Compose would create new, empty volumes (a fresh database) in the hidden
directory.

## 1. Diagnose (read-only, no privileges)

```bash
python3 -I infra/scripts/docker_data_preflight.py --expect-mount /mnt/devdata \
    --require-volume cf-gh-test_postgres_data
```

It exits 0 when Docker and containerd started after the mounts holding their
data, the data root matches `daemon.json`, the mounts exist, and the named
volumes are visible. Otherwise it exits 1 and names each mismatch. It changes
nothing and prints only paths, unit names, times and volume names. The same
facts by hand:

```bash
findmnt /mnt/devdata; findmnt /var/lib/containerd   # one path per call
systemctl show docker.service containerd.service mnt-devdata.mount var-lib-containerd.mount \
    -p Id -p ActiveEnterTimestampMonotonic
docker info --format '{{.DockerRootDir}} containers={{.Containers}}'
df -h /mnt/devdata        # usage unchanged since before the reboot = data still there
```

A daemon whose `ActiveEnterTimestampMonotonic` is smaller than its mount's
started first, and is using the wrong directory.

## 2. Recover (administrator; needs sudo)

Only after `findmnt /mnt/devdata` and `findmnt /var/lib/containerd` show the mounts:

```bash
sudo systemctl restart containerd docker
```

Containers with a restart policy (the `cf-gh-test` stack uses
`unless-stopped`) start again on their own. Nothing is deleted by this.

## 3. Verify after the restart

```bash
python3 -I infra/scripts/docker_data_preflight.py --expect-mount /mnt/devdata \
    --require-volume cf-gh-test_postgres_data          # must print OK
docker volume ls                                      # the old volumes are back
cd /mnt/devdata/gh-app-test && ./dc.sh ps && ./verify.sh
```

`verify.sh` checks the services, configuration (execution disabled), webhook
self-test, Docker socket absence, migrations and stored rows. The isolated
test stack's `dc.sh` runs the preflight itself before every Compose command
except `config`, and stops (exit 3) on a mismatch.

## 4. Prevent it (administrator review; needs sudo)

Make both daemons wait for the filesystems they use. `RequiresMountsFor=`
adds `Requires=` and `After=` on the mount units for those paths:

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d /etc/systemd/system/containerd.service.d
printf '[Unit]\nRequiresMountsFor=/mnt/devdata /var/lib/containerd\n' \
  | sudo tee /etc/systemd/system/docker.service.d/devdata.conf \
             /etc/systemd/system/containerd.service.d/devdata.conf
sudo systemctl daemon-reload
systemctl show docker.service containerd.service -p RequiresMountsFor   # check
```

`daemon-reload` is required: until it runs, systemd keeps the old unit
definition (`NeedDaemonReload=yes` shows that it is pending). It changes
nothing for the running daemons; the new ordering applies the next time they
start (at boot). No restart is needed to install it.

### Verify the installation (no privileges)

The two files are only half of it; check what systemd actually loaded:

```bash
systemctl show docker.service containerd.service \
    -p Id -p DropInPaths -p RequiresMountsFor -p NeedDaemonReload
# expected for each: DropInPaths=/etc/systemd/system/<service>.d/devdata.conf
#                    RequiresMountsFor=/mnt/devdata /var/lib/containerd
#                    NeedDaemonReload=no
for u in docker.service containerd.service; do
  systemctl show "$u" -p Requires -p After --value | tr ' ' '\n' \
    | grep -E '^(mnt-devdata|var-lib-containerd)\.mount$' | sort | uniq -c
done
# expected: each mount unit listed twice per service (Requires= and After=)
systemctl cat docker.service | grep -E '^# /'   # the unit file and its drop-ins
```

The existing dependencies (`docker.socket`, `containerd.service`,
`network-online.target` for Docker) must still be present: a drop-in adds to
the unit, it does not replace it.

If the files exist but `DropInPaths` is empty, `daemon-reload` has not run, or
the file name or directory is wrong (it must end in `.conf` and sit in
`<unit name>.d/`). If the files do not exist at all, the commands did not run
on this host; `journalctl --since today _COMM=sudo | grep COMMAND=` shows which
privileged commands did.

**What this proves:** that the next boot will order the daemons after the
mounts, as configured. It does not prove that the next boot went well. After
a reboot, `docker_data_preflight.py` compares the actual activation times
and reports any daemon that started before its mount.

Trade-off: if the disk is missing at boot (its fstab entries are `nofail`),
Docker and containerd will not start at all instead of starting empty. That
is the intended behaviour for a host whose only Docker data is on that disk.
To undo, delete the two `devdata.conf` files and run `sudo systemctl
daemon-reload`.

The empty directories the daemons created under the mount points during the
bad boot stay hidden underneath the mounts. They hold no data that matters
and need no action.
