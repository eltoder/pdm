from __future__ import annotations

import io
import os
from pathlib import Path
from typing import TYPE_CHECKING

from installer import __version__, install
from installer._core import _determine_scheme, _process_WHEEL_file
from installer.destinations import SchemeDictionaryDestination
from installer.exceptions import InvalidWheelSource
from installer.records import RecordEntry
from installer.sources import WheelFile as _WheelFile
from installer.utils import parse_entrypoints

from pdm.installers.packages import CachedPackage
from pdm.models.candidates import Candidate
from pdm.models.environment import Environment
from pdm.termui import logger
from pdm.utils import cached_property, normalize_name

if TYPE_CHECKING:
    from typing import BinaryIO

    from installer.destinations import Scheme


class WheelFile(_WheelFile):
    @cached_property
    def dist_info_dir(self) -> str:
        namelist = self._zipfile.namelist()
        try:
            return next(
                name.split("/")[0]
                for name in namelist
                if name.split("/")[0].endswith(".dist-info")
            )
        except StopIteration:  # pragma: no cover
            canonical_name = super().dist_info_dir
            raise InvalidWheelSource(
                f"The wheel doesn't contain metadata {canonical_name!r}"
            )


class InstallDestination(SchemeDictionaryDestination):
    def write_to_fs(
        self, scheme: Scheme, path: str | Path, stream: BinaryIO
    ) -> RecordEntry:
        target_path = Path(self.scheme_dict[scheme], path)
        if target_path.exists():
            target_path.unlink()
        return super().write_to_fs(scheme, path, stream)


def install_wheel(candidate: Candidate, scheme: dict[str, str] | None = None) -> None:
    """Install a normal wheel file into the environment.
    Optional install scheme can be given to change the destination.
    """
    wheel = candidate.build()
    env = candidate.environment

    destination = InstallDestination(
        scheme or env.get_paths(),
        interpreter=env.interpreter.executable,
        script_kind=_get_kind(env),
    )

    with WheelFile.open(wheel) as source:
        install(
            source=source,
            destination=destination,
            # Additional metadata that is generated by the installation tool.
            additional_metadata={
                "INSTALLER": f"installer {__version__}".encode(),
            },
        )


def _get_kind(environment: Environment) -> str:
    if os.name != "nt":
        return "posix"
    is_32bit = environment.interpreter.is_32bit
    # TODO: support win arm64
    if is_32bit:
        return "win-ia32"
    else:
        return "win-amd64"


def install_editable(
    candidate: Candidate, scheme: dict[str, str] | None = None
) -> None:
    """Install package in editable mode using the legacy `python setup.py develop`"""
    # TODO: PEP 660
    from pdm.builders import EditableBuilder

    candidate.prepare()
    env = candidate.environment
    assert candidate.source_dir
    builder = EditableBuilder(candidate.source_dir, env)
    setup_path = builder.ensure_setup_py()
    paths = scheme or env.get_paths()
    install_script = Path(__file__).with_name("_editable_install.py")
    install_args = [
        env.interpreter.executable,
        "-u",
        str(install_script),
        setup_path,
        paths["prefix"],
        paths["purelib"],
        paths["scripts"],
    ]
    builder.install(["setuptools"])
    builder.subprocess_runner(install_args, candidate.source_dir)


def install_wheel_with_cache(
    candidate: Candidate, scheme: dict[str, str] | None = None
) -> None:
    """Only create .pth files referring to the cached package.
    If the cache doesn't exist, create one.
    """
    wheel = candidate.build()
    wheel_stem = Path(wheel).stem
    cache_path = candidate.environment.project.cache("packages") / wheel_stem
    package_cache = CachedPackage(cache_path)
    if not cache_path.is_dir():
        logger.debug("Installing wheel into cached location %s", cache_path)
        cache_path.mkdir(exist_ok=True)
        install_wheel(candidate, package_cache.scheme())
    _install_from_cache(candidate, package_cache)


def _install_from_cache(candidate: Candidate, package_cache: CachedPackage) -> None:
    env = candidate.environment
    destination = InstallDestination(
        env.get_paths(),
        interpreter=env.interpreter.executable,
        script_kind=_get_kind(env),
    )

    with WheelFile.open(candidate.wheel) as source:
        root_scheme = _process_WHEEL_file(source)

        # RECORD handling
        record_file_path = os.path.join(source.dist_info_dir, "RECORD")
        written_records = []

        # console-scripts and gui-scripts are copied anyway.
        if "entry_points.txt" in source.dist_info_filenames:
            entrypoints_text = source.read_dist_info("entry_points.txt")
            for name, module, attr, section in parse_entrypoints(entrypoints_text):
                record = destination.write_script(
                    name=name,
                    module=module,
                    attr=attr,
                    section=section,
                )

        for record_elements, stream in source.get_contents():
            source_record = RecordEntry.from_elements(*record_elements)
            path = source_record.path
            # Only copy .pth files and metadata_dir in this mode
            if path == record_file_path or not (
                path.endswith(".pth") or path.startswith(source.dist_info_dir)
            ):
                continue

            # Figure out where to write this file.
            scheme, destination_path = _determine_scheme(
                path=path,
                source=source,
                root_scheme=root_scheme,
            )
            record = destination.write_file(
                scheme=scheme,
                path=destination_path,
                stream=stream,
            )
            written_records.append(record)

        # Write .pth file
        lib_path = package_cache.scheme()["purelib"]
        record = destination.write_file(
            scheme=root_scheme,
            path=f"{normalize_name(candidate.name)}.pth",  # type: ignore
            stream=io.BytesIO(f"{lib_path}\n".encode()),
        )
        written_records.append(record)

        # Write all the installation-specific metadata
        additional_metadata = {
            "INSTALLER": f"installer {__version__}".encode(),
            "REFER_TO": package_cache.path.as_posix().encode(),
        }
        for filename, contents in additional_metadata.items():
            path = os.path.join(source.dist_info_dir, filename)

            with io.BytesIO(contents) as other_stream:
                record = destination.write_file(
                    scheme=root_scheme,
                    path=path,
                    stream=other_stream,
                )
            written_records.append(record)

        written_records.append(RecordEntry(record_file_path, None, None))
        destination.finalize_installation(
            scheme=root_scheme,
            record_file_path=record_file_path,
            records=written_records,
        )
        package_cache.add_referrer(source.dist_info_dir)
