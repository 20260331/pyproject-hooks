import hashlib
import json
import os
import sys
import tempfile
import subprocess
import uuid
from contextlib import contextmanager
from os.path import abspath
from os.path import join as pjoin
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Optional, Sequence
import warnings

from ._in_process import _in_proc_script_path

if TYPE_CHECKING:
    from typing import Protocol

    class SubprocessRunner(Protocol):
        """A protocol for the subprocess runner."""

        def __call__(
            self,
            cmd: Sequence[str],
            cwd: Optional[str] = None,
            extra_environ: Optional[Mapping[str, str]] = None,
        ) -> None:
            ...


#: Version of the optional request/response correlation protocol.
#:
#: When correlation checks are enabled, the frontend puts this in
#: ``input.json`` and requires it to be echoed in ``output.json``. Bump this
#: whenever the structure of either JSON file changes.
CORRELATION_PROTOCOL_VERSION = 1

#: Keys every correlated ``output.json`` must contain.
_REQUIRED_RESPONSE_KEYS = frozenset(
    {
        "protocol",
        "request_id",
        "request_digest",
        "return_val",
        "unsupported",
        "warnings",
    }
)

#: Keys which a correlated ``output.json`` is allowed to contain in addition to
#: the required ones.
_KNOWN_RESPONSE_KEYS = _REQUIRED_RESPONSE_KEYS | {
    "no_backend",
    "traceback",
    "backend_error",
    "hook_missing",
    "missing_hook_name",
}


def write_json(obj: Mapping[str, Any], path: str, **kwargs) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, **kwargs)


def read_json(path: str) -> Mapping[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _reject_duplicate_keys(pairs: Sequence[tuple]) -> dict:
    """``object_pairs_hook`` turning duplicate JSON keys into a ``ValueError``."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def read_json_strict(path: str) -> Mapping[str, Any]:
    """Read JSON, rejecting duplicate object keys anywhere in the document."""
    with open(path, encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=_reject_duplicate_keys)


def canonical_json_digest(obj: Any) -> str:
    """Return a SHA-256 digest of a canonical JSON encoding of ``obj``.

    Mappings compare equal regardless of their key order, while the order of
    sequence items is significant and an explicit ``None`` is never treated as
    a missing value.
    """
    canonical = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_digest(hook_name: str, kwargs: Mapping[str, Any]) -> str:
    """Digest identifying the hook invoked and the arguments it was given."""
    return canonical_json_digest({"hook": hook_name, "kwargs": kwargs})


class BuildBackendWarning(UserWarning):
    """Will be emitted for every UserWarning emitted by the hook process."""


class BackendUnavailable(Exception):
    """Will be raised if the backend cannot be imported in the hook process."""

    def __init__(
        self,
        traceback: str,
        message: Optional[str] = None,
        backend_name: Optional[str] = None,
        backend_path: Optional[Sequence[str]] = None,
    ) -> None:
        # Preserving arg order for the sake of API backward compatibility.
        self.backend_name = backend_name
        self.backend_path = backend_path
        self.traceback = traceback or ""
        super().__init__(
            f"{message or 'Error while importing backend'}"
            + (f"\n\nTraceback:\n{self.traceback}" if self.traceback else "")
        )


class HookMissing(Exception):
    """Will be raised on missing hooks (if a fallback can't be used)."""

    def __init__(self, hook_name: str) -> None:
        super().__init__(hook_name)
        self.hook_name = hook_name


class UnsupportedOperation(Exception):
    """May be raised by build_sdist if the backend indicates that it can't."""

    def __init__(self, traceback: str) -> None:
        self.traceback = traceback


class CorrelationError(Exception):
    """Will be raised when a hook response cannot be correlated with its request.

    Only raised when the caller was created with ``verify_correlation=True``.
    It means the temporary JSON files exchanged with the hook subprocess do not
    prove that ``output.json`` answers this particular ``input.json``: the
    protocol version, the per-call identity or the normalised request content
    does not match (or the response is missing fields, contains duplicate
    fields or carries unknown fields). The return value and the warnings of
    such a response are not delivered.
    """


def default_subprocess_runner(
    cmd: Sequence[str],
    cwd: Optional[str] = None,
    extra_environ: Optional[Mapping[str, str]] = None,
) -> None:
    """The default method of calling the wrapper subprocess.

    This uses :func:`subprocess.run` under the hood.
    """
    env = os.environ.copy()
    if extra_environ:
        env.update(extra_environ)

    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def quiet_subprocess_runner(
    cmd: Sequence[str],
    cwd: Optional[str] = None,
    extra_environ: Optional[Mapping[str, str]] = None,
) -> None:
    """Call the subprocess while suppressing output.

    This uses :func:`subprocess.run` with `stdout=PIPE, stderr=STDOUT` under the hood.
    """
    env = os.environ.copy()
    if extra_environ:
        env.update(extra_environ)

    # Capturing output in case it fails (available on the exception object)
    subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )


def norm_and_check(source_tree: str, requested: str) -> str:
    """Normalise and check a backend path.

    Ensure that the requested backend path is specified as a relative path,
    and resolves to a location under the given source tree.

    Return an absolute version of the requested path.
    """
    if os.path.isabs(requested):
        raise ValueError("paths must be relative")

    abs_source = os.path.abspath(source_tree)
    abs_requested = os.path.normpath(os.path.join(abs_source, requested))
    norm_source = os.path.normcase(abs_source)
    norm_requested = os.path.normcase(abs_requested)
    if os.path.commonpath([norm_source, norm_requested]) != norm_source:
        raise ValueError("paths must be inside source tree")

    return abs_requested


class BuildBackendHookCaller:
    """A wrapper to call the build backend hooks for a source directory."""

    def __init__(
        self,
        source_dir: str,
        build_backend: str,
        backend_path: Optional[Sequence[str]] = None,
        runner: Optional["SubprocessRunner"] = None,
        python_executable: Optional[str] = None,
        verify_correlation: bool = False,
    ) -> None:
        """
        :param source_dir: The source directory to invoke the build backend for
        :param build_backend: The build backend spec
        :param backend_path: Additional path entries for the build backend spec
        :param runner: The :ref:`subprocess runner <Subprocess Runners>` to use
        :param python_executable:
            The Python executable used to invoke the build backend
        :param verify_correlation:
            Verify that the subprocess response answers this exact request.
            Off by default. When enabled, every request carries a protocol
            version, a unique per-call identifier and the digest of its
            normalised content, which the hook process must echo back. A
            response that is missing fields, repeats fields, answers a
            different call or certifies different request content is rejected
            with :exc:`CorrelationError` before its result or warnings are
            delivered. This protects against custom subprocess runners that
            replay or misdeliver stale responses, and requires the hook
            process to run a compatible version of ``pyproject_hooks``.
        """
        if runner is None:
            runner = default_subprocess_runner

        self.source_dir = abspath(source_dir)
        self.build_backend = build_backend
        if backend_path:
            backend_path = [norm_and_check(self.source_dir, p) for p in backend_path]
        self.backend_path = backend_path
        self._subprocess_runner = runner
        if not python_executable:
            python_executable = sys.executable
        self.python_executable = python_executable
        self.verify_correlation = verify_correlation

    @contextmanager
    def subprocess_runner(self, runner: "SubprocessRunner") -> Iterator[None]:
        """A context manager for temporarily overriding the default
        :ref:`subprocess runner <Subprocess Runners>`.

        :param runner: The new subprocess runner to use within the context.

        .. warning::

            This context manager temporarily mutates the hook caller instance and
            is not thread-safe. Callers that need to run hooks concurrently
            should create separate :class:`BuildBackendHookCaller` instances, or
            provide the subprocess runner when constructing the caller.

        .. code-block:: python

            hook_caller = BuildBackendHookCaller(...)
            with hook_caller.subprocess_runner(quiet_subprocess_runner):
                ...
        """
        prev = self._subprocess_runner
        self._subprocess_runner = runner
        try:
            yield
        finally:
            self._subprocess_runner = prev

    def _supported_features(self) -> Sequence[str]:
        """Return the list of optional features supported by the backend."""
        return self._call_hook("_supported_features", {})

    def get_requires_for_build_wheel(
        self,
        config_settings: Optional[Mapping[str, Any]] = None,
    ) -> Sequence[str]:
        """Get additional dependencies required for building a wheel.

        :param config_settings: The configuration settings for the build backend
        :returns: A list of :pep:`dependency specifiers <508>`.

        .. admonition:: Fallback

            If the build backend does not defined a hook with this name, an
            empty list will be returned.
        """
        return self._call_hook(
            "get_requires_for_build_wheel", {"config_settings": config_settings}
        )

    def prepare_metadata_for_build_wheel(
        self,
        metadata_directory: str,
        config_settings: Optional[Mapping[str, Any]] = None,
        _allow_fallback: bool = True,
    ) -> str:
        """Prepare a ``*.dist-info`` folder with metadata for this project.

        :param metadata_directory: The directory to write the metadata to
        :param config_settings: The configuration settings for the build backend
        :param _allow_fallback:
            Whether to allow the fallback to building a wheel and extracting
            the metadata from it. Should be passed as a keyword argument only.

        :returns: Name of the newly created subfolder within
                  ``metadata_directory``, containing the metadata.

        .. admonition:: Fallback

            If the build backend does not define a hook with this name and
            ``_allow_fallback`` is truthy, the backend will be asked to build a
            wheel via the ``build_wheel`` hook and the dist-info extracted from
            that will be returned.
        """
        return self._call_hook(
            "prepare_metadata_for_build_wheel",
            {
                "metadata_directory": abspath(metadata_directory),
                "config_settings": config_settings,
                "_allow_fallback": _allow_fallback,
            },
        )

    def build_wheel(
        self,
        wheel_directory: str,
        config_settings: Optional[Mapping[str, Any]] = None,
        metadata_directory: Optional[str] = None,
    ) -> str:
        """Build a wheel from this project.

        :param wheel_directory: The directory to write the wheel to
        :param config_settings: The configuration settings for the build backend
        :param metadata_directory: The directory to reuse existing metadata from
        :returns:
            The name of the newly created wheel within ``wheel_directory``.

        .. admonition:: Interaction with fallback

            If the ``build_wheel`` hook was called in the fallback for
            :meth:`prepare_metadata_for_build_wheel`, the build backend would
            not be invoked. Instead, the previously built wheel will be copied
            to ``wheel_directory`` and the name of that file will be returned.
        """
        if metadata_directory is not None:
            metadata_directory = abspath(metadata_directory)
        return self._call_hook(
            "build_wheel",
            {
                "wheel_directory": abspath(wheel_directory),
                "config_settings": config_settings,
                "metadata_directory": metadata_directory,
            },
        )

    def get_requires_for_build_editable(
        self,
        config_settings: Optional[Mapping[str, Any]] = None,
    ) -> Sequence[str]:
        """Get additional dependencies required for building an editable wheel.

        :param config_settings: The configuration settings for the build backend
        :returns: A list of :pep:`dependency specifiers <508>`.

        .. admonition:: Fallback

            If the build backend does not defined a hook with this name, an
            empty list will be returned.
        """
        return self._call_hook(
            "get_requires_for_build_editable", {"config_settings": config_settings}
        )

    def prepare_metadata_for_build_editable(
        self,
        metadata_directory: str,
        config_settings: Optional[Mapping[str, Any]] = None,
        _allow_fallback: bool = True,
    ) -> str:
        """Prepare a ``*.dist-info`` folder with metadata for this project.

        :param metadata_directory: The directory to write the metadata to
        :param config_settings: The configuration settings for the build backend
        :param _allow_fallback:
            Whether to allow the fallback to building a wheel and extracting
            the metadata from it. Should be passed as a keyword argument only.
        :returns: Name of the newly created subfolder within
                  ``metadata_directory``, containing the metadata.

        .. admonition:: Fallback

            If the build backend does not define a hook with this name and
            ``_allow_fallback`` is truthy, the backend will be asked to build a
            wheel via the ``build_editable`` hook and the dist-info
            extracted from that will be returned.
        """
        return self._call_hook(
            "prepare_metadata_for_build_editable",
            {
                "metadata_directory": abspath(metadata_directory),
                "config_settings": config_settings,
                "_allow_fallback": _allow_fallback,
            },
        )

    def build_editable(
        self,
        wheel_directory: str,
        config_settings: Optional[Mapping[str, Any]] = None,
        metadata_directory: Optional[str] = None,
    ) -> str:
        """Build an editable wheel from this project.

        :param wheel_directory: The directory to write the wheel to
        :param config_settings: The configuration settings for the build backend
        :param metadata_directory: The directory to reuse existing metadata from
        :returns:
            The name of the newly created wheel within ``wheel_directory``.

        .. admonition:: Interaction with fallback

            If the ``build_editable`` hook was called in the fallback for
            :meth:`prepare_metadata_for_build_editable`, the build backend
            would not be invoked. Instead, the previously built wheel will be
            copied to ``wheel_directory`` and the name of that file will be
            returned.
        """
        if metadata_directory is not None:
            metadata_directory = abspath(metadata_directory)
        return self._call_hook(
            "build_editable",
            {
                "wheel_directory": abspath(wheel_directory),
                "config_settings": config_settings,
                "metadata_directory": metadata_directory,
            },
        )

    def get_requires_for_build_sdist(
        self,
        config_settings: Optional[Mapping[str, Any]] = None,
    ) -> Sequence[str]:
        """Get additional dependencies required for building an sdist.

        :returns: A list of :pep:`dependency specifiers <508>`.
        """
        return self._call_hook(
            "get_requires_for_build_sdist", {"config_settings": config_settings}
        )

    def build_sdist(
        self,
        sdist_directory: str,
        config_settings: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """Build an sdist from this project.

        :returns:
            The name of the newly created sdist within ``sdist_directory``.
        """
        return self._call_hook(
            "build_sdist",
            {
                "sdist_directory": abspath(sdist_directory),
                "config_settings": config_settings,
            },
        )

    def _read_correlated_output(
        self,
        td: str,
        hook_name: str,
        kwargs: Mapping[str, Any],
        request_id: Optional[str],
    ) -> Mapping[str, Any]:
        """Read ``output.json`` and prove it answers this very request.

        Raise :exc:`CorrelationError` (without delivering any return value or
        warnings) if anything about the response does not correlate.
        """

        def fail(reason: str) -> "CorrelationError":
            return CorrelationError(
                f"Cannot trust the response of hook {hook_name!r}: {reason}"
            )

        path = pjoin(td, "output.json")
        try:
            data = read_json_strict(path)
        except FileNotFoundError:
            raise fail(
                "the subprocess did not write output.json, so its result cannot "
                "be matched to the request"
            ) from None
        except json.JSONDecodeError as e:
            raise fail(f"output.json is not valid JSON ({e.msg})") from None
        except ValueError as e:
            raise fail(f"output.json is malformed ({e})") from None

        if not isinstance(data, dict):
            raise fail("output.json must contain a JSON object")

        keys = set(data)
        missing = _REQUIRED_RESPONSE_KEYS - keys
        if missing:
            raise fail(f"output.json is missing field(s): {sorted(missing)}")
        unknown = keys - _KNOWN_RESPONSE_KEYS
        if unknown:
            raise fail(f"output.json contains unexpected field(s): {sorted(unknown)}")

        protocol = data["protocol"]
        # bool is a subclass of int; reject it explicitly.
        if type(protocol) is not int:
            raise fail(f"the protocol version must be an integer, not {protocol!r}")
        if protocol != CORRELATION_PROTOCOL_VERSION:
            raise fail(
                f"protocol version mismatch: the response uses version {protocol}, "
                f"but this frontend supports version {CORRELATION_PROTOCOL_VERSION}"
            )

        response_id = data["request_id"]
        if not isinstance(response_id, str):
            raise fail("the response call identifier must be a string")
        if response_id != request_id:
            raise fail(
                f"the response answers call {response_id!r}, but this request was "
                f"call {request_id!r}; a stale or misrouted response may have been "
                "delivered by the subprocess runner"
            )

        response_digest = data["request_digest"]
        if not isinstance(response_digest, str):
            raise fail("the request digest must be a string")
        expected_digest = _request_digest(hook_name, kwargs)
        if response_digest != expected_digest:
            raise fail(
                "the response certifies different request content than the request "
                "which was sent (different hook name or arguments)"
            )

        if type(data["unsupported"]) is not bool:
            raise fail("the 'unsupported' field must be a boolean")
        if not isinstance(data["warnings"], list):
            raise fail("the 'warnings' field must be a list")

        return data

    def _call_hook(self, hook_name: str, kwargs: Mapping[str, Any]) -> Any:
        extra_environ = {"_PYPROJECT_HOOKS_BUILD_BACKEND": self.build_backend}

        if self.backend_path:
            extra_environ["_PYPROJECT_HOOKS_BACKEND_PATH_JSON"] = json.dumps(
                self.backend_path
            )

        with tempfile.TemporaryDirectory() as td:
            hook_input: dict[str, Any] = {"kwargs": kwargs}
            request_id: Optional[str] = None
            if self.verify_correlation:
                # A fresh identity for each independent hook call makes a
                # replayed response from an earlier call detectable.
                request_id = str(uuid.uuid4())
                hook_input["protocol"] = CORRELATION_PROTOCOL_VERSION
                hook_input["request_id"] = request_id
            write_json(hook_input, pjoin(td, "input.json"), indent=2)

            # Run the hook in a subprocess
            with _in_proc_script_path() as script:
                python = self.python_executable
                self._subprocess_runner(
                    [python, abspath(str(script)), hook_name, td],
                    cwd=self.source_dir,
                    extra_environ=extra_environ,
                )

            if self.verify_correlation:
                # Validate before anything (return value, warnings, errors) is
                # taken from the response.
                data = self._read_correlated_output(td, hook_name, kwargs, request_id)
            else:
                data = read_json(pjoin(td, "output.json"))
            if data.get("unsupported"):
                raise UnsupportedOperation(data.get("traceback", ""))
            if data.get("no_backend"):
                raise BackendUnavailable(
                    data.get("traceback", ""),
                    message=data.get("backend_error", ""),
                    backend_name=self.build_backend,
                    backend_path=self.backend_path,
                )
            if data.get("hook_missing"):
                raise HookMissing(data.get("missing_hook_name") or hook_name)

            for w in data.get("warnings", []):
                warnings.warn_explicit(
                    message=w["message"],
                    category=BuildBackendWarning,
                    filename=w["filename"],
                    lineno=w["lineno"],
                )
            return data["return_val"]
