from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.evidence_registry import register_evidence
from tools.extraction_schema import LineRangeLocator
from tools.project_analysis import canonical_json_bytes
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
from tools.source_registry import load_source_registry, sync_source_registry


class ResearchLinkageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.source = self.root / "research"
        self.workspace.mkdir()
        self.source.mkdir()
        for directory in ("papers", "src", "data", "configs"):
            (self.source / directory).mkdir()
        (self.source / "papers" / "method.md").write_text(
            "method paper\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.source / "src" / "model.py").write_text(
            "class Model: pass\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.source / "data" / "sample.csv").write_text(
            "x,y\n1,2\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.source / "configs" / "train.yaml").write_text(
            "dataset: ../data/sample.csv\n",
            encoding="utf-8",
            newline="\n",
        )
        self.registration = register_project(self.workspace, self.source)
        self.project_id = self.registration.project_id
        self.inventory = inventory_project(self.workspace, self.project_id)
        sync_source_registry(self.workspace, self.project_id)
        self.manifest = load_project_manifest(
            self.inventory.manifest_file,
            project_id=self.project_id,
            project_root=self.source,
            required_manifest_version="project-inventory-v4",
        )
        self.manifest_sha256 = hashlib.sha256(
            self.inventory.manifest_file.read_bytes()
        ).hexdigest()
        registry = load_source_registry(self.workspace, self.project_id)
        self.sources = registry.current_by_path
        self.paper_evidence = self.register_path_evidence(
            "papers/method.md", "method paper\n"
        )
        self.code_evidence = self.register_path_evidence(
            "src/model.py", "class Model: pass\n"
        )
        self.data_evidence = self.register_path_evidence("data/sample.csv", "x,y\n")
        self.config_evidence = self.register_path_evidence(
            "configs/train.yaml", "dataset: ../data/sample.csv\n"
        )
        self.known_evidence_ids = frozenset(
            {
                self.paper_evidence.evidence_id,
                self.code_evidence.evidence_id,
                self.data_evidence.evidence_id,
                self.config_evidence.evidence_id,
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def register_path_evidence(self, path: str, excerpt: str):
        source = self.sources[path]
        self.assertIsNotNone(source.current_content_hash)
        return register_evidence(
            self.workspace,
            self.project_id,
            source_id=source.source_id,
            content_hash=source.current_content_hash,
            locator=LineRangeLocator(1, 1),
            excerpt=excerpt,
        ).evidence

    def observations(self):
        entities = [
            ResearchEntityObservation(
                "paper",
                "paper",
                "Method Paper",
                "papers/method.md",
                "metadata",
                evidence_ids=(self.paper_evidence.evidence_id,),
            ),
            ResearchEntityObservation(
                "method",
                "method",
                "Contrastive Method",
                assertion_class="paper-claim",
                summary="Paper claims a contrastive objective.",
                evidence_ids=(self.paper_evidence.evidence_id,),
            ),
            ResearchEntityObservation(
                "impl",
                "implementation",
                "Model implementation",
                "src/model.py",
                "implementation",
                evidence_ids=(self.code_evidence.evidence_id,),
            ),
            ResearchEntityObservation(
                "data",
                "dataset",
                "Sample data",
                "data/sample.csv",
                "metadata",
                evidence_ids=(self.data_evidence.evidence_id,),
            ),
            ResearchEntityObservation(
                "config",
                "configuration",
                "Training configuration",
                "configs/train.yaml",
                "implementation",
                evidence_ids=(self.config_evidence.evidence_id,),
            ),
            ResearchEntityObservation(
                "innovation",
                "innovation",
                "Joint encoder",
                assertion_class="inference",
                evidence_ids=(
                    self.paper_evidence.evidence_id,
                    self.code_evidence.evidence_id,
                ),
                uncertainty="Host interpretation across paper and code",
            ),
        ]
        relations = [
            ResearchRelationObservation(
                "paper",
                "method",
                "describes",
                "paper-claim",
                "Paper description",
                (self.paper_evidence.evidence_id,),
            ),
            ResearchRelationObservation(
                "impl",
                "method",
                "implements",
                "implementation",
                "Code implementation",
                (self.code_evidence.evidence_id,),
            ),
            ResearchRelationObservation(
                "impl",
                "data",
                "uses-dataset",
                "implementation",
                "Configured input",
                (self.code_evidence.evidence_id, self.data_evidence.evidence_id),
            ),
            ResearchRelationObservation(
                "impl",
                "config",
                "configured-by",
                "implementation",
                "Runtime configuration",
                (self.code_evidence.evidence_id, self.config_evidence.evidence_id),
            ),
            ResearchRelationObservation(
                "config",
                "data",
                "selects-dataset",
                "implementation",
                "Dataset selection",
                (self.config_evidence.evidence_id, self.data_evidence.evidence_id),
            ),
            ResearchRelationObservation(
                "innovation",
                "impl",
                "derived-from",
                "inference",
                "Cross-source inference",
                (self.paper_evidence.evidence_id, self.code_evidence.evidence_id),
                "inferred",
            ),
        ]
        return entities + relations

    def build(self, observations):
        return build_research_linkage(
            self.manifest,
            project_id=self.project_id,
            manifest_sha256=self.manifest_sha256,
            observations=observations,
            known_evidence_ids=self.known_evidence_ids,
        )

    def test_explicit_linkage_preserves_grounded_provenance_and_links(self) -> None:
        first = generate_research_linkage(
            self.workspace,
            self.project_id,
            observations=self.observations(),
        )
        second = generate_research_linkage(
            self.workspace,
            self.project_id,
            observations=list(reversed(self.observations())),
        )

        self.assertEqual(first.project_id, self.registration.project_id)
        self.assertEqual(first.research_linkage, second.research_linkage)
        payload = first.research_linkage
        self.assertEqual(payload["kind"], RESEARCH_LINKAGE_KIND)
        self.assertEqual(payload["linkage_version"], RESEARCH_LINKAGE_VERSION)
        self.assertGreater(payload["provenance_counts"]["implementation"], 0)
        self.assertGreater(payload["provenance_counts"]["paper-claim"], 0)
        self.assertGreater(payload["provenance_counts"]["inference"], 0)
        self.assertEqual(payload["coverage"]["relations"], 6)
        self.assertEqual(payload["coverage"]["configurations"], 1)
        self.assertEqual(
            {item["kind"] for item in payload["entities"]},
            {
                "paper",
                "method",
                "innovation",
                "dataset",
                "configuration",
                "implementation",
            },
        )
        self.assertEqual(
            load_current_research_linkage(self.workspace, self.project_id),
            payload,
        )

    def test_metadata_fallback_is_explicit_and_does_not_invent_links(self) -> None:
        payload = generate_research_linkage(
            self.workspace,
            self.project_id,
        ).research_linkage

        self.assertEqual(payload["derivation"]["mode"], "manifest-metadata-fallback")
        self.assertFalse(payload["derivation"]["semantic_input"])
        self.assertEqual(payload["relations"], [])
        self.assertIn(
            "No grounded research relationships were supplied",
            payload["gaps"],
        )
        self.assertGreaterEqual(payload["coverage"]["papers"], 1)
        self.assertGreaterEqual(payload["coverage"]["datasets"], 1)
        self.assertGreaterEqual(payload["coverage"]["configurations"], 1)
        self.assertGreaterEqual(payload["coverage"]["implementations"], 1)
        self.assertEqual(payload["provenance_counts"]["implementation"], 0)
        implementation_candidates = [
            item for item in payload["entities"] if item["kind"] == "implementation"
        ]
        self.assertTrue(implementation_candidates)
        self.assertTrue(
            all(item["assertion_class"] == "metadata" for item in implementation_candidates)
        )

    def test_relation_labels_and_inference_certainty_follow_host_boundary(self) -> None:
        relation = ResearchRelationObservation(
            "a",
            "b",
            "host-declared-relation",
            "metadata",
        )
        self.assertEqual(relation.relation, "host-declared-relation")
        for invalid in (
            "Uppercase",
            "contains space",
            "contains/slash",
            "trailing-",
            "a" * 81,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ResearchLinkageError):
                ResearchRelationObservation("a", "b", invalid, "metadata")

        with self.assertRaisesRegex(
            ResearchLinkageError,
            "inference entities cannot claim observed certainty",
        ):
            ResearchEntityObservation(
                "inference",
                "innovation",
                "Inference",
                assertion_class="inference",
                certainty="observed",
            )
        with self.assertRaisesRegex(
            ResearchLinkageError,
            "inference relations cannot claim observed certainty",
        ):
            ResearchRelationObservation(
                "a",
                "b",
                "derived-from",
                "inference",
                certainty="observed",
            )

        payload = self.build(
            [
                {
                    "record_type": "entity",
                    "entity_id": "mapped-inference",
                    "kind": "innovation",
                    "title": "Mapped inference",
                    "assertion_class": "inference",
                }
            ]
        )
        self.assertEqual(payload["entities"][0]["certainty"], "inferred")

    def test_invalid_path_and_missing_relation_endpoint_fail_closed(self) -> None:
        with self.assertRaises(ResearchLinkageError):
            self.build(
                [
                    ResearchEntityObservation(
                        "x",
                        "implementation",
                        "x",
                        "missing.py",
                        "implementation",
                    )
                ]
            )
        with self.assertRaises(ResearchLinkageError):
            self.build(
                [
                    ResearchEntityObservation(
                        "a",
                        "paper",
                        "a",
                        assertion_class="metadata",
                    ),
                    ResearchRelationObservation(
                        "a",
                        "b",
                        "uses",
                        "metadata",
                    ),
                ]
            )

    def test_evidence_bearing_build_and_generation_require_project_registry(self) -> None:
        observation = ResearchEntityObservation(
            "paper",
            "paper",
            "Paper",
            "papers/method.md",
            "metadata",
            evidence_ids=(self.paper_evidence.evidence_id,),
        )
        with self.assertRaisesRegex(
            ResearchLinkageError,
            "requires a bound Evidence registry snapshot",
        ):
            build_research_linkage(
                self.manifest,
                project_id=self.project_id,
                manifest_sha256=self.manifest_sha256,
                observations=[observation],
            )
        with self.assertRaisesRegex(
            ResearchLinkageError,
            "unknown project Evidence",
        ):
            build_research_linkage(
                self.manifest,
                project_id=self.project_id,
                manifest_sha256=self.manifest_sha256,
                observations=[observation],
                known_evidence_ids=(),
            )

        unknown = "evd-" + "f" * 64
        with self.assertRaisesRegex(
            ResearchLinkageError,
            "unknown project Evidence",
        ):
            generate_research_linkage(
                self.workspace,
                self.project_id,
                observations=[replace(observation, evidence_ids=(unknown,))],
            )

    def test_cross_project_evidence_is_rejected(self) -> None:
        other_source_root = self.root / "other-research"
        other_source_root.mkdir()
        (other_source_root / "paper.txt").write_text(
            "other paper\n",
            encoding="utf-8",
            newline="\n",
        )
        other = register_project(
            self.workspace,
            other_source_root,
            project_id="other-linkage-study",
        )
        inventory_project(self.workspace, other.project_id)
        sync_source_registry(self.workspace, other.project_id)
        other_source = load_source_registry(
            self.workspace,
            other.project_id,
        ).current_by_path["paper.txt"]
        self.assertIsNotNone(other_source.current_content_hash)
        foreign_evidence = register_evidence(
            self.workspace,
            other.project_id,
            source_id=other_source.source_id,
            content_hash=other_source.current_content_hash,
            locator=LineRangeLocator(1, 1),
            excerpt="other paper\n",
        ).evidence

        with self.assertRaisesRegex(
            ResearchLinkageError,
            "unknown project Evidence",
        ):
            generate_research_linkage(
                self.workspace,
                self.project_id,
                observations=[
                    ResearchEntityObservation(
                        "foreign-paper",
                        "paper",
                        "Foreign paper",
                        assertion_class="metadata",
                        evidence_ids=(foreign_evidence.evidence_id,),
                    )
                ],
            )

    def test_relation_identity_ignores_mutable_prose_and_rejects_duplicates(self) -> None:
        observations = self.observations()
        relation_index = next(
            index
            for index, item in enumerate(observations)
            if isinstance(item, ResearchRelationObservation)
            and item.relation == "describes"
        )
        original = observations[relation_index]
        assert isinstance(original, ResearchRelationObservation)
        changed = list(observations)
        changed[relation_index] = replace(original, summary="Reworded description")

        first = self.build(observations)
        second = self.build(changed)
        first_relation = next(
            item for item in first["relations"] if item["relation"] == "describes"
        )
        second_relation = next(
            item for item in second["relations"] if item["relation"] == "describes"
        )
        self.assertEqual(first_relation["id"], second_relation["id"])
        self.assertNotEqual(first["artifact_id"], second["artifact_id"])

        entities = [
            item for item in observations if isinstance(item, ResearchEntityObservation)
        ]
        with self.assertRaisesRegex(ResearchLinkageError, "duplicate research relation"):
            self.build(
                [
                    *entities,
                    original,
                    replace(original, summary="Duplicate identity with other prose"),
                ]
            )

    def test_loader_rejects_tamper_duplicate_key_noncanonical_and_v1(self) -> None:
        result = generate_research_linkage(
            self.workspace,
            self.project_id,
            observations=self.observations(),
        )
        payload = deepcopy(result.research_linkage)
        payload["relations"][0]["id"] = "link-" + "f" * 32
        result.research_linkage_file.write_bytes(canonical_json_bytes(payload))
        with self.assertRaises(ResearchLinkageError):
            load_research_linkage(
                result.research_linkage_file,
                project_id=self.project_id,
            )

        result.research_linkage_file.write_text(
            '{"kind":"x","kind":"x"}\n',
            encoding="utf-8",
        )
        with self.assertRaises(ResearchLinkageError):
            load_research_linkage(
                result.research_linkage_file,
                project_id=self.project_id,
            )

        result.research_linkage_file.write_text(
            json.dumps(result.research_linkage, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(ResearchLinkageError):
            load_research_linkage(
                result.research_linkage_file,
                project_id=self.project_id,
            )

        v1 = deepcopy(result.research_linkage)
        v1["linkage_version"] = "research-linkage-v1"
        result.research_linkage_file.write_bytes(canonical_json_bytes(v1))
        with self.assertRaisesRegex(ResearchLinkageError, "unsupported"):
            load_research_linkage(
                result.research_linkage_file,
                project_id=self.project_id,
            )

    def test_current_loader_rejects_stale_manifest_and_missing_evidence_registry(self) -> None:
        generate_research_linkage(self.workspace, self.project_id)
        (self.source / "new.md").write_text("new\n", encoding="utf-8")
        inventory_project(self.workspace, self.project_id)
        with self.assertRaisesRegex(ResearchLinkageError, "stale"):
            load_current_research_linkage(self.workspace, self.project_id)

        inventory_project(self.workspace, self.project_id)
        generate_research_linkage(
            self.workspace,
            self.project_id,
            observations=self.observations(),
        )
        self.registration.layout.evidence_file.unlink()
        with self.assertRaisesRegex(
            ResearchLinkageError,
            "could not load the bound Evidence registry",
        ):
            load_current_research_linkage(self.workspace, self.project_id)

    def test_generation_never_opens_source_content_or_curated_markdown(self) -> None:
        before = sorted(
            path.relative_to(self.registration.layout.knowledge_root).as_posix()
            for path in self.registration.layout.knowledge_root.rglob("*")
        )
        source_root = self.source.resolve()
        original = Path.read_bytes

        def guarded(path: Path) -> bytes:
            if path.resolve().is_relative_to(source_root):
                raise AssertionError("research linkage must not open source bytes")
            return original(path)

        with patch.object(Path, "read_bytes", guarded):
            generate_research_linkage(
                self.workspace,
                self.project_id,
                observations=self.observations(),
            )
        after = sorted(
            path.relative_to(self.registration.layout.knowledge_root).as_posix()
            for path in self.registration.layout.knowledge_root.rglob("*")
        )
        self.assertEqual(before, after)

    def test_core_facade(self) -> None:
        result = ResearchCoreService(self.workspace).research_linkage(
            self.project_id,
            observations=self.observations(),
        )
        self.assertEqual(result.research_linkage["kind"], RESEARCH_LINKAGE_KIND)
        self.assertEqual(result.project_id, self.registration.project_id)


if __name__ == "__main__":
    unittest.main()
