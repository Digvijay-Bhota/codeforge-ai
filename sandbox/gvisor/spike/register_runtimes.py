"""Merge the spike's runtime registrations into a Docker daemon.json.

Used only on the disposable CI runner (``sudo python3 -I register_runtimes.py
runtimes.json /etc/docker/daemon.json --runc "$(command -v runc)"``).  Unlike
``runsc install`` it never rewrites unrelated configuration:

* every top-level key other than ``runtimes`` is kept exactly as it was;
* existing runtime entries are kept; an existing entry with one of our names
  but a different definition is an error, not an overwrite;
* the original file is backed up next to it and the new one is written
  atomically with the original permissions.

``{RUNC}`` in a runtime path is replaced by the ``--runc`` argument (the impostor
registration).  The Docker daemon must be restarted afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise SystemExit(f"{path} is not a JSON object")
    return data


def merge(existing: dict[str, Any], wanted: dict[str, Any]) -> dict[str, Any]:
    runtimes = existing.get("runtimes", {})
    if not isinstance(runtimes, dict):
        raise SystemExit("existing 'runtimes' is not a JSON object")
    merged_runtimes = dict(runtimes)
    for name, definition in wanted.items():
        if name in runtimes and runtimes[name] != definition:
            raise SystemExit(f"runtime {name!r} is already registered differently; refusing to overwrite")
        merged_runtimes[name] = definition
    merged = dict(existing)
    merged["runtimes"] = merged_runtimes
    # Nothing but our runtime entries may change.
    others_kept = {k: v for k, v in merged.items() if k != "runtimes"} == {
        k: v for k, v in existing.items() if k != "runtimes"
    }
    if not others_kept or any(merged_runtimes[k] != v for k, v in runtimes.items()):
        raise SystemExit("merge would change unrelated daemon configuration")
    return merged


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtimes", type=Path)
    parser.add_argument("daemon_json", type=Path)
    parser.add_argument("--runc", required=True)
    args = parser.parse_args()

    runc = args.runc
    if not os.path.isabs(runc) or not os.access(runc, os.X_OK):
        raise SystemExit(f"--runc must be an absolute path to an executable, got {runc!r}")
    wanted = json.loads(args.runtimes.read_text())
    for definition in wanted.values():
        definition["path"] = definition["path"].replace("{RUNC}", runc)
        if not os.path.isabs(definition["path"]):
            raise SystemExit(f"runtime path {definition['path']!r} is not absolute")

    target: Path = args.daemon_json
    existing = _load(target)
    merged = merge(existing, wanted)

    print("daemon.json before:", json.dumps(existing, indent=2, sort_keys=True))
    mode = 0o644
    if target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
        backup = target.with_name(target.name + ".codeforge-backup")
        backup.write_bytes(target.read_bytes())
        os.chmod(backup, mode)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".daemon.json.")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(merged, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    print("daemon.json after:", json.dumps(merged, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
