"""A build backend which deliberately violates the PEP 517 / PEP 660 hook
return contracts, controlled through ``config_settings["invalid"]``.

The kind of violation is selected with these values:

- ``wrong_type``: return a value of the wrong type (e.g. a string instead of
  a requirements list, or an integer instead of an artifact basename)
- ``bad_member``: return a requirements list containing a non-string item
- ``empty``: return an empty string as an artifact basename
- ``wrong_suffix``: return an artifact name with the wrong suffix
- ``not_basename``: return a name containing path separators
- ``missing``: return a valid-looking name without creating the artifact

Without an ``invalid`` setting, the hooks behave conformingly.

This backend exists only to test optional hook return validation. Don't use
this for any real code.
"""
import os
import tarfile
from os.path import join as pjoin
from zipfile import ZipFile

WHEEL_NAME = "invalid_pkg-0.1-py3-none-any.whl"
SDIST_NAME = "invalid_pkg-0.1.tar.gz"
DIST_INFO_NAME = "invalid_pkg-0.1.dist-info"


def _invalid_mode(config_settings):
    return (config_settings or {}).get("invalid")


def get_requires_for_build_wheel(config_settings):
    mode = _invalid_mode(config_settings)
    if mode == "wrong_type":
        return "not-a-list"
    if mode == "bad_member":
        return ["wheelwright", 3.14]
    return ["wheelwright"]


get_requires_for_build_editable = get_requires_for_build_wheel


def get_requires_for_build_sdist(config_settings):
    mode = _invalid_mode(config_settings)
    if mode == "wrong_type":
        return 42
    if mode == "bad_member":
        return ["frog", None]
    return ["frog"]


def prepare_metadata_for_build_wheel(metadata_directory, config_settings):
    mode = _invalid_mode(config_settings)
    if mode == "wrong_type":
        return 7
    if mode == "empty":
        return ""
    if mode == "wrong_suffix":
        return "invalid_pkg-0.1.dist-info2"
    if mode == "not_basename":
        return os.path.join("..", DIST_INFO_NAME)
    if mode == "missing":
        return "ghost-0.1.dist-info"
    os.mkdir(pjoin(metadata_directory, DIST_INFO_NAME))
    return DIST_INFO_NAME


prepare_metadata_for_build_editable = prepare_metadata_for_build_wheel


def build_wheel(wheel_directory, config_settings, metadata_directory=None):
    mode = _invalid_mode(config_settings)
    if mode == "wrong_type":
        return 42
    if mode == "empty":
        return ""
    if mode == "wrong_suffix":
        return "invalid_pkg-0.1.zip"
    if mode == "not_basename":
        return os.path.join("dist", WHEEL_NAME)
    if mode == "missing":
        return "ghost-0.1-py3-none-any.whl"
    with ZipFile(pjoin(wheel_directory, WHEEL_NAME), "w") as zf:
        zf.writestr("invalid_pkg.py", "# test package")
    return WHEEL_NAME


build_editable = build_wheel


def build_sdist(sdist_directory, config_settings):
    mode = _invalid_mode(config_settings)
    if mode == "wrong_type":
        return 42
    if mode == "empty":
        return ""
    if mode == "wrong_suffix":
        return "invalid_pkg-0.1.zip"
    if mode == "not_basename":
        return os.path.join("..", SDIST_NAME)
    if mode == "missing":
        return "ghost-0.1.tar.gz"
    with tarfile.open(pjoin(sdist_directory, SDIST_NAME), "w:gz"):
        pass
    return SDIST_NAME
