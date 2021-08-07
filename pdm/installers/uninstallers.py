from __future__ import annotations

import abc
import csv
import glob
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable, Type, TypeVar, cast

from pip._vendor import pkg_resources

from pdm import termui
from pdm.exceptions import UninstallError
from pdm.installers.packages import CachedPackage
from pdm.models.environment import Environment
from pdm.utils import is_dist_editable

_T = TypeVar("_T", bound="BaseRemovePaths")


def get_egg_link_path(
    dist: pkg_resources.Distribution, site_packages: str
) -> str | None:
    """Find the .egg-link file for the editable distribution"""
    egglink = os.path.join(site_packages, dist.project_name) + ".egg-link"
    if os.path.isfile(egglink):
        return egglink
    return None


def renames(old: str, new: str) -> None:
    """Like os.renames(), but handles renaming across devices."""
    # Implementation borrowed from os.renames().
    head, tail = os.path.split(new)
    if head and tail and not os.path.exists(head):
        os.makedirs(head)

    shutil.move(old, new)

    head, tail = os.path.split(old)
    if head and tail:
        try:
            os.removedirs(head)
        except OSError:
            pass


def compress_for_rename(paths: Iterable[str]) -> set[str]:
    """Returns a set containing the paths that need to be renamed.

    This set may include directories when the original sequence of paths
    included every file on disk.
    """
    case_map = {os.path.normcase(p): p for p in paths}
    remaining = set(case_map)
    unchecked = sorted({os.path.split(p)[0] for p in case_map.values()}, key=len)
    wildcards: set[str] = set()

    def norm_join(*a):
        # type: (str) -> str
        return os.path.normcase(os.path.join(*a))

    for root in unchecked:
        if any(os.path.normcase(root).startswith(w) for w in wildcards):
            # This directory has already been handled.
            continue

        all_files: set[str] = set()
        all_subdirs: set[str] = set()
        for dirname, subdirs, files in os.walk(root):
            all_subdirs.update(norm_join(root, dirname, d) for d in subdirs)
            all_files.update(norm_join(root, dirname, f) for f in files)
        # If all the files we found are in our remaining set of files to
        # remove, then remove them from the latter set and add a wildcard
        # for the directory.
        if not (all_files - remaining):
            remaining.difference_update(all_files)
            wildcards.add(root + os.sep)

    return set(map(case_map.__getitem__, remaining)) | wildcards


def _script_names(script_name: str, is_gui: bool) -> Iterable[str]:
    yield script_name
    if os.name == "nt":
        yield script_name + ".exe"
        yield script_name + ".exe.manifest"
        if is_gui:
            yield script_name + "-script.pyw"
        else:
            yield script_name + "-script.py"


def _cache_file_from_source(py_file: str) -> Iterable[str]:
    py2_cache = py_file[:-3] + ".pyc"
    if os.path.isfile(py2_cache):
        yield py2_cache
    parent, base = os.path.split(py_file)
    cache_dir = os.path.join(parent, "__pycache__")
    for path in glob.glob(os.path.join(cache_dir, base[:-3] + ".*.pyc")):
        yield path


def _get_file_root(path: str, base: str) -> str | None:
    try:
        rel_path = Path(path).relative_to(base)
    except ValueError:
        return None
    else:
        root = rel_path.parts[0] if len(rel_path.parts) > 1 else ""
        return os.path.normcase(os.path.join(base, root))


class BaseRemovePaths(abc.ABC):
    """A collection of paths and/or pth entries to remove"""

    def __init__(
        self, dist: pkg_resources.Distribution, envrionment: Environment
    ) -> None:
        self.dist = dist
        self.envrionment = envrionment
        self._paths: set[str] = set()
        self._pth_entries: set[str] = set()

    @abc.abstractmethod
    def remove(self) -> None:
        """Remove the files"""

    def commit(self) -> None:
        """Commit the removal"""

    def rollback(self) -> None:
        """Roll back the removal operations"""

    @classmethod
    def from_dist(
        cls: Type[_T], dist: pkg_resources.Distribution, envrionment: Environment
    ) -> _T:
        """Create an instance from the distribution"""
        scheme = envrionment.get_paths()
        instance = cls(dist, envrionment)
        if is_dist_editable(dist):
            egg_link_path = get_egg_link_path(dist, scheme["purelib"])
            if not egg_link_path:
                termui.logger.warn(
                    "No egg link is found for editable distribution %s, do nothing.",
                    dist.project_name,
                )
            else:
                link_pointer = os.path.normcase(next(open(egg_link_path)).strip())
                if link_pointer != dist.location:
                    raise UninstallError(
                        f"The link pointer in {egg_link_path} doesn't match "
                        f"the location of {dist.project_name}(at {dist.location}"
                    )
                instance.add_path(egg_link_path)
                instance.add_pth(link_pointer)
        else:
            records = csv.reader(dist.get_metadata_lines("RECORD"))
            for filename, *_ in records:
                location = os.path.join(dist.location, filename)
                instance.add_path(location)
                bare_name, ext = os.path.splitext(location)
                if ext == ".py":
                    # .pyc files are added by add_path()
                    instance.add_path(bare_name + ".pyo")

        bin_dir = scheme["scripts"]
        if dist.has_metadata("scripts") and dist.metadata_isdir("scripts"):
            for script in dist.metadata_listdir("scripts"):
                instance.add_path(os.path.join(bin_dir, script))
                if os.name == "nt":
                    instance.add_path(os.path.join(bin_dir, script) + ".bat")

        # find console_scripts
        _scripts_to_remove: list[str] = []
        console_scripts = cast(dict, dist.get_entry_map(group="console_scripts"))
        for name in console_scripts:
            _scripts_to_remove.extend(_script_names(name, False))
        # find gui_scripts
        gui_scripts = dist.get_entry_map(group="gui_scripts")
        for name in gui_scripts:
            _scripts_to_remove.extend(_script_names(name, True))

        for s in _scripts_to_remove:
            instance.add_path(os.path.join(bin_dir, s))
        return instance

    def add_pth(self, line: str) -> None:
        self._pth_entries.add(line)

    def add_path(self, path: str) -> None:
        path = os.path.normcase(os.path.expanduser(os.path.abspath(path)))
        self._paths.add(path)
        if path.endswith(".py"):
            self._paths.update(_cache_file_from_source(path))
        elif os.path.basename(path) == "REFER_TO":
            line = open(path, "rb").readline().decode().strip()
            if line:
                self.refer_to = line


class StashedRemovePaths(BaseRemovePaths):
    """Stash the paths to temporarily location and remove them after commit"""

    PTH_REGISTRY = "easy-install.pth"

    def __init__(
        self, dist: pkg_resources.Distribution, environment: Environment
    ) -> None:
        super().__init__(dist, environment)
        self._pth_file = os.path.join(
            self.envrionment.get_paths()["purelib"], self.PTH_REGISTRY
        )
        self._saved_pth: bytes | None = None
        self._stashed: list[tuple[str, str]] = []
        self._tempdirs: dict[str, TemporaryDirectory] = {}

    def remove(self) -> None:
        self._remove_pth()
        self._stash_files()

    def _remove_pth(self) -> None:
        if not self._pth_entries:
            return
        self._saved_pth = open(self._pth_file, "rb").read()
        endline = "\r\n" if b"\r\n" in self._saved_pth else "\n"
        lines = self._saved_pth.decode().splitlines()
        for item in self._pth_entries:
            termui.logger.debug("Removing pth entry: %s", item)
            lines.remove(item)
        with open(self._pth_file, "wb") as f:
            f.write((endline.join(lines) + endline).encode("utf8"))

    def _stash_files(self) -> None:
        paths_to_rename = compress_for_rename(self._paths)

        for old_path in paths_to_rename:
            if not os.path.exists(old_path):
                continue
            is_dir = os.path.isdir(old_path) and not os.path.islink(old_path)
            termui.logger.debug(
                "Removing %s %s", "directory" if is_dir else "file", old_path
            )
            if old_path.endswith(".pyc"):
                # Don't stash cache files, remove them directly
                os.unlink(old_path)
            root = _get_file_root(
                old_path, os.path.abspath(self.envrionment.get_paths()["prefix"])
            )
            if root is None:
                termui.logger.debug(
                    "File path %s is not under packages root, skip", old_path
                )
                continue
            if root not in self._tempdirs:
                self._tempdirs[root] = TemporaryDirectory("-uninstall", "pdm-")
            new_root = self._tempdirs[root].name
            relpath = os.path.relpath(old_path, root)
            new_path = os.path.join(new_root, relpath)
            if is_dir and os.path.isdir(new_path):
                os.rmdir(new_path)
            renames(old_path, new_path)
            self._stashed.append((old_path, new_path))

    def commit(self) -> None:
        for tempdir in self._tempdirs.values():
            try:
                tempdir.cleanup()
            except FileNotFoundError:
                pass
        self._tempdirs.clear()
        self._stashed.clear()
        self._saved_pth = None
        refer_to = getattr(self, "refer_to", None)
        if refer_to:
            termui.logger.debug("Unlink from cached package %s", refer_to)
            CachedPackage(refer_to).remove_referrer(os.path.dirname(refer_to))

    def rollback(self) -> None:
        if not self._stashed:
            termui.logger.error("Can't rollback, not uninstalled yet")
            return
        if self._saved_pth is not None:
            with open(self._pth_file, "wb") as f:
                f.write(self._saved_pth)
        for old_path, new_path in self._stashed:
            termui.logger.debug("Rollback %s\n from %s", old_path, new_path)
            if os.path.isfile(old_path) or os.path.islink(old_path):
                os.unlink(old_path)
            elif os.path.isdir(old_path):
                shutil.rmtree(old_path)
            renames(new_path, old_path)
        self.commit()
