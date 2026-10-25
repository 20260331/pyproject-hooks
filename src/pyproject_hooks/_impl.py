import json
import os
import sys
import tempfile
import subprocess
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


def write_json(obj: Mapping[str, Any], path: str, **kwargs) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, **kwargs)


def read_json(path: str) -> Mapping[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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


class InvalidHookReturn(ValueError):
    """Will be raised if a hook returns a value violating its contract.

    Only raised when the caller opts in to return value validation via
    :class:`BuildBackendHookCaller`'s ``validate_results`` parameter. The
    ``hook_name`` attribute identifies the hook whose return value was
    invalid.
    """

    def __init__(self, hook_name: str, message: str) -> None:
        self.hook_name = hook_name
        super().__init__(f"Invalid value returned by {hook_name} hook: {message}")


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


# --- Optional validation of values returned by build backend hooks ---------
#
# PEP 517 / PEP 660 establish what each hook must return (a list of strings,
# or the basename of an artifact inside a given directory). A misbehaving
# backend may instead return a value of the wrong type, an incorrectly named
# or non-existent artifact, or a path escaping the target directory, which
# would otherwise surface as a confusing failure later on in the frontend.
# The checks below are a single implementation shared by every hook, and are
# only run when the caller opts in via ``validate_results=True``.

# Hooks which must return a list of strings.
_STRING_LIST_HOOKS: Mapping[str, str] = {
    "get_requires_for_build_wheel": "PEP 508 requirement specifiers",
    "get_requires_for_build_editable": "PEP 508 requirement specifiers",
    "get_requires_for_build_sdist": "PEP 508 requirement specifiers",
    "_supported_features": "supported feature names",
}

# Hooks which must return the basename of an artifact created inside the
# directory passed through a given keyword argument:
# hook name -> (directory kwarg, required name suffix, "file"/"directory")
_ARTIFACT_HOOKS: Mapping[str, tuple] = {
    "build_wheel": ("wheel_directory", ".whl", "file"),
    "build_editable": ("wheel_directory", ".whl", "file"),
    "build_sdist": ("sdist_directory", ".tar.gz", "file"),
    "prepare_metadata_for_build_wheel": (
        "metadata_directory",
        ".dist-info",
        "directory",
    ),
    "prepare_metadata_for_build_editable": (
        "metadata_directory",
        ".dist-info",
        "directory",
    ),
}


def _validate_string_list(hook_name: str, result: Any, item_kind: str) -> None:
    if not isinstance(result, list):
        raise InvalidHookReturn(
            hook_name,
            f"expected a list of {item_kind}, got {type(result).__name__}: "
            f"{result!r}",
        )
    for item in result:
        if not isinstance(item, str):
            raise InvalidHookReturn(
                hook_name,
                f"expected a list of {item_kind}, but it contains a "
                f"{type(item).__name__}: {item!r}",
            )


def _validate_artifact(
    hook_name: str,
    result: Any,
    directory: str,
    suffix: str,
    artifact_kind: str,
) -> None:
    if not isinstance(result, str):
        raise InvalidHookReturn(
            hook_name,
            f"expected the basename of a {artifact_kind} as a string, got "
            f"{type(result).__name__}: {result!r}",
        )
    if not result:
        raise InvalidHookReturn(hook_name, "returned an empty string")

    # The contract requires a bare basename: no absolute paths, separators or
    # traversal components pointing outside the target directory.
    if (
        os.path.isabs(result)
        or os.path.basename(result) != result
        or (os.altsep is not None and os.altsep in result)
    ):
        raise InvalidHookReturn(
            hook_name,
            f"must return the basename of the {artifact_kind} created inside "
            f"{directory!r}, without any path separators, got {result!r}",
        )
    if not result.endswith(suffix):
        raise InvalidHookReturn(
            hook_name,
            f"must return the name of a {artifact_kind} ending with "
            f"{suffix!r}, got {result!r}",
        )

    artifact_path = pjoin(directory, result)
    if artifact_kind == "file":
        if not os.path.isfile(artifact_path):
            raise InvalidHookReturn(
                hook_name,
                f"the returned file does not exist in {directory!r}: " f"{result!r}",
            )
    else:
        if not os.path.isdir(artifact_path):
            raise InvalidHookReturn(
                hook_name,
                f"the returned directory does not exist in {directory!r}: "
                f"{result!r}",
            )

    # Guard against a symlink (or any other non-lexical path) placing the
    # artifact outside of the directory the hook was told to write into.
    real_directory = os.path.realpath(directory)
    real_artifact = os.path.realpath(artifact_path)
    if os.path.commonpath([real_directory, real_artifact]) != real_directory:
        raise InvalidHookReturn(
            hook_name,
            f"the returned {artifact_kind} resolves outside of {directory!r}: "
            f"{result!r}",
        )


def _validate_hook_result(
    hook_name: str, result: Any, kwargs: Mapping[str, Any]
) -> None:
    """Check a successful hook return value against its declared contract.

    Raises :exc:`InvalidHookReturn` if the value does not conform. Hooks not
    present in the contract tables are left untouched.
    """
    item_kind = _STRING_LIST_HOOKS.get(hook_name)
    if item_kind is not None:
        _validate_string_list(hook_name, result, item_kind)
        return
    artifact_spec = _ARTIFACT_HOOKS.get(hook_name)
    if artifact_spec is not None:
        directory_kwarg, suffix, artifact_kind = artifact_spec
        _validate_artifact(
            hook_name,
            result,
            kwargs[directory_kwarg],
            suffix,
            artifact_kind,
        )


class BuildBackendHookCaller:
    """A wrapper to call the build backend hooks for a source directory."""

    def __init__(
        self,
        source_dir: str,
        build_backend: str,
        backend_path: Optional[Sequence[str]] = None,
        runner: Optional["SubprocessRunner"] = None,
        python_executable: Optional[str] = None,
        validate_results: bool = False,
    ) -> None:
        """
        :param source_dir: The source directory to invoke the build backend for
        :param build_backend: The build backend spec
        :param backend_path: Additional path entries for the build backend spec
        :param runner: The :ref:`subprocess runner <Subprocess Runners>` to use
        :param python_executable:
            The Python executable used to invoke the build backend
        :param validate_results:
            If true, the value returned by each successful hook call is
            checked against the hook's contract (PEP 517 / PEP 660): its
            type, the expected artifact name, and that the artifact exists
            inside (and does not escape) the directory given to the hook.
            A violation raises :exc:`InvalidHookReturn`. Existing exception
            behaviour for backend, hook and subprocess failures is unchanged.
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
        self._validate_results = validate_results

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

    def _call_hook(self, hook_name: str, kwargs: Mapping[str, Any]) -> Any:
        extra_environ = {"_PYPROJECT_HOOKS_BUILD_BACKEND": self.build_backend}

        if self.backend_path:
            extra_environ["_PYPROJECT_HOOKS_BACKEND_PATH_JSON"] = json.dumps(
                self.backend_path
            )

        with tempfile.TemporaryDirectory() as td:
            hook_input = {"kwargs": kwargs}
            write_json(hook_input, pjoin(td, "input.json"), indent=2)

            # Run the hook in a subprocess
            with _in_proc_script_path() as script:
                python = self.python_executable
                self._subprocess_runner(
                    [python, abspath(str(script)), hook_name, td],
                    cwd=self.source_dir,
                    extra_environ=extra_environ,
                )

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

            return_val = data["return_val"]
            if self._validate_results:
                _validate_hook_result(hook_name, return_val, kwargs)
            return return_val
