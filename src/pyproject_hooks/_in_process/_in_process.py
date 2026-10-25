"""This is invoked in a subprocess to call the build backend hooks.

It expects:
- Command line args: hook_name, control_dir
- Environment variables:
      _PYPROJECT_HOOKS_BUILD_BACKEND=entry.point:spec
      _PYPROJECT_HOOKS_BACKEND_PATH_JSON=paths (JSON encoded)
- control_dir/input.json:
  - {"kwargs": {...}}

Results:
- control_dir/output.json
  - {"return_val": ...}

When the frontend enables request/response correlation checks, input.json
additionally carries a "protocol" version, a unique "request_id" and the
request is answered with matching "protocol", "request_id" and
"request_digest" fields in output.json:
  - {"kwargs": {...}, "protocol": 1, "request_id": "..."}
  - {"return_val": ..., "protocol": 1, "request_id": "...",
     "request_digest": "..."}
"""
import hashlib
import json
import os
import os.path
import re
import shutil
import sys
import traceback
import uuid
from glob import glob
from importlib import import_module
from importlib.machinery import PathFinder
from os.path import join as pjoin
import warnings

# Must match CORRELATION_PROTOCOL_VERSION in pyproject_hooks._impl.
CORRELATION_PROTOCOL_VERSION = 1

# This file is run as a script, and `import wrappers` is not zip-safe, so we
# include write_json() from wrappers.py.


def write_json(obj, path, **kwargs):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, **kwargs)


def _reject_duplicate_keys(pairs):
    """object_pairs_hook turning duplicate JSON keys into a ValueError."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: {!r}".format(key))
        result[key] = value
    return result


def canonical_json_digest(obj):
    """SHA-256 digest of a canonical JSON encoding.

    Dict key order is irrelevant, list order and explicit nulls are not.
    Kept byte-for-byte compatible with the copy in pyproject_hooks._impl.
    """
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_digest(hook_name, kwargs):
    return canonical_json_digest({"hook": hook_name, "kwargs": kwargs})


def _reject_request(message):
    """Refuse a malformed correlated request, without importing the backend."""
    sys.stderr.write(
        "pyproject-hooks: refusing to run hook, invalid correlated request: "
        + message
        + "\n"
    )
    raise SystemExit(1)


class BackendUnavailable(Exception):
    """Raised if we cannot import the backend"""

    def __init__(self, message, traceback=None):
        super().__init__(message)
        self.message = message
        self.traceback = traceback


class HookMissing(Exception):
    """Raised if a hook is missing and we are not executing the fallback"""

    def __init__(self, hook_name=None):
        super().__init__(hook_name)
        self.hook_name = hook_name


def _build_backend():
    """Find and load the build backend"""
    backend_path_json = os.environ.get("_PYPROJECT_HOOKS_BACKEND_PATH_JSON")
    ep = os.environ["_PYPROJECT_HOOKS_BUILD_BACKEND"]
    mod_path, _, obj_path = ep.partition(":")

    if backend_path_json:
        extra_pathitems = json.loads(backend_path_json)
    else:
        extra_pathitems = []

    if extra_pathitems:
        # Ensure in-tree backend directories have the highest priority when importing.
        sys.meta_path.insert(0, _BackendPathFinder(extra_pathitems, mod_path))

    try:
        obj = import_module(mod_path)
    except ImportError:
        msg = f"Cannot import {mod_path!r}"
        raise BackendUnavailable(msg, traceback.format_exc())

    if obj_path:
        for path_part in obj_path.split("."):
            obj = getattr(obj, path_part)
    return obj


class _BackendPathFinder:
    """Implements the MetaPathFinder interface to locate modules in ``backend-path``.

    Since the environment provided by the frontend can contain all sorts of
    MetaPathFinders, the only way to ensure the backend is loaded from the
    right place is to prepend our own.
    """

    def __init__(self, backend_path, backend_module):
        self.backend_path = backend_path
        self.backend_module = backend_module
        self.backend_parent, _, _ = backend_module.partition(".")

    def find_spec(self, fullname, _path, _target=None):
        if "." in fullname:
            # Rely on importlib to find nested modules based on parent's path
            return None

        # Ignore other items in _path or sys.path and use backend_path instead:
        spec = PathFinder.find_spec(fullname, path=self.backend_path)
        if spec is None and fullname == self.backend_parent:
            # According to the spec, the backend MUST be loaded from backend-path.
            # Therefore, we can halt the import machinery and raise a clean error.
            msg = f"Cannot find module {self.backend_module!r} in {self.backend_path!r}"
            raise BackendUnavailable(msg)

        return spec

    if sys.version_info >= (3, 8):

        def find_distributions(self, context=None):
            # Delayed import: Python 3.7 does not contain importlib.metadata
            from importlib.metadata import DistributionFinder, MetadataPathFinder

            name = context.name if context else None
            context = DistributionFinder.Context(name=name, path=self.backend_path)
            return MetadataPathFinder.find_distributions(context=context)


def _supported_features():
    """Return the list of options features supported by the backend.

    Returns a list of strings.
    The only possible value is 'build_editable'.
    """
    backend = _build_backend()
    features = []
    if hasattr(backend, "build_editable"):
        features.append("build_editable")
    return features


def get_requires_for_build_wheel(config_settings):
    """Invoke the optional get_requires_for_build_wheel hook

    Returns [] if the hook is not defined.
    """
    backend = _build_backend()
    try:
        hook = backend.get_requires_for_build_wheel
    except AttributeError:
        return []
    else:
        return hook(config_settings)


def get_requires_for_build_editable(config_settings):
    """Invoke the optional get_requires_for_build_editable hook

    Returns [] if the hook is not defined.
    """
    backend = _build_backend()
    try:
        hook = backend.get_requires_for_build_editable
    except AttributeError:
        return []
    else:
        return hook(config_settings)


def prepare_metadata_for_build_wheel(
    metadata_directory, config_settings, _allow_fallback
):
    """Invoke optional prepare_metadata_for_build_wheel

    Implements a fallback by building a wheel if the hook isn't defined,
    unless _allow_fallback is False in which case HookMissing is raised.
    """
    backend = _build_backend()
    try:
        hook = backend.prepare_metadata_for_build_wheel
    except AttributeError:
        if not _allow_fallback:
            raise HookMissing()
    else:
        return hook(metadata_directory, config_settings)
    # fallback to build_wheel outside the try block to avoid exception chaining
    # which can be confusing to users and is not relevant
    whl_basename = backend.build_wheel(metadata_directory, config_settings)
    return _get_wheel_metadata_from_wheel(
        whl_basename, metadata_directory, config_settings
    )


def prepare_metadata_for_build_editable(
    metadata_directory, config_settings, _allow_fallback
):
    """Invoke optional prepare_metadata_for_build_editable

    Implements a fallback by building an editable wheel if the hook isn't
    defined, unless _allow_fallback is False in which case HookMissing is
    raised.
    """
    backend = _build_backend()
    try:
        hook = backend.prepare_metadata_for_build_editable
    except AttributeError:
        if not _allow_fallback:
            raise HookMissing()
        try:
            build_hook = backend.build_editable
        except AttributeError:
            raise HookMissing(hook_name="build_editable")
        else:
            whl_basename = build_hook(metadata_directory, config_settings)
            return _get_wheel_metadata_from_wheel(
                whl_basename, metadata_directory, config_settings
            )
    else:
        return hook(metadata_directory, config_settings)


WHEEL_BUILT_MARKER = "PYPROJECT_HOOKS_ALREADY_BUILT_WHEEL"


def _dist_info_files(whl_zip):
    """Identify the .dist-info folder inside a wheel ZipFile."""
    res = []
    for path in whl_zip.namelist():
        m = re.match(r"[^/\\]+-[^/\\]+\.dist-info/", path)
        if m:
            res.append(path)
    if res:
        return res
    raise Exception("No .dist-info folder found in wheel")


def _get_wheel_metadata_from_wheel(whl_basename, metadata_directory, config_settings):
    """Extract the metadata from a wheel.

    Fallback for when the build backend does not
    define the 'get_wheel_metadata' hook.
    """
    from zipfile import ZipFile

    with open(os.path.join(metadata_directory, WHEEL_BUILT_MARKER), "wb"):
        pass  # Touch marker file

    whl_file = os.path.join(metadata_directory, whl_basename)
    with ZipFile(whl_file) as zipf:
        dist_info = _dist_info_files(zipf)
        zipf.extractall(path=metadata_directory, members=dist_info)
    return dist_info[0].split("/")[0]


def _find_already_built_wheel(metadata_directory):
    """Check for a wheel already built during the get_wheel_metadata hook."""
    if not metadata_directory:
        return None
    metadata_parent = os.path.dirname(metadata_directory)
    if not os.path.isfile(pjoin(metadata_parent, WHEEL_BUILT_MARKER)):
        return None

    whl_files = glob(os.path.join(metadata_parent, "*.whl"))
    if not whl_files:
        print("Found wheel built marker, but no .whl files")
        return None
    if len(whl_files) > 1:
        print(
            "Found multiple .whl files; unspecified behaviour. "
            "Will call build_wheel."
        )
        return None

    # Exactly one .whl file
    return whl_files[0]


def build_wheel(wheel_directory, config_settings, metadata_directory=None):
    """Invoke the mandatory build_wheel hook.

    If a wheel was already built in the
    prepare_metadata_for_build_wheel fallback, this
    will copy it rather than rebuilding the wheel.
    """
    prebuilt_whl = _find_already_built_wheel(metadata_directory)
    if prebuilt_whl:
        shutil.copy2(prebuilt_whl, wheel_directory)
        return os.path.basename(prebuilt_whl)

    return _build_backend().build_wheel(
        wheel_directory, config_settings, metadata_directory
    )


def build_editable(wheel_directory, config_settings, metadata_directory=None):
    """Invoke the optional build_editable hook.

    If a wheel was already built in the
    prepare_metadata_for_build_editable fallback, this
    will copy it rather than rebuilding the wheel.
    """
    backend = _build_backend()
    try:
        hook = backend.build_editable
    except AttributeError:
        raise HookMissing()
    else:
        prebuilt_whl = _find_already_built_wheel(metadata_directory)
        if prebuilt_whl:
            shutil.copy2(prebuilt_whl, wheel_directory)
            return os.path.basename(prebuilt_whl)

        return hook(wheel_directory, config_settings, metadata_directory)


def get_requires_for_build_sdist(config_settings):
    """Invoke the optional get_requires_for_build_wheel hook

    Returns [] if the hook is not defined.
    """
    backend = _build_backend()
    try:
        hook = backend.get_requires_for_build_sdist
    except AttributeError:
        return []
    else:
        return hook(config_settings)


class _DummyException(Exception):
    """Nothing should ever raise this exception"""


class GotUnsupportedOperation(Exception):
    """For internal use when backend raises UnsupportedOperation"""

    def __init__(self, traceback):
        self.traceback = traceback


def build_sdist(sdist_directory, config_settings):
    """Invoke the mandatory build_sdist hook."""
    backend = _build_backend()
    try:
        return backend.build_sdist(sdist_directory, config_settings)
    except getattr(backend, "UnsupportedOperation", _DummyException):
        raise GotUnsupportedOperation(traceback.format_exc())


HOOK_NAMES = {
    "get_requires_for_build_wheel",
    "prepare_metadata_for_build_wheel",
    "build_wheel",
    "get_requires_for_build_editable",
    "prepare_metadata_for_build_editable",
    "build_editable",
    "get_requires_for_build_sdist",
    "build_sdist",
    "_supported_features",
}


def _validate_correlated_input(hook_input):
    """Validate a correlated request before the build backend is imported."""
    if not isinstance(hook_input, dict):
        _reject_request("input.json must contain a JSON object")

    keys = set(hook_input)
    required = {"kwargs", "protocol", "request_id"}
    missing = required - keys
    if missing:
        _reject_request("missing field(s): {}".format(sorted(missing)))
    extra = keys - required
    if extra:
        _reject_request("unexpected field(s): {}".format(sorted(extra)))

    protocol = hook_input["protocol"]
    # bool is a subclass of int; reject it explicitly.
    if type(protocol) is not int:
        _reject_request("the protocol version must be an integer")
    if protocol != CORRELATION_PROTOCOL_VERSION:
        _reject_request(
            "unsupported protocol version {}; this hook process supports "
            "version {}".format(protocol, CORRELATION_PROTOCOL_VERSION)
        )

    request_id = hook_input["request_id"]
    if not isinstance(request_id, str) or not request_id:
        _reject_request("the request id must be a non-empty string")
    try:
        uuid.UUID(request_id)
    except ValueError:
        _reject_request("the request id must be a valid UUID: {!r}".format(request_id))

    if not isinstance(hook_input["kwargs"], dict):
        _reject_request("the kwargs field must be a JSON object")


def main():
    if len(sys.argv) < 3:
        sys.exit("Needs args: hook_name, control_dir")
    hook_name = sys.argv[1]
    control_dir = sys.argv[2]
    if hook_name not in HOOK_NAMES:
        sys.exit("Unknown hook: %s" % hook_name)

    # Remove the parent directory from sys.path to avoid polluting the backend
    # import namespace with this directory.
    here = os.path.normcase(os.path.realpath(os.path.dirname(__file__)))
    sys.path[:] = [
        path for path in sys.path if os.path.normcase(os.path.realpath(path)) != here
    ]

    hook = globals()[hook_name]

    # The presence of the "protocol" field marks a correlated request from a
    # frontend that opted into the checks; a plain {"kwargs": ...} document is
    # handled exactly as before.
    input_path = pjoin(control_dir, "input.json")
    try:
        with open(input_path, encoding="utf-8") as f:
            raw_input_text = f.read()
    except OSError as e:
        # Even a plain request needs its input, but a missing or unreadable
        # file must never lead to importing the backend.
        sys.exit("Cannot read input.json: {}".format(e))

    hook_input = json.loads(raw_input_text)
    correlated = isinstance(hook_input, dict) and "protocol" in hook_input
    if correlated:
        # Parse the same text strictly (rejecting duplicate keys) and
        # validate *before* importing the backend.
        try:
            hook_input = json.loads(
                raw_input_text, object_pairs_hook=_reject_duplicate_keys
            )
        except ValueError as e:
            _reject_request(str(e))
        _validate_correlated_input(hook_input)
        request_id = hook_input["request_id"]
        request_digest = _request_digest(hook_name, hook_input["kwargs"])

    with warnings.catch_warnings(record=True) as captured_warnings:
        json_out = {"unsupported": False, "return_val": None}
        try:
            json_out["return_val"] = hook(**hook_input["kwargs"])
        except BackendUnavailable as e:
            json_out["no_backend"] = True
            json_out["traceback"] = e.traceback
            json_out["backend_error"] = e.message
        except GotUnsupportedOperation as e:
            json_out["unsupported"] = True
            json_out["traceback"] = e.traceback
        except HookMissing as e:
            json_out["hook_missing"] = True
            json_out["missing_hook_name"] = e.hook_name or hook_name

    json_out["warnings"] = [
        {
            "message": str(w.message),
            "filename": w.filename,
            "lineno": w.lineno,
        }
        for w in captured_warnings
        if isinstance(w.category, type) and issubclass(w.category, UserWarning)
    ]

    if correlated:
        # Echoed on every outcome (success, missing backend, unsupported
        # operation, missing hook) so the frontend can correlate errors too.
        json_out["protocol"] = CORRELATION_PROTOCOL_VERSION
        json_out["request_id"] = request_id
        json_out["request_digest"] = request_digest

    write_json(json_out, pjoin(control_dir, "output.json"), indent=2)


if __name__ == "__main__":
    main()
