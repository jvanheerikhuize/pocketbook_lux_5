"""pbtk.toml: what a consuming repo (mmIV, inkquest, ...) tells pbtk about its app."""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import PbtkError

FILENAME = "pbtk.toml"


@dataclass
class Project:
    root: Path
    name: str
    dist: Path
    log: str | None  # device path
    build: str | None
    #: globs (relative to dist, e.g. "mmiv/saves/*") only copied when missing on the reader
    keep: list[str] = field(default_factory=list)
    #: how app screenshots are turned (none, cw, ccw); taps use the same coordinates
    view: str = "none"

    def kept(self, rel: str) -> bool:
        return any(fnmatch.fnmatchcase(rel, pat) for pat in self.keep)

    @property
    def device_log(self) -> str | None:
        return self.log


VIEWS = ("none", "cw", "ccw")


def _view(v: str, where) -> str:
    if v not in VIEWS:
        raise PbtkError(f"{where}: view must be one of {', '.join(VIEWS)}")
    return v


def find(start: Path | None = None) -> Path | None:
    p = (start or Path.cwd()).resolve()
    for d in (p, *p.parents):
        if (d / FILENAME).is_file():
            return d / FILENAME
    return None


def load(path_or_dir: Path | str | None = None) -> Project:
    p = Path(path_or_dir) if path_or_dir else None
    if p and p.is_dir():
        p = p / FILENAME
    if p is None:
        p = find()
    if p is None or not p.is_file():
        raise PbtkError(f"no {FILENAME} found (looked from {Path.cwd()} upwards)")
    data = tomllib.loads(p.read_text())
    app = data.get("app") or {}
    name = app.get("name")
    if not name:
        raise PbtkError(f"{p}: [app] name is required")
    root = p.parent
    log = app.get("log")
    if log and not log.startswith("/"):
        log = "/mnt/ext1/applications/" + log
    return Project(
        root=root,
        name=name,
        dist=root / app.get("dist", "dist/applications"),
        log=log,
        build=app.get("build"),
        keep=list(app.get("keep") or []),
        view=_view(app.get("view", "none"), p),
    )
