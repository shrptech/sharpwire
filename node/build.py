"""Build the node bootstrap package: dist/sharpwire-node-<version>.tar.gz.

    uv run python node/build.py

The tarball is reproducible: fixed order, owners and timestamps, so the same source gives the same bytes.
"""

from __future__ import annotations

import gzip
import io
import re
import sys
import tarfile
from pathlib import Path

NODE = Path(__file__).resolve().parent
# Published path in the tarball → (source, mode).
CONTENTS = {
    "install.sh": ("install.sh", 0o755),
    "sharpwire_node.py": ("sharpwire_node.py", 0o755),
    "sharpwire-restore.service": ("sharpwire-restore.service", 0o644),
    "sudoers": ("sudoers", 0o644),
    "README.md": ("README.md", 0o644),
}


def helper_version() -> int:
    match = re.search(r"^VERSION = (\d+)$", (NODE / "sharpwire_node.py").read_text(), re.MULTILINE)
    if match is None:
        raise SystemExit("sharpwire_node.py has no `VERSION = <n>` line")
    return int(match.group(1))


def build(out_dir: Path) -> Path:
    version = helper_version()
    prefix = f"sharpwire-node-{version}"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, (source, mode) in sorted(CONTENTS.items()):
            data = (NODE / source).read_bytes()
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(data))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{prefix}.tar.gz"
    with out.open("wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as gz:
        gz.write(raw.getvalue())
    return out


if __name__ == "__main__":
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else NODE.parent / "dist"
    print(build(out_dir))
