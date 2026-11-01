"""T0 transport: the reader's internal storage mounted as a USB disk.

While mounted, the reader shows its "connected to PC" screen and no app runs.
Device paths are written as on the reader (/mnt/ext1/...); this module maps
them onto the mount point.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import PbtkError

DEVICE_ROOT = "/mnt/ext1"
MARKER = Path("system") / ".pocketbook"


def find_mount() -> Path | None:
    """The mount point of the reader's internal storage, or None."""
    env = os.environ.get("PBTK_MOUNT")
    if env:
        p = Path(env)
        return p if (p / MARKER).exists() or (p / "applications").is_dir() else None
    user = getpass.getuser()
    candidates: list[Path] = []
    for base in (Path("/run/media") / user, Path("/media") / user, Path("/media"), Path("/mnt")):
        try:
            candidates += sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
    for p in candidates:
        try:
            if (p / MARKER).exists():
                return p
        except OSError:
            continue
    return None


@dataclass
class UsbStorage:
    mount: Path

    @classmethod
    def connect(cls) -> "UsbStorage":
        m = find_mount()
        if m is None:
            raise PbtkError(
                "Reader storage not mounted. Plug in USB and choose 'PC link' on the reader "
                "(or set PBTK_MOUNT)."
            )
        return cls(m)

    # ------------------------------------------------------------- paths
    def local(self, device_path: str) -> Path:
        """Map /mnt/ext1/x (or a path relative to it) onto the mount."""
        p = device_path
        if p.startswith(DEVICE_ROOT):
            p = p[len(DEVICE_ROOT) :]
        p = p.lstrip("/")
        out = (self.mount / p).resolve()
        if out != self.mount.resolve() and self.mount.resolve() not in out.parents:
            raise PbtkError(f"path escapes the reader storage: {device_path}")
        return out

    # ------------------------------------------------------------- files
    def ls(self, device_path: str = "/mnt/ext1") -> list[dict]:
        d = self.local(device_path)
        if not d.is_dir():
            raise PbtkError(f"not a directory on the reader: {device_path}")
        out = []
        for e in sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            st = e.stat()
            out.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size, "mtime": int(st.st_mtime)})
        return out

    def read(self, device_path: str) -> bytes:
        p = self.local(device_path)
        if not p.is_file():
            raise PbtkError(f"no such file on the reader: {device_path}")
        return p.read_bytes()

    def write(self, device_path: str, data: bytes) -> None:
        p = self.local(device_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".pbtk-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def push(self, local_path: Path, device_path: str) -> list[str]:
        """Copy a file or tree; returns the device paths that changed."""
        src = Path(local_path)
        dst = self.local(device_path)
        changed: list[str] = []
        files = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
        for f in files:
            target = dst if src.is_file() else dst / f.relative_to(src)
            if _same(f, target):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, target)  # FAT keeps no modes, so copy data + mtime only
            os.utime(target, (f.stat().st_atime, f.stat().st_mtime))
            changed.append(DEVICE_ROOT + "/" + str(target.relative_to(self.mount)))
        return changed

    def pull(self, device_path: str, local_path: Path) -> list[Path]:
        src = self.local(device_path)
        dst = Path(local_path)
        if src.is_file():
            if dst.is_dir():
                dst = dst / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return [dst]
        out = []
        for f in sorted(p for p in src.rglob("*") if p.is_file()):
            t = dst / f.relative_to(src)
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, t)
            out.append(t)
        return out

    def deploy(self, dist: Path) -> list[str]:
        """Copy a built `applications/` tree (bundles + .app launchers) to the reader."""
        dist = Path(dist)
        if not dist.is_dir():
            raise PbtkError(f"nothing to deploy: {dist} does not exist (build first)")
        changed = self.push(dist, "/mnt/ext1/applications")
        self.sync()
        return changed

    def tail(self, device_path: str, lines: int = 50) -> str:
        p = self.local(device_path)
        if not p.is_file():
            raise PbtkError(f"no log at {device_path}")
        data = p.read_bytes()[-256 * 1024 :]
        return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])

    def install_token(self, token: str) -> str:
        path = "/mnt/ext1/applications/pbagent.token"
        self.write(path, (token + "\n").encode())
        self.sync()
        return path

    # ----------------------------------------------------------- device
    def block_device(self) -> str | None:
        target = str(self.mount.resolve())
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].replace("\\040", " ") == target:
                        return parts[0]
        except OSError:
            pass
        return None

    def sync(self) -> None:
        try:
            os.sync()
        except AttributeError:  # pragma: no cover
            pass

    def eject(self) -> str:
        """Unmount and power off the USB disk, so the reader returns to its UI."""
        dev = self.block_device()
        if not dev:
            raise PbtkError(f"cannot find the block device for {self.mount}")
        if not shutil.which("udisksctl"):
            raise PbtkError("udisksctl not found; eject the reader from the file manager")
        self.sync()
        for cmd in (["udisksctl", "unmount", "-b", dev], ["udisksctl", "power-off", "-b", dev]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise PbtkError(f"{' '.join(cmd)} failed: {r.stderr.strip() or r.stdout.strip()}")
        return dev


def _same(a: Path, b: Path) -> bool:
    """Equal size and mtime within FAT's 2 s resolution."""
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return sa.st_size == sb.st_size and abs(sa.st_mtime - sb.st_mtime) <= 2
