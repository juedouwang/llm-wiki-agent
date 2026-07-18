from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.experiment_chains import (
    ClaimLinkObservation,
    ClaimObservation,
    ConfigObservation,
    ExperimentChainError,
    ResultObservation,
    RunObservation,
    generate_experiment_chains,
    load_current_experiment_chains,
    load_experiment_chains,
)
from tools.project_analysis import canonical_json_bytes
from tools.project_inventory import inventory_project
from tools.project_registry import register_project
from tools.research_core import ResearchCoreService


class ExperimentChainsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.source = root / "research"
        self.workspace.mkdir()
        self.source.mkdir()
        (self.source / "configs").mkdir()
        (self.source / "runs").mkdir()
        (self.source / "results").mkdir()
        (self.source / "configs" / "a.yaml").write_text("lr: 0.1\n", encoding="utf-8")
        (self.source / "configs" / "b.yaml").write_text("lr: 0.2\n", encoding="utf-8")
        (self.source / "runs" / "a.log").write_text("run a\n", encoding="utf-8")
        (self.source / "runs" / "b.log").write_text("run b\n", encoding="utf-8")
        (self.source / "results" / "a.csv").write_text(
            "metric\n0.9\n", encoding="utf-8"
        )
        (self.source / "results" / "b.csv").write_text(
            "metric\n0.4\n", encoding="utf-8"
        )
        self.registration = register_project(self.workspace, self.source)
        inventory_project(self.workspace, self.registration.project_id)
        self.project_id = self.registration.project_id
        self.evidence_a = "evd-" + "a" * 64
        self.evidence_b = "evd-" + "b" * 64

    def tearDown(self) -> None:
        self.temp.cleanup()

    def observations(self):
        return [
            ConfigObservation(
                "cfg-a",
                "Configuration A",
                "configs/a.yaml",
                {"lr": 0.1},
                (self.evidence_a,),
            ),
            ConfigObservation(
                "cfg-b",
                "Configuration B",
                "configs/b.yaml",
                {"lr": 0.2},
                (self.evidence_b,),
            ),
            RunObservation(
                "run-a",
                "Run A",
                "cfg-a",
                "runs/a.log",
                "completed",
                {"seed": 1},
                (self.evidence_a,),
            ),
            RunObservation(
                "run-b",
                "Run B",
                "cfg-b",
                "runs/b.log",
                "completed",
                {"seed": 2},
                (self.evidence_b,),
            ),
            ResultObservation(
                "result-a",
                "Result A",
                "run-a",
                "results/a.csv",
                "reported",
                {"seed": 1, "split": "test"},
                {"accuracy": 0.9},
                (self.evidence_a,),
            ),
            ResultObservation(
                "result-b",
                "Result B",
                "run-b",
                "results/b.csv",
                "reported",
                {"seed": 2, "split": "test"},
                {"accuracy": 0.4},
                (self.evidence_b,),
            ),
            ClaimObservation(
                "claim-positive",
                "Method improves accuracy",
                evidence_ids=(self.evidence_a,),
            ),
            ClaimObservation(
                "claim-negative",
                "Method does not improve accuracy",
                evidence_ids=(self.evidence_b,),
            ),
            ClaimLinkObservation(
                "result-a",
                "claim-positive",
                "supports",
                "positive result",
                (self.evidence_a,),
            ),
            ClaimLinkObservation(
                "result-b",
                "claim-positive",
                "contradicts",
                "conflicting result",
                (self.evidence_b,),
            ),
        ]

    def test_conflicting_experiments_coexist_and_preserve_chain_context(self) -> None:
        first = generate_experiment_chains(
            self.workspace, self.project_id, observations=self.observations()
        )
        second = generate_experiment_chains(
            self.workspace,
            self.project_id,
            observations=list(reversed(self.observations())),
        )
        self.assertEqual(first.experiment_chains, second.experiment_chains)
        payload = first.experiment_chains
        self.assertEqual(payload["derivation"]["mode"], "host-observations")
        self.assertEqual(
            {row["relation"] for row in payload["result_claim_links"]},
            {"supports", "contradicts"},
        )
        self.assertEqual(len(payload["chains"]), 2)
        self.assertEqual(
            sorted(row["conditions"]["seed"] for row in payload["chains"]),
            [1, 2],
        )
        self.assertTrue(
            all(row["conditions"]["split"] == "test" for row in payload["chains"])
        )
        self.assertEqual(payload["coverage"]["complete_chains"], 2)
        self.assertEqual(
            load_current_experiment_chains(self.workspace, self.project_id), payload
        )

    def test_result_requires_conditions_metrics_and_evidence(self) -> None:
        with self.assertRaises(ExperimentChainError):
            ResultObservation(
                "r",
                "R",
                "run",
                conditions={},
                metrics={"x": 1},
                evidence_ids=(self.evidence_a,),
            )
        with self.assertRaises(ExperimentChainError):
            ResultObservation(
                "r",
                "R",
                "run",
                conditions={"x": 1},
                metrics={},
                evidence_ids=(self.evidence_a,),
            )
        with self.assertRaises(ExperimentChainError):
            ResultObservation("r", "R", "run", conditions={"x": 1}, metrics={"x": 1})

    def test_manifest_metadata_fallback_does_not_invent_semantic_chain(self) -> None:
        payload = generate_experiment_chains(
            self.workspace, self.project_id
        ).experiment_chains
        self.assertEqual(payload["derivation"]["mode"], "manifest-metadata-fallback")
        self.assertFalse(payload["derivation"]["semantic_input"])
        self.assertEqual(payload["configs"], [])
        self.assertEqual(payload["runs"], [])
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["claims"], [])
        self.assertEqual(payload["result_claim_links"], [])
        self.assertEqual(payload["chains"], [])
        self.assertTrue(
            all(row["certainty"] == "inferred" for row in payload["candidates"])
        )
        self.assertTrue(
            any("No grounded experiment observations" in gap for gap in payload["gaps"])
        )

    def test_invalid_path_endpoint_duplicate_and_invalid_result_fail_closed(
        self,
    ) -> None:
        with self.assertRaises(ExperimentChainError):
            generate_experiment_chains(
                self.workspace,
                self.project_id,
                observations=[ConfigObservation("cfg", "Cfg", "missing.yaml")],
            )
        with self.assertRaises(ExperimentChainError):
            generate_experiment_chains(
                self.workspace,
                self.project_id,
                observations=[
                    ConfigObservation("cfg", "Cfg"),
                    ConfigObservation("cfg", "Duplicate"),
                ],
            )
        with self.assertRaises(ExperimentChainError):
            generate_experiment_chains(
                self.workspace,
                self.project_id,
                observations=[RunObservation("run", "Run", "missing-config")],
            )

    def test_loader_rejects_tamper_duplicate_key_unknown_field_and_noncanonical(
        self,
    ) -> None:
        result = generate_experiment_chains(
            self.workspace, self.project_id, observations=self.observations()
        )
        target = result.experiment_chains_file
        payload = deepcopy(result.experiment_chains)
        payload["chains"][0]["conditions"] = {"tampered": True}
        target.write_bytes(canonical_json_bytes(payload))
        with self.assertRaises(ExperimentChainError):
            load_experiment_chains(target, project_id=self.project_id)

        payload = deepcopy(result.experiment_chains)
        payload["configs"][0]["unexpected"] = True
        target.write_bytes(canonical_json_bytes(payload))
        with self.assertRaises(ExperimentChainError):
            load_experiment_chains(target, project_id=self.project_id)

        target.write_text('{"kind":"x","kind":"x"}\n', encoding="utf-8")
        with self.assertRaises(ExperimentChainError):
            load_experiment_chains(target, project_id=self.project_id)

        target.write_text(
            json.dumps(result.experiment_chains, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(ExperimentChainError):
            load_experiment_chains(target, project_id=self.project_id)

    def test_current_loader_rejects_stale_manifest_and_missing_path(self) -> None:
        generate_experiment_chains(
            self.workspace, self.project_id, observations=self.observations()
        )
        (self.source / "new.txt").write_text("new\n", encoding="utf-8")
        inventory_project(self.workspace, self.project_id)
        with self.assertRaises(ExperimentChainError):
            load_current_experiment_chains(self.workspace, self.project_id)

        # Rebuild against the new Manifest, then tamper a bound path while retaining structure.
        regenerated = generate_experiment_chains(
            self.workspace, self.project_id, observations=self.observations()
        )
        tampered = deepcopy(regenerated.experiment_chains)
        tampered["configs"][0]["path"] = "does-not-exist.txt"
        tampered["artifact_id"] = "experiments-" + "0" * 64
        regenerated.experiment_chains_file.write_bytes(canonical_json_bytes(tampered))
        with self.assertRaises(ExperimentChainError):
            load_current_experiment_chains(self.workspace, self.project_id)

    def test_generation_never_opens_source_content_or_curated_markdown(self) -> None:
        knowledge_root = self.registration.layout.knowledge_root
        before = sorted(
            path.relative_to(knowledge_root).as_posix()
            for path in knowledge_root.rglob("*")
        )
        source_root = self.source.resolve()
        original = Path.read_bytes

        def guarded(path: Path) -> bytes:
            if path.resolve().is_relative_to(source_root):
                raise AssertionError("experiment chains must not open source bytes")
            return original(path)

        with patch.object(Path, "read_bytes", guarded):
            generate_experiment_chains(self.workspace, self.project_id)
        after = sorted(
            path.relative_to(knowledge_root).as_posix()
            for path in knowledge_root.rglob("*")
        )
        self.assertEqual(before, after)

    def test_core_facade(self) -> None:
        result = ResearchCoreService(self.workspace).experiment_chains(
            self.project_id, observations=self.observations()
        )
        self.assertEqual(result.experiment_chains["kind"], "llmwiki-experiment-chains")


if __name__ == "__main__":
    unittest.main()
