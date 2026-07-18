from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.execution_flow import (
    EXECUTION_FLOW_KIND,
    EXECUTION_FLOW_VERSION,
    ExecutionFlowError,
    FlowEdgeObservation,
    FlowNodeObservation,
    build_execution_flow,
    generate_execution_flow,
    load_current_execution_flow,
    load_execution_flow,
)
from tools.project_inventory import inventory_project, load_project_manifest
from tools.project_registry import register_project
from tools.research_core import ResearchCoreService


class ExecutionFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.source = root / "research"
        self.workspace.mkdir()
        self.source.mkdir()
        (self.source / "src").mkdir()
        (self.source / "src" / "model.py").write_text("class Model: pass\n", encoding="utf-8")
        (self.source / "src" / "train.py").write_text("from model import Model\n", encoding="utf-8")
        (self.source / "run.sh").write_text("python src/train.py\n", encoding="utf-8")
        self.registration = register_project(self.workspace, self.source)
        self.project_id = self.registration.project_id
        self.inventory = inventory_project(self.workspace, self.project_id)
        self.manifest = load_project_manifest(
            self.inventory.manifest_file,
            project_id=self.project_id,
            project_root=self.source,
            required_manifest_version="project-inventory-v4",
        )
        self.a = "evd-" + "a" * 64
        self.b = "evd-" + "b" * 64

    def tearDown(self) -> None:
        self.temp.cleanup()

    def nodes(self):
        return [
            FlowNodeObservation("run", "process", "run.sh", "run.sh", entrypoint=True, certainty="inferred"),
            FlowNodeObservation("train", "module", "training", "src/train.py", symbol="train", evidence_ids=(self.a,)),
            FlowNodeObservation("model", "module", "model", "src/model.py", symbol="Model", evidence_ids=(self.b,)),
        ]

    def edges(self):
        return [
            FlowEdgeObservation("run", "train", "calls", "launch", (self.a,)),
            FlowEdgeObservation("train", "model", "calls", "imports", (self.a, self.b)),
            FlowEdgeObservation("model", "train", "data-flow", "predictions", (self.b,), "uncertain", "direction inferred from host summary"),
        ]

    def test_explicit_flow_is_deterministic_and_current(self) -> None:
        observations = self.nodes() + self.edges()
        first = generate_execution_flow(self.workspace, self.project_id, observations=observations)
        second = generate_execution_flow(self.workspace, self.project_id, observations=list(reversed(observations)))
        self.assertEqual(first.execution_flow, second.execution_flow)
        self.assertEqual(first.execution_flow_file.read_bytes(), second.execution_flow_file.read_bytes())
        payload = first.execution_flow
        self.assertEqual(payload["kind"], EXECUTION_FLOW_KIND)
        self.assertEqual(payload["flow_version"], EXECUTION_FLOW_VERSION)
        self.assertEqual(payload["entrypoints"], ["run"])
        self.assertEqual(payload["coverage"]["call_edges"], 2)
        self.assertEqual(payload["coverage"]["data_flow_edges"], 1)
        self.assertEqual(payload["coverage"]["uncertain_edges"], 1)
        self.assertEqual(load_current_execution_flow(self.workspace, self.project_id), payload)

    def test_metadata_fallback_marks_unknown_paths_uncertain(self) -> None:
        result = generate_execution_flow(self.workspace, self.project_id)
        payload = result.execution_flow
        self.assertEqual(payload["derivation"]["mode"], "manifest-metadata-fallback")
        self.assertFalse(payload["derivation"]["source_content_read"])
        self.assertGreaterEqual(payload["coverage"]["entrypoints"], 1)
        self.assertGreater(payload["coverage"]["uncertain_nodes"], 0)
        self.assertEqual(load_current_execution_flow(self.workspace, self.project_id), payload)

    def test_invalid_endpoint_path_and_evidence_fail_closed(self) -> None:
        with self.assertRaises(ExecutionFlowError):
            build_execution_flow(self.manifest, project_id=self.project_id, manifest_sha256="0" * 64, observations=[FlowNodeObservation("x", "module", "missing", "missing.py")])
        with self.assertRaises(ExecutionFlowError):
            FlowEdgeObservation("x", "y", "calls", evidence_ids=("evd-" + "z" * 63,))
        with self.assertRaises(ExecutionFlowError):
            build_execution_flow(self.manifest, project_id=self.project_id, manifest_sha256="0" * 64, observations=[FlowNodeObservation("x", "module", "x"), FlowEdgeObservation("x", "y", "calls")])

    def test_loader_rejects_tamper_duplicate_and_noncanonical(self) -> None:
        result = generate_execution_flow(self.workspace, self.project_id, observations=self.nodes() + self.edges())
        payload = deepcopy(result.execution_flow)
        payload["summary"] = "tampered"
        result.execution_flow_file.write_bytes((json.dumps(payload, sort_keys=True, indent=2) + "\n").encode())
        with self.assertRaises(ExecutionFlowError):
            load_execution_flow(result.execution_flow_file, project_id=self.project_id)
        duplicate = result.execution_flow_file.read_bytes().replace(b'"kind": "llmwiki-execution-flow"', b'"kind": "llmwiki-execution-flow", "kind": "llmwiki-execution-flow"', 1)
        result.execution_flow_file.write_bytes(duplicate)
        with self.assertRaises(ExecutionFlowError):
            load_execution_flow(result.execution_flow_file, project_id=self.project_id)

    def test_current_loader_rejects_stale_manifest(self) -> None:
        generate_execution_flow(self.workspace, self.project_id)
        (self.source / "new.py").write_text("new = True\n", encoding="utf-8")
        inventory_project(self.workspace, self.project_id)
        with self.assertRaises(ExecutionFlowError):
            load_current_execution_flow(self.workspace, self.project_id)

    def test_generation_does_not_open_source_bytes_or_write_curated_markdown(self) -> None:
        before = sorted(p.relative_to(self.registration.layout.knowledge_root).as_posix() for p in self.registration.layout.knowledge_root.rglob("*"))
        original = Path.read_bytes
        source_root = self.source.resolve()
        def guarded(path: Path) -> bytes:
            if path.resolve().is_relative_to(source_root):
                raise AssertionError("execution-flow must not open source content")
            return original(path)
        with patch.object(Path, "read_bytes", guarded):
            generate_execution_flow(self.workspace, self.project_id)
        after = sorted(p.relative_to(self.registration.layout.knowledge_root).as_posix() for p in self.registration.layout.knowledge_root.rglob("*"))
        self.assertEqual(before, after)

    def test_core_facade(self) -> None:
        result = ResearchCoreService(self.workspace).execution_flow(self.project_id, observations=self.nodes() + self.edges())
        self.assertEqual(result.execution_flow["kind"], EXECUTION_FLOW_KIND)


if __name__ == "__main__":
    unittest.main()
