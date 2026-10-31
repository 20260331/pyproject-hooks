"""Compare ``get_requires`` queries on independent frozen source copies.

This is a *test support* interface.  A ``get_requires`` hook runs with the
source directory as its current working directory, and a backend may create or
modify files there (bytecode caches, generated sources, lock files...).  When
several queries are made against the same tree, files written by an earlier
query can be imported or inspected by a later one, so that the interpreter
state or the configuration being compared no longer reflects a pristine tree.

To keep scenarios independent, :func:`compare_get_requires` first freezes the
ordinary (regular) source files once, in memory, and then materialises a fresh
copy of that frozen snapshot for every scenario.  Each query therefore starts
from an identical, pristine tree and files written by one scenario cannot leak
into another.

The returned :class:`QueryComparisonReport` keeps the *original* requirement
lists and reports, per scenario, the dependency members, repeated occurrences
and every file added, deleted or modified inside the copy (with the file
contents captured).  It also reports order and membership differences across
scenarios.

Scope and limitations:

* Only the three ``get_requires_*`` hooks are ever called.  Dependencies are
  never installed and ``prepare_metadata_*`` / ``build_*`` hooks are never
  executed.
* A failure of a single scenario is recorded on that scenario's result and the
  remaining scenarios still run.  A :class:`KeyboardInterrupt` stops the run
  straight away; all copies are cleaned up.
* Everything in the report is plain captured data, so it stays viewable after
  the temporary copies have been removed.
* This is **not** a sandbox.  Only current-working-directory relative writes
  are isolated: a backend can still read or write files outside of its copy
  through absolute paths, the network, the environment, etc.
* Only regular files are tracked.  Symbolic links and other non-regular files
  are ignored both when freezing and when diffing a copy.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from pyproject_hooks import BuildBackendHookCaller

if TYPE_CHECKING:
    from pyproject_hooks import SubprocessRunner

#: The only hooks this helper is allowed to call.
GET_REQUIRES_HOOKS = frozenset(
    {
        "get_requires_for_build_wheel",
        "get_requires_for_build_sdist",
        "get_requires_for_build_editable",
    }
)

#: Path parts ignored when freezing/diffing trees by default (bytecode caches
#: written while importing the backend are not source-tree pollution).
DEFAULT_IGNORED_PARTS: Tuple[str, ...] = ("__pycache__",)


@dataclass(frozen=True)
class Scenario:
    """One ``get_requires`` query to compare.

    :param name: Unique label used in the report.
    :param hook: Name of the ``get_requires_*`` hook to call.
    :param config_settings: ``config_settings`` passed to the hook.
    """

    name: str
    hook: str = "get_requires_for_build_wheel"
    config_settings: Optional[Mapping[str, Any]] = None


@dataclass
class FileChanges:
    """Files added, deleted or modified inside a scenario copy.

    Mapping keys are forward-slash relative paths.  The captured bytes are kept
    on the report so that the changes remain inspectable once the copy is gone.
    """

    added: Dict[str, bytes] = field(default_factory=dict)
    deleted: Dict[str, bytes] = field(default_factory=dict)
    modified: Dict[str, Dict[str, bytes]] = field(default_factory=dict)

    def is_clean(self) -> bool:
        """Return whether the copy is byte-for-byte identical to the freeze."""
        return not (self.added or self.deleted or self.modified)


@dataclass
class ScenarioResult:
    """Outcome of running one :class:`Scenario` on its own copy."""

    name: str
    hook: str
    config_settings: Optional[Mapping[str, Any]]
    requirements: Optional[List[str]] = None
    members: Optional[List[str]] = None
    duplicates: Dict[str, int] = field(default_factory=dict)
    changes: FileChanges = field(default_factory=FileChanges)
    error: Optional[Dict[str, str]] = None
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        """Whether the hook ran without raising."""
        return self.error is None and not self.interrupted


@dataclass
class OrderDifference:
    """Two scenarios with the same member set returned in a different order."""

    reference: str
    scenario: str
    reference_order: List[str]
    scenario_order: List[str]
    reordered: List[str]


@dataclass
class MembershipDifference:
    """Two scenarios whose requirement member sets differ."""

    reference: str
    scenario: str
    only_in_reference: List[str]
    only_in_scenario: List[str]


@dataclass
class QueryComparisonReport:
    """Result of a :func:`compare_get_requires` run.

    Stays fully usable after every temporary copy has been deleted: all file
    contents are inlined on the per-scenario :class:`FileChanges`.
    """

    source_dir: str
    scenarios: List[ScenarioResult] = field(default_factory=list)
    order_differences: List[OrderDifference] = field(default_factory=list)
    membership_differences: List[MembershipDifference] = field(default_factory=list)
    interrupted: bool = False

    def result(self, name: str) -> ScenarioResult:
        """Return the result for the scenario called ``name``."""
        for scenario in self.scenarios:
            if scenario.name == name:
                return scenario
        raise KeyError(name)

    def to_text(self) -> str:
        """Render a human-readable summary of the whole report."""
        lines = [
            f"get_requires query comparison for: {self.source_dir}",
            f"interrupted: {'yes' if self.interrupted else 'no'}",
        ]
        for scenario in self.scenarios:
            lines.append("")
            lines.append(
                f"Scenario {scenario.name!r} via {scenario.hook} "
                f"(config_settings={scenario.config_settings!r})"
            )
            if scenario.interrupted:
                lines.append("  interrupted before it finished")
            if scenario.error is not None:
                lines.append(
                    f"  error: {scenario.error['type']}: {scenario.error['message']}"
                )
            if scenario.requirements is not None:
                lines.append(f"  requirements: {scenario.requirements!r}")
                lines.append(f"  members: {scenario.members!r}")
                lines.append(f"  repeated occurrences: {scenario.duplicates!r}")
            changes = scenario.changes
            for path in sorted(changes.added):
                lines.append(f"  added: {path}")
                lines.extend(_indent_bytes(changes.added[path]))
            for path in sorted(changes.deleted):
                lines.append(f"  deleted: {path} (before)")
                lines.extend(_indent_bytes(changes.deleted[path]))
            for path in sorted(changes.modified):
                lines.append(f"  modified: {path} (before)")
                lines.extend(_indent_bytes(changes.modified[path]["before"]))
                lines.append(f"  modified: {path} (after)")
                lines.extend(_indent_bytes(changes.modified[path]["after"]))
            if changes.is_clean():
                lines.append("  copy changes: none")
        for membership in self.membership_differences:
            lines.append("")
            lines.append(
                f"membership differs: {membership.scenario!r} vs reference "
                f"{membership.reference!r}"
            )
            lines.append(f"  only in reference: {membership.only_in_reference!r}")
            lines.append(f"  only in scenario: {membership.only_in_scenario!r}")
        for order in self.order_differences:
            lines.append("")
            lines.append(
                f"order differs: {order.scenario!r} vs reference "
                f"{order.reference!r}"
            )
            lines.append(f"  reference order: {order.reference_order!r}")
            lines.append(f"  scenario order: {order.scenario_order!r}")
            lines.append(f"  reordered members: {order.reordered!r}")
        return "\n".join(lines)


def _indent_bytes(data: bytes) -> List[str]:
    text = data.decode("utf-8", errors="replace")
    return [f"    | {line}" for line in text.splitlines()] or ["    | <empty file>"]


def _iter_regular_files(
    root: str, ignored_parts: Sequence[str]
) -> List[Tuple[str, str]]:
    """List ordinary files under ``root`` as ``(relative, absolute)`` pairs.

    Directories or files whose path contains one of ``ignored_parts`` are
    skipped, as are symlinks and other non-regular files.
    """
    ignored = frozenset(ignored_parts)
    found: List[Tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in ignored)
        for filename in sorted(filenames):
            absolute = os.path.join(dirpath, filename)
            relative = os.path.relpath(absolute, root).replace(os.sep, "/")
            if any(part in ignored for part in relative.split("/")):
                continue
            if os.path.islink(absolute) or not os.path.isfile(absolute):
                continue
            found.append((relative, absolute))
    return found


def _freeze_tree(source_dir: str, ignored_parts: Sequence[str]) -> Dict[str, bytes]:
    """Read every ordinary source file once into an in-memory snapshot."""
    snapshot: Dict[str, bytes] = {}
    for relative, absolute in _iter_regular_files(source_dir, ignored_parts):
        with open(absolute, "rb") as f:
            snapshot[relative] = f.read()
    return snapshot


def _materialize(copy_dir: str, snapshot: Mapping[str, bytes]) -> None:
    """Write a fresh copy of ``snapshot`` into the empty ``copy_dir``."""
    for relative in sorted(snapshot):
        destination = os.path.join(copy_dir, *relative.split("/"))
        parent = os.path.dirname(destination)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(destination, "wb") as f:
            f.write(snapshot[relative])


def _diff_copy(
    copy_dir: str, snapshot: Mapping[str, bytes], ignored_parts: Sequence[str]
) -> FileChanges:
    """Capture the additions, deletions and modifications made to a copy."""
    current = _freeze_tree(copy_dir, ignored_parts)
    changes = FileChanges()
    for relative in sorted(set(current) - set(snapshot)):
        changes.added[relative] = current[relative]
    for relative in sorted(set(snapshot) - set(current)):
        changes.deleted[relative] = snapshot[relative]
    for relative in sorted(set(current) & set(snapshot)):
        if current[relative] != snapshot[relative]:
            changes.modified[relative] = {
                "before": snapshot[relative],
                "after": current[relative],
            }
    return changes


def _members_and_duplicates(
    requirements: Sequence[str],
) -> Tuple[List[str], Dict[str, int]]:
    """Return unique members in first-seen order and extra repeat counts."""
    members: List[str] = []
    counts: Dict[str, int] = {}
    for requirement in requirements:
        if requirement not in counts:
            counts[requirement] = 0
            members.append(requirement)
        counts[requirement] += 1
    duplicates = {
        requirement: count - 1 for requirement, count in counts.items() if count > 1
    }
    return members, duplicates


def _preflight(
    source_dir: str,
    scenarios: Sequence[Scenario],
    reference: Optional[str],
) -> List[Scenario]:
    """Validate the request before any copy is created or hook is called."""
    if not os.path.isdir(source_dir):
        raise NotADirectoryError(source_dir)
    scenario_list = list(scenarios)
    if not scenario_list:
        raise ValueError("at least one scenario is required")
    names = set()
    for scenario in scenario_list:
        if not isinstance(scenario, Scenario):
            raise TypeError(f"expected a Scenario, got {scenario!r}")
        if not scenario.name:
            raise ValueError("scenario names must be non-empty strings")
        if scenario.name in names:
            raise ValueError(f"duplicate scenario name: {scenario.name!r}")
        names.add(scenario.name)
        if scenario.hook not in GET_REQUIRES_HOOKS:
            raise ValueError(
                f"scenario {scenario.name!r} requests disallowed hook "
                f"{scenario.hook!r}; only {sorted(GET_REQUIRES_HOOKS)} are "
                "permitted"
            )
    if reference is not None and reference not in names:
        raise ValueError(f"unknown reference scenario: {reference!r}")
    return scenario_list


def _fill_comparisons(report: QueryComparisonReport, reference: Optional[str]) -> None:
    """Compare successful scenarios against the reference scenario."""
    successful = [
        scenario
        for scenario in report.scenarios
        if scenario.ok and scenario.members is not None
    ]
    if not successful:
        return
    if reference is not None:
        named = next(
            (scenario for scenario in successful if scenario.name == reference),
            None,
        )
        # A failed reference cannot be used; fall back to the first success.
        ref = named if named is not None else successful[0]
    else:
        ref = successful[0]

    for scenario in successful:
        if scenario is ref:
            continue
        assert ref.members is not None and scenario.members is not None
        ref_set = set(ref.members)
        scenario_set = set(scenario.members)
        only_reference = sorted(ref_set - scenario_set)
        only_scenario = sorted(scenario_set - ref_set)
        if only_reference or only_scenario:
            report.membership_differences.append(
                MembershipDifference(
                    reference=ref.name,
                    scenario=scenario.name,
                    only_in_reference=only_reference,
                    only_in_scenario=only_scenario,
                )
            )
        elif ref.members != scenario.members:
            reordered = [
                member
                for member in ref.members
                if ref.members.index(member) != scenario.members.index(member)
            ]
            report.order_differences.append(
                OrderDifference(
                    reference=ref.name,
                    scenario=scenario.name,
                    reference_order=list(ref.members),
                    scenario_order=list(scenario.members),
                    reordered=reordered,
                )
            )


def compare_get_requires(
    source_dir: str,
    build_backend: str,
    scenarios: Sequence[Scenario],
    backend_path: Optional[Sequence[str]] = None,
    runner: Optional[SubprocessRunner] = None,
    python_executable: Optional[str] = None,
    reference: Optional[str] = None,
    ignored_parts: Sequence[str] = DEFAULT_IGNORED_PARTS,
) -> QueryComparisonReport:
    """Run several ``get_requires`` queries, each on an independent frozen copy.

    :param source_dir: The source tree to freeze and copy.
    :param build_backend: The build backend spec, as for
        :class:`~pyproject_hooks.BuildBackendHookCaller`.
    :param scenarios: The queries to run, one fresh copy per scenario.
    :param backend_path: Relative ``backend-path`` entries, copied together
        with the tree and re-resolved against every scenario copy.
    :param runner: Optional subprocess runner, passed to every hook caller.
    :param python_executable: Python executable used for the hook subprocesses.
    :param reference: Name of the scenario to compare the others against;
        defaults to the first successful scenario.
    :param ignored_parts: Path parts to ignore when freezing and diffing.
    :returns: A :class:`QueryComparisonReport` that remains usable after the
        temporary copies have been removed.

    A failing scenario is recorded on its result and does not stop the run.
    A :class:`KeyboardInterrupt` stops the run instead; copies are always
    removed and results gathered so far are returned on the report.
    """
    source_dir = os.path.abspath(source_dir)
    scenario_list = _preflight(source_dir, scenarios, reference)

    # Freeze the ordinary source files once; every copy comes from this
    # snapshot, never from another (possibly mutated) copy.
    snapshot = _freeze_tree(source_dir, ignored_parts)

    report = QueryComparisonReport(source_dir=source_dir)
    in_flight: Optional[ScenarioResult] = None
    try:
        for scenario in scenario_list:
            result = ScenarioResult(
                name=scenario.name,
                hook=scenario.hook,
                config_settings=scenario.config_settings,
            )
            in_flight = result
            copy_dir = tempfile.mkdtemp(prefix="pyproject-hooks-query-")
            try:
                _materialize(copy_dir, snapshot)
                # A fresh caller per scenario keeps instance state separate;
                # each hook call additionally runs in its own subprocess.
                caller = BuildBackendHookCaller(
                    copy_dir,
                    build_backend,
                    backend_path=backend_path,
                    runner=runner,
                    python_executable=python_executable,
                )
                hook = getattr(caller, scenario.hook)
                try:
                    returned = hook(scenario.config_settings)
                except Exception as exc:
                    result.error = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                else:
                    result.requirements = list(returned)
                    (
                        result.members,
                        result.duplicates,
                    ) = _members_and_duplicates(result.requirements)
                # Capture the damage before the copy is removed, even if the
                # hook itself failed.
                result.changes = _diff_copy(copy_dir, snapshot, ignored_parts)
            finally:
                shutil.rmtree(copy_dir, ignore_errors=True)
            in_flight = None
            report.scenarios.append(result)
    except KeyboardInterrupt:
        # Stop on user interrupt; the copy of the in-flight scenario has
        # already been removed by its ``finally`` block.
        report.interrupted = True
        if in_flight is not None:
            in_flight.interrupted = True
            report.scenarios.append(in_flight)

    _fill_comparisons(report, reference)
    return report
