``pyproject_hooks``
===================

.. autoclass:: pyproject_hooks.BuildBackendHookCaller
   :special-members: __init__
   :members:

.. _Subprocess Runners:

Subprocess Runners
------------------

A subprocess runner is a function that is expected to execute the subprocess. They are typically used for controlling how output is presented from the subprocess.

The subprocess runners provided out-of-the-box with this library are:

.. autofunction:: pyproject_hooks.default_subprocess_runner(...)
.. autofunction:: pyproject_hooks.quiet_subprocess_runner(...)

Custom Subprocess Runners
^^^^^^^^^^^^^^^^^^^^^^^^^

It is possible to provide a custom subprocess runner, that behaves differently. The expected protocol for subprocess runners is as follows:

.. function:: subprocess_runner_protocol(cmd, cwd=None, extra_environ=None, env=None)
   :noindex:

   :param cmd: The command and arguments to execute, as would be passed to :func:`subprocess.run`.
   :type cmd: typing.Sequence[str]
   :param cwd: The working directory that must be used for the subprocess.
   :type cwd: typing.Optional[str]
   :param extra_environ: Legacy overlay of environment variables (name to value) which must be set for the subprocess execution. Prefer implementing ``env`` instead: ``extra_environ`` cannot express variables that have to be removed from the inherited environment.
   :type extra_environ: typing.Optional[typing.Mapping[str, str]]
   :param env: The complete environment snapshot for the hook call (the per-hook environment envelope). When provided, it must be passed to the subprocess verbatim as its ``env`` argument, instead of copying and patching :data:`os.environ`.
   :type env: typing.Optional[typing.Mapping[str, str]]

   :rtype: None

   Runners which only accept ``extra_environ`` keep working: they receive a
   best-effort overlay of the snapshot's differences against :data:`os.environ`.
   Removing inherited variables requires accepting the ``env`` parameter.

Since this codebase is currently Python 3.7-compatible, the type annotation for this protocol is only available to type checkers. To annotate a variable as a subprocess runner, you can do something along the lines of:

.. code-block:: python

   from typing import TYPE_CHECKING

   if TYPE_CHECKING:
      from pyproject_hooks import SubprocessRunner

   # Example usage
   def build(awesome_runner: "SubprocessRunner") -> None:
      ...

Exceptions
----------

Each exception has public attributes with the same name as their constructors.

.. autoexception:: pyproject_hooks.BackendUnavailable
.. autoexception:: pyproject_hooks.HookMissing
.. autoexception:: pyproject_hooks.UnsupportedOperation
