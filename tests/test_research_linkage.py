from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.project_inventory import inventory_project, load_project_manifest
from tools.project_registry import register_project
from tools.research_core import ResearchCoreService
from tools.research_linkage import (
    RESEARCH_LINKAGE_KIND,
    RESEARCH_LINKAGE_VERSION,
    ResearchEntityObservation,
    ResearchLinkageError,
    ResearchRelationObservation,
    build_research_linkage,
    generate_research_linkage,
    load_current_research_linkage,
    load_research_linkage,
)


class ResearchLinkageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.source = root / "research"
        self.workspace.mkdir()
        self.source.mkdir()
        (self.source / "papers").mkdir()
        (self.source / "src").mkdir()
        (self.source / "data").mkdir()
        (self.source / "papers" / "method.md").write_text("method paper\n", encoding="utf-8")
        (self.source / "src" / "model.py").write_text("class Model: pass\n", encoding="utf-8")
        (self.source / "data" / "sample.csv").write_text("x,y\n1,2\n", encoding="utf-8")
        self.registration = register_project(self.workspace, self.source)
        self.project_id = self.registration.project_id
        self.inventory = inventory_project(self.workspace, self.project_id)
        self.manifest = load_project_manifest(self.inventory.manifest_file, project_id=self.project_id, project_root=self.source, required_manifest_version="project-inventory-v4")
        self.a = "evd-" + "a" * 64
        self.b = "evd-" + "b" * 64

    def tearDown(self) -> None:
        self.temp.cleanup()

    def observations(self):
        entities = [
            ResearchEntityObservation("paper", "paper", "Method Paper", "papers/method.md", "metadata", evidence_ids=(self.a,)),
            ResearchEntityObservation("method", "method", "Contrastive Method", assertion_class="paper-claim", summary="Paper claims a contrastive objective.", evidence_ids=(self.a,)),
            ResearchEntityObservation("impl", "implementation", "Model implementation", "src/model.py", "implementation", evidence_ids=(self.b,)),
            ResearchEntityObservation("data", "dataset", "Sample data", "data/sample.csv", "metadata", evidence_ids=(self.b,)),
            ResearchEntityObservation("innovation", "innovation", "Joint encoder", assertion_class="inference", certainty="inferred", uncertainty="Host interpretation across paper and code"),
        ]
        relations = [
            ResearchRelationObservation("paper", "method", "describes", "paper-claim", "Paper description", (self.a,)),
            ResearchRelationObservation("impl", "method", "implements", "implementation", "Code implementation", (self.b,)),
            ResearchRelationObservation("impl", "data", "uses", "implementation", "Configured input", (self.b,)),
            ResearchRelationObservation("innovation", "impl", "derived-from", "inference", "Cross-source inference", (self.a, self.b), "inferred"),
        ]
        return entities + relations

    def test_explicit_linkage_preserves_provenance_and_links(self) -> None:
        first = generate_research_linkage(self.workspace, self.project_id, observations=self.observations())
        second = generate_research_linkage(self.workspace, self.project_id, observations=list(reversed(self.observations())))
        self.assertEqual(first.research_linkage, second.research_linkage)
        payload = first.research_linkage
        self.assertEqual(payload["kind"], RESEARCH_LINKAGE_KIND)
        self.assertEqual(payload["linkage_version"], RESEARCH_LINKAGE_VERSION)
        self.assertGreater(payload["provenance_counts"]["implementation"], 0)
        self.assertGreater(payload["provenance_counts"]["paper-claim"], 0)
        self.assertGreater(payload["provenance_counts"]["inference"], 0)
        self.assertEqual(payload["coverage"]["relations"], 4)
        self.assertEqual(load_current_research_linkage(self.workspace, self.project_id), payload)

    def test_metadata_fallback_is_explicit_and_does_not_invent_links(self) -> None:
        payload = generate_research_linkage(self.workspace, self.project_id).research_linkage
        self.assertEqual(payload["derivation"]["mode"], "manifest-metadata-fallback")
        self.assertFalse(payload["derivation"]["semantic_input"])
        self.assertEqual(payload["relations"], [])
        self.assertIn("No grounded research relationships were supplied", payload["gaps"])
        self.assertGreaterEqual(payload["coverage"]["papers"], 1)
        self.assertGreaterEqual(payload["coverage"]["datasets"], 1)

    def test_invalid_path_endpoint_and_inference_claim_fail_closed(self) -> None:
        with self.assertRaises(ResearchLinkageError):
            build_research_linkage(self.manifest, project_id=self.project_id, manifest_sha256="0" * 64, observations=[ResearchEntityObservation("x", "implementation", "x", "missing.py", "implementation")])
        with self.assertRaises(ResearchLinkageError):
            ResearchRelationObservation("a", "b", "derived-from", "inference", certainty="observed")
        with self.assertRaises(ResearchLinkageError):
            build_research_linkage(self.manifest, project_id=self.project_id, manifest_sha256="0" * 64, observations=[ResearchEntityObservation("a", "paper", "a", assertion_class="metadata"), ResearchRelationObservation("a", "b", "uses", "metadata")])

    def test_loader_rejects_stable_id_tamper_duplicate_key_and_noncanonical(self) -> None:
        result = generate_research_linkage(self.workspace, self.project_id, observations=self.observations())
        payload = deepcopy(result.research_linkage)
        payload["relations"][0]["id"] = "link-" + "f" * 32
        result.research_linkage_file.write_bytes((json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())
        with self.assertRaises(ResearchLinkageError):
            load_research_linkage(result.research_linkage_file, project_id=self.project_id)
        result.research_linkage_file.write_text('{"kind":"x","kind":"x"}\n', encoding="utf-8")
        with self.assertRaises(ResearchLinkageError):
            load_research_linkage(result.research_linkage_file, project_id=self.project_id)

    def test_current_loader_rejects_stale_manifest(self) -> None:
        generate_research_linkage(self.workspace, self.project_id)
        (self.source / "new.md").write_text("new\n", encoding="utf-8")
        inventory_project(self.workspace, self.project_id)
        with self.assertRaises(ResearchLinkageError):
            load_current_research_linkage(self.workspace, self.project_id)

    def test_generation_never_opens_source_content_or_curated_markdown(self) -> None:
        before = sorted(p.relative_to(self.registration.layout.knowledge_root).as_posix() for p in self.registration.layout.knowledge_root.rglob("*"))
        source_root = self.source.resolve()
        original = Path.read_bytes
        def guarded(path: Path) -> bytes:
            if path.resolve().is_relative_to(source_root):
                raise AssertionError("research linkage must not open source bytes")
            return original(path)
        with patch.object(Path, "read_bytes", guarded):
            generate_research_linkage(self.workspace, self.project_id)
        after = sorted(p.relative_to(self.registration.layout.knowledge_root).as_posix() for p in self.registration.layout.knowledge_root.rglob("*"))
        self.assertEqual(before, after)

    def test_core_facade(self) -> None:
        result = ResearchCoreService(self.workspace).research_linkage(self.project_id, observations=self.observations())
        self.assertEqual(result.research_linkage["kind"], RESEARCH_LINKAGE_KIND)


if __name__ == "__main__":
    unittest.main()
