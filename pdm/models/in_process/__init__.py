"""
A collection of functions that need to be called via a subprocess call.
"""
import functools
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

FOLDER_PATH = Path(__file__).parent


@functools.lru_cache()
def get_python_abi_tag(executable: str):
    script = str(FOLDER_PATH / "get_abi_tag.py")
    return json.loads(subprocess.check_output(args=[executable, "-Es", script]))


@functools.lru_cache()
def get_python_version(
    executable: str, as_string: bool = False, digits: int = 3
) -> Tuple[Union[Tuple[int, ...], str], bool]:
    """Get the version of the Python interperter.

    :param executable: The path of the python executable
    :param as_string: return the version string if set to True
        and version tuple otherwise
    :param digits: the number of version parts to be returned
    :returns: A tuple of (version, is_64bit)
    """
    args = [
        executable,
        "-Esc",
        "import sys,json;print"
        f"(json.dumps([sys.version_info[:{digits}], sys.maxsize > 2 ** 32]))",
    ]
    result, is_64bit = json.loads(subprocess.check_output(args))
    if not as_string:
        return tuple(result), is_64bit
    return ".".join(map(str, result)), is_64bit


def get_sys_config_paths(
    executable: str, vars: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    """Return the sys_config.get_paths() result for the python interpreter"""
    if not vars:
        args = [
            executable,
            "-Esc",
            "import sysconfig,json;print(json.dumps(sysconfig.get_paths()))",
        ]
        return json.loads(subprocess.check_output(args))
    else:
        env = os.environ.copy()
        env.update(SYSCONFIG_VARS=json.dumps(vars))
        args = [
            executable,
            "-Esc",
            "import os,sysconfig,json;print(json.dumps(sysconfig."
            "get_paths(vars=json.loads(os.getenv('SYSCONFIG_VARS')))))",
        ]
        return json.loads(subprocess.check_output(args, env=env))


def get_pep508_environment(executable: str) -> Dict[str, Any]:
    """Get PEP 508 environment markers dict."""
    script = str(FOLDER_PATH / "pep508.py")
    args = [executable, "-Es", script]
    return json.loads(subprocess.check_output(args))
