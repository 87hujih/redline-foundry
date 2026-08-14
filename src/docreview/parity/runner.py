"""Versioned, capture-only parity comparator and report renderer."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

RUNNER_VERSION = "1.0.0"
MANIFEST_SCHEMA = "docreview.parity.manifest/v1"
CAPTURE_SCHEMA = "docreview.parity.capture/v1"
EXECUTION_POLICY = "capture_only_no_real_side_effects"
VERIFICATION_LEVELS = frozenset({"fixed_fixture", "static_contract", "authorized_snapshot"})
CAPTURE_MODES = frozenset({"offline_fixture_and_static_contract", "authorized_snapshot"})

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]


class ParityFormatError(ValueError):
    """A manifest or capture does not satisfy the versioned schema."""


class CaptureSafetyError(ParityFormatError):
    """A capture violates the no-real-side-effects comparison boundary."""


@dataclass(frozen=True, slots=True)
class ScenarioCapture:
    id: str
    category: str
    verification: str
    observation: JSONValue
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Capture:
    implementation: str
    fixture_version: str
    source_revision: str
    capture_mode: str
    real_side_effects_executed: bool
    digest: str
    scenarios: tuple[ScenarioCapture, ...]


@dataclass(frozen=True, slots=True)
class Gate:
    id: str
    area: str
    status: str
    requirement: str
    missing_evidence: str


@dataclass(frozen=True, slots=True)
class ParityBundle:
    manifest_path: Path
    runner_version: str
    fixture_version: str
    evidence_date: str
    request_execution_policy: str
    canary_verdict: str
    go_capture: Capture
    python_capture: Capture
    gates: tuple[Gate, ...]


@dataclass(frozen=True, slots=True)
class Difference:
    path: str
    go: JSONValue | str
    python: JSONValue | str


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    id: str
    category: str
    verification: str
    passed: bool
    differences: tuple[Difference, ...]
    go_evidence: tuple[str, ...]
    python_evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParityResult:
    runner_version: str
    fixture_version: str
    go_digest: str
    python_digest: str
    scenarios: tuple[ScenarioResult, ...]

    @property
    def passed(self) -> int:
        return sum(item.passed for item in self.scenarios)

    @property
    def failed(self) -> int:
        return len(self.scenarios) - self.passed

    def as_json(self) -> dict[str, JSONValue]:
        return {
            "schema_version": "docreview.parity.result/v1",
            "runner_version": self.runner_version,
            "fixture_version": self.fixture_version,
            "go_digest": self.go_digest,
            "python_digest": self.python_digest,
            "summary": {
                "total": len(self.scenarios),
                "passed": self.passed,
                "failed": self.failed,
            },
            "scenarios": [
                {
                    "id": item.id,
                    "category": item.category,
                    "verification": item.verification,
                    "passed": item.passed,
                    "differences": [
                        {"path": diff.path, "go": diff.go, "python": diff.python}
                        for diff in item.differences
                    ],
                }
                for item in self.scenarios
            ],
        }


def _read_json(path: Path) -> tuple[dict[str, JSONValue], str]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ParityFormatError(f"invalid JSON in {path}") from error
    if not isinstance(value, dict):
        raise ParityFormatError(f"{path} must contain a JSON object")
    return cast(dict[str, JSONValue], value), hashlib.sha256(raw).hexdigest()


def _object(value: JSONValue | None, field: str) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        raise ParityFormatError(f"{field} must be an object")
    return value


def _array(value: JSONValue | None, field: str) -> list[JSONValue]:
    if not isinstance(value, list):
        raise ParityFormatError(f"{field} must be an array")
    return value


def _string(value: JSONValue | None, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ParityFormatError(f"{field} must be a non-empty string")
    return value


def _boolean(value: JSONValue | None, field: str) -> bool:
    if not isinstance(value, bool):
        raise ParityFormatError(f"{field} must be a boolean")
    return value


def _strings(value: JSONValue | None, field: str) -> tuple[str, ...]:
    items = _array(value, field)
    return tuple(_string(item, f"{field}[]") for item in items)


def _load_capture(path: Path, expected: str, fixture_version: str) -> Capture:
    value, digest = _read_json(path)
    if value.get("schema_version") != CAPTURE_SCHEMA:
        raise ParityFormatError(f"{path} has an unsupported capture schema")
    implementation = _string(value.get("implementation"), "implementation")
    if implementation != expected:
        raise ParityFormatError(f"{path} must describe the {expected} implementation")
    if _string(value.get("fixture_version"), "fixture_version") != fixture_version:
        raise ParityFormatError(f"{path} fixture version does not match the manifest")
    real_side_effects = _boolean(
        value.get("real_side_effects_executed"), "real_side_effects_executed"
    )
    if real_side_effects:
        raise CaptureSafetyError(f"{path} records real side effects and cannot be compared")
    capture_mode = _string(value.get("capture_mode"), "capture_mode")
    if capture_mode not in CAPTURE_MODES:
        raise CaptureSafetyError(f"{path} uses unsupported capture mode {capture_mode!r}")
    raw_scenarios = _array(value.get("scenarios"), "scenarios")
    scenarios: list[ScenarioCapture] = []
    seen: set[str] = set()
    for index, raw_scenario in enumerate(raw_scenarios):
        scenario = _object(raw_scenario, f"scenarios[{index}]")
        scenario_id = _string(scenario.get("id"), f"scenarios[{index}].id")
        if scenario_id in seen:
            raise ParityFormatError(f"duplicate scenario id {scenario_id!r} in {path}")
        seen.add(scenario_id)
        verification = _string(scenario.get("verification"), f"scenarios[{index}].verification")
        if verification not in VERIFICATION_LEVELS:
            raise ParityFormatError(f"unsupported verification level {verification!r}")
        if "observation" not in scenario:
            raise ParityFormatError(f"scenario {scenario_id!r} is missing observation")
        scenarios.append(
            ScenarioCapture(
                id=scenario_id,
                category=_string(scenario.get("category"), f"scenarios[{index}].category"),
                verification=verification,
                observation=scenario["observation"],
                evidence=_strings(scenario.get("evidence"), f"scenarios[{index}].evidence"),
            )
        )
    return Capture(
        implementation=implementation,
        fixture_version=fixture_version,
        source_revision=_string(value.get("source_revision"), "source_revision"),
        capture_mode=capture_mode,
        real_side_effects_executed=real_side_effects,
        digest=digest,
        scenarios=tuple(scenarios),
    )


def _capture_path(root: Path, value: JSONValue | None, field: str) -> Path:
    path = (root / _string(value, field)).resolve()
    if not path.is_relative_to(root.resolve()):
        raise CaptureSafetyError(f"{field} must stay inside the versioned fixture directory")
    return path


def load_bundle(manifest_path: Path) -> ParityBundle:
    manifest_path = manifest_path.resolve()
    value, _ = _read_json(manifest_path)
    if value.get("schema_version") != MANIFEST_SCHEMA:
        raise ParityFormatError("unsupported parity manifest schema")
    runner_version = _string(value.get("runner_version"), "runner_version")
    if runner_version != RUNNER_VERSION:
        raise ParityFormatError(
            f"manifest runner {runner_version} is incompatible with runner {RUNNER_VERSION}"
        )
    policy = _string(value.get("request_execution_policy"), "request_execution_policy")
    if policy != EXECUTION_POLICY:
        raise CaptureSafetyError("manifest does not enforce capture-only comparison")
    fixture_version = _string(value.get("fixture_version"), "fixture_version")
    root = manifest_path.parent
    go_capture = _load_capture(
        _capture_path(root, value.get("go_capture"), "go_capture"), "go", fixture_version
    )
    python_capture = _load_capture(
        _capture_path(root, value.get("python_capture"), "python_capture"),
        "python",
        fixture_version,
    )
    gates: list[Gate] = []
    for index, raw_gate in enumerate(_array(value.get("gates"), "gates")):
        gate = _object(raw_gate, f"gates[{index}]")
        gates.append(
            Gate(
                id=_string(gate.get("id"), f"gates[{index}].id"),
                area=_string(gate.get("area"), f"gates[{index}].area"),
                status=_string(gate.get("status"), f"gates[{index}].status"),
                requirement=_string(gate.get("requirement"), f"gates[{index}].requirement"),
                missing_evidence=_string(
                    gate.get("missing_evidence"), f"gates[{index}].missing_evidence"
                ),
            )
        )
    return ParityBundle(
        manifest_path=manifest_path,
        runner_version=runner_version,
        fixture_version=fixture_version,
        evidence_date=_string(value.get("evidence_date"), "evidence_date"),
        request_execution_policy=policy,
        canary_verdict=_string(value.get("canary_verdict"), "canary_verdict"),
        go_capture=go_capture,
        python_capture=python_capture,
        gates=tuple(gates),
    )


def _differences(go: JSONValue, python: JSONValue, path: str = "$") -> list[Difference]:
    if type(go) is not type(python):
        return [Difference(path, go, python)]
    if isinstance(go, dict) and isinstance(python, dict):
        output: list[Difference] = []
        for key in sorted(set(go) | set(python)):
            child_path = f"{path}.{key}"
            if key not in go:
                output.append(Difference(child_path, "<missing>", python[key]))
            elif key not in python:
                output.append(Difference(child_path, go[key], "<missing>"))
            else:
                output.extend(_differences(go[key], python[key], child_path))
        return output
    if isinstance(go, list) and isinstance(python, list):
        output = []
        for index in range(max(len(go), len(python))):
            child_path = f"{path}[{index}]"
            if index >= len(go):
                output.append(Difference(child_path, "<missing>", python[index]))
            elif index >= len(python):
                output.append(Difference(child_path, go[index], "<missing>"))
            else:
                output.extend(_differences(go[index], python[index], child_path))
        return output
    return [] if go == python else [Difference(path, go, python)]


def compare_bundle(bundle: ParityBundle) -> ParityResult:
    go = {item.id: item for item in bundle.go_capture.scenarios}
    python = {item.id: item for item in bundle.python_capture.scenarios}
    if set(go) != set(python):
        missing_go = sorted(set(python) - set(go))
        missing_python = sorted(set(go) - set(python))
        raise ParityFormatError(
            "capture scenario sets differ: "
            f"missing_go={missing_go}, missing_python={missing_python}"
        )
    results: list[ScenarioResult] = []
    for scenario_id in go:
        go_item = go[scenario_id]
        python_item = python[scenario_id]
        if (go_item.category, go_item.verification) != (
            python_item.category,
            python_item.verification,
        ):
            raise ParityFormatError(f"scenario metadata differs for {scenario_id!r}")
        differences = tuple(_differences(go_item.observation, python_item.observation))
        results.append(
            ScenarioResult(
                id=scenario_id,
                category=go_item.category,
                verification=go_item.verification,
                passed=not differences,
                differences=differences,
                go_evidence=go_item.evidence,
                python_evidence=python_item.evidence,
            )
        )
    return ParityResult(
        runner_version=bundle.runner_version,
        fixture_version=bundle.fixture_version,
        go_digest=bundle.go_capture.digest,
        python_digest=bundle.python_capture.digest,
        scenarios=tuple(results),
    )


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_markdown(bundle: ParityBundle, result: ParityResult) -> str:
    authorized_snapshots = sum(
        capture.capture_mode == "authorized_snapshot"
        for capture in (bundle.go_capture, bundle.python_capture)
    )
    category_rows: list[str] = []
    for category in sorted({item.category for item in result.scenarios}):
        items = [item for item in result.scenarios if item.category == category]
        passed = sum(item.passed for item in items)
        category_rows.append(
            f"| `{category}` | {passed}/{len(items)} | "
            f"{'PASS' if passed == len(items) else 'FAIL'} |"
        )
    scenario_rows = [
        "| 场景 | 分类 | 证据级别 | 结果 |",
        "| --- | --- | --- | --- |",
    ]
    for item in result.scenarios:
        scenario_rows.append(
            f"| `{item.id}` | `{item.category}` | `{item.verification}` | "
            f"{'PASS' if item.passed else 'FAIL'} |"
        )
    difference_lines: list[str] = []
    for item in result.scenarios:
        for difference in item.differences:
            difference_lines.append(
                f"- `{item.id}` `{difference.path}`: Go=`{_escape(str(difference.go))}`; "
                f"Python=`{_escape(str(difference.python))}`"
            )
    if not difference_lines:
        difference_lines.append("- 固定 fixture 与静态契约捕获没有差异。")
    gate_rows = [
        "| Gate | 领域 | 状态 | 要求 | 尚缺证据 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for gate in bundle.gates:
        gate_rows.append(
            f"| `{gate.id}` | {gate.area} | `{gate.status}` | {_escape(gate.requirement)} | "
            f"{_escape(gate.missing_evidence)} |"
        )
    return "\n".join(
        [
            "# Go/Python Parity Report",
            "",
            f"**Evidence date:** {bundle.evidence_date}  ",
            f"**Runner:** `{bundle.runner_version}`  ",
            f"**Fixture:** `{bundle.fixture_version}`  ",
            f"**Go capture SHA-256:** `{result.go_digest}`  ",
            f"**Python capture SHA-256:** `{result.python_digest}`",
            "",
            "## Safety boundary",
            "",
            "The runner is capture-only. It does not send HTTP requests, call providers or tools, "
            "open a database connection, run migrations, or execute commit/outbox side effects. "
            "Both captures declare `real_side_effects_executed=false`; the runner rejects "
            "a capture "
            "that declares otherwise. No request is executed against both implementations.",
            "",
            "## Summary",
            "",
            f"- Scenarios: {len(result.scenarios)}",
            f"- Passed: {result.passed}",
            f"- Failed: {result.failed}",
            f"- Authorized database snapshot captures: {authorized_snapshots}",
            "- Production/canary requests: 0",
            "",
            "| Category | Passed | Result |",
            "| --- | --- | --- |",
            *category_rows,
            "",
            "## Scenario results",
            "",
            *scenario_rows,
            "",
            "## Differences",
            "",
            *difference_lines,
            "",
            "A PASS means the versioned fixed fixture or static contract capture is equal. It is "
            "not database round-trip, multi-worker, provider, ingress, load, or production "
            "evidence.",
            "",
            "## Unmet release gates",
            "",
            *gate_rows,
            "",
            "## Canary verdict",
            "",
            f"`{bundle.canary_verdict}`. 当前不得执行生产 canary; 完成全部阻塞 gate 后, "
            "仍需人工批准入口单写 canary。",
            "",
            "The operational sequence and rollback steps are in "
            "`docs/remediation/canary-runbook.md`.",
            "",
        ]
    )


def _write_or_check(path: Path, content: str, check: bool) -> bool:
    if check:
        return path.exists() and path.read_text(encoding="utf-8") == content
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare offline Go/Python parity captures")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    bundle = load_bundle(args.manifest)
    result = compare_bundle(bundle)
    result_json = json.dumps(result.as_json(), ensure_ascii=False, indent=2) + "\n"
    report = render_markdown(bundle, result)
    current = _write_or_check(args.result, result_json, args.check)
    current = _write_or_check(args.report, report, args.check) and current
    return 0 if current and result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CaptureSafetyError",
    "ParityBundle",
    "ParityFormatError",
    "ParityResult",
    "compare_bundle",
    "load_bundle",
    "main",
    "render_markdown",
]
