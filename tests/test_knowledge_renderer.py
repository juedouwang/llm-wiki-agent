from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from tools.controlled_markdown_persistence import (
    CONTROLLED_MARKDOWN_AUDIT_FILE,
    TrustedHostSessionContext,
)
from tools.knowledge_artifacts import (
    KNOWLEDGE_PAGE_KIND,
    KNOWLEDGE_SCHEMA_VERSION,
    artifact_contract_for_path,
    parse_knowledge_page,
    serialize_knowledge_frontmatter,
)
from tools.knowledge_renderer import (
    PRODUCT_ARTIFACT_SPECS,
    build_project_knowledge_pages,
    render_project_knowledge,
)
from tools.project_registry import register_project
from tools.research_core import ResearchCoreService


PROJECT_ID = "e-07-renderer"
SOURCE_ID = "src-0123456789abcdef0123456789abcdef"
EVIDENCE_ID = "evd-" + "a" * 64
SECOND_EVIDENCE_ID = "evd-" + "b" * 64
MANIFEST_HASH = "f" * 64


class KnowledgeRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.source = self.root / "research-project"
        self.knowledge_parent = self.root / "personal-knowledge"
        self.source.mkdir()
        (self.source / "src").mkdir()
        (self.source / "src" / "train.py").write_text("print('train')\n", encoding="utf-8")
        (self.source / "README.md").write_text("research fixture\n", encoding="utf-8")
        (self.source / "results.json").write_text('{"score": 0.9}\n', encoding="utf-8")
        self.registration = register_project(
            self.workspace,
            self.source,
            project_id=PROJECT_ID,
            knowledge_root=self.knowledge_parent,
            final_goal="Reproduce the baseline and test the proposed method",
            current_stage="initial understanding",
            important_question="Which result is robust under the held-out split?",
            deadline="2026-08-01",
            daily_available_hours=2.5,
        )
        self.source_snapshot_before = self.source_snapshot()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def source_snapshot(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in sorted(self.source.rglob("*")):
            if path.is_file():
                result[path.relative_to(self.source).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return result

    @staticmethod
    def evidence_ref(evidence_id: str = EVIDENCE_ID, stance: str = "context") -> dict[str, str]:
        return {"evidence_id": evidence_id, "stance": stance}

    def machine_fixture(self) -> dict[str, dict[str, Any]]:
        return {
            "project_map": {
                "schema_version": 1,
                "derivation": {"source_content_read": False, "llm_used": False},
                "manifest": {
                    "ordinary_file_count": 3,
                    "ordinary_byte_count": 100,
                    "sha256": MANIFEST_HASH,
                },
                "directories": [
                    {"path": ".", "recursive_file_count": 3, "recursive_byte_count": 100},
                    {"path": "src", "recursive_file_count": 1, "recursive_byte_count": 15},
                ],
                "distributions": {
                    "languages": [{"value": "Python", "file_count": 1, "byte_count": 15}]
                },
                "candidates": {
                    "entrypoints": [{"path": "src/train.py", "reason_codes": ["name"]}],
                    "dependencies": [{"path": "requirements.txt", "reason_codes": ["name"]}],
                    "configurations": [{"path": "configs/base.yaml", "reason_codes": ["name"]}],
                    "run_scripts": [{"path": "scripts/run.sh", "reason_codes": ["name"]}],
                    "key_files": [{"path": "src/train.py", "reason_codes": ["entrypoint"], "rank": 1}],
                },
            },
            "hierarchical": {
                "schema_version": 1,
                "derivation": {"source_content_read": False, "semantic_input": False},
                "project": {"summary": "A bounded training and evaluation fixture."},
                "coverage": {"metadata_only_files": 2},
                "omissions": {"semantic_files": 2},
                "files": [],
                "modules": [],
                "chunks": [],
            },
            "execution_flow": {
                "schema_version": 1,
                "summary": "The training entrypoint feeds a metric-producing evaluation step.",
                "entrypoints": ["train"],
                "nodes": [
                    {"id": "train", "label": "train", "kind": "entrypoint", "certainty": "observed"},
                    {"id": "evaluate", "label": "evaluate", "kind": "module", "certainty": "inferred"},
                ],
                "edges": [
                    {"source": "train", "relation": "calls", "target": "evaluate", "certainty": "observed"}
                ],
                "uncertainties": ["Dataset preparation was not observed."],
            },
            "research_linkage": {
                "schema_version": 1,
                "derivation": {"semantic_input": True},
                "entities": [
                    {
                        "id": "paper-a",
                        "kind": "paper",
                        "title": "Same Title",
                        "summary": "Paper A claim.",
                        "path": "papers/paper-a.pdf",
                        "certainty": "observed",
                        "assertion_class": "paper-claim",
                        "source_ids": [SOURCE_ID],
                        "evidence_refs": [self.evidence_ref()],
                    },
                    {
                        "id": "paper-b",
                        "kind": "paper",
                        "title": "Same Title",
                        "summary": "Paper B claim.",
                        "path": "papers/paper-b.pdf",
                        "certainty": "observed",
                        "assertion_class": "paper-claim",
                        "source_ids": [SOURCE_ID],
                        "evidence_refs": [self.evidence_ref(SECOND_EVIDENCE_ID, "supporting")],
                    },
                    {
                        "id": "method-1",
                        "kind": "method",
                        "title": "Proposed method",
                        "summary": "A host-declared method observation.",
                        "path": "src/train.py",
                        "certainty": "inferred",
                        "assertion_class": "implementation",
                    },
                    {
                        "id": "dataset-1",
                        "kind": "dataset",
                        "title": "Held-out dataset",
                        "summary": "Dataset source and split observation.",
                        "path": "data/test.csv",
                        "certainty": "uncertain",
                        "assertion_class": "metadata",
                    },
                ],
                "gaps": ["Confirm the dataset preprocessing version."],
            },
            "experiment_chains": {
                "schema_version": 1,
                "derivation": {"semantic_input": True},
                "configs": [
                    {"id": "cfg-1", "title": "Baseline config", "path": "configs/base.yaml", "parameters": {"lr": 0.1}}
                ],
                "runs": [
                    {
                        "id": "run-1",
                        "title": "Baseline run",
                        "config_id": "cfg-1",
                        "status": "completed",
                        "path": "runs/baseline.log",
                        "conditions": {"split": "test", "seed": 1},
                    }
                ],
                "results": [
                    {
                        "id": "result-1",
                        "title": "Baseline result",
                        "conditions": {"split": "test"},
                        "metrics": {"accuracy": 0.9},
                        "evidence_ids": [EVIDENCE_ID],
                    }
                ],
                "claims": [
                    {
                        "id": "claim-1",
                        "title": "Baseline claim",
                        "statement": "The baseline reaches the observed score.",
                        "certainty": "uncertain",
                        "evidence_ids": [EVIDENCE_ID],
                    }
                ],
                "chains": [],
                "coverage": {"complete_chains": 1, "results": 1, "claims": 1, "unlinked_results": 0},
            },
        }

    def render_kwargs(self) -> dict[str, Any]:
        return {
            "rendered_at": "2026-07-18T12:00:00Z",
            "plan_date": "2026-07-18",
            "machine_artifacts": self.machine_fixture(),
        }

    def test_fixed_fixture_emits_fifteen_deliverables_and_valid_v2_pages(self) -> None:
        pages = build_project_knowledge_pages(
            self.workspace, PROJECT_ID, **self.render_kwargs()
        )
        by_path = {page.path: page for page in pages}
        expected = {spec["path"] for spec in PRODUCT_ARTIFACT_SPECS}
        expected.update({"index.md", "plans/daily/2026-07-18.md"})
        self.assertTrue(expected.issubset(by_path))
        self.assertIn("Same Title", by_path["papers/index.md"].body)
        self.assertGreaterEqual(len(pages), len(expected))
        for path, page in by_path.items():
            parsed = parse_knowledge_page(page.payload, path=path)
            contract = artifact_contract_for_path(path)
            self.assertEqual(parsed.frontmatter.schema_version, KNOWLEDGE_SCHEMA_VERSION)
            self.assertEqual(parsed.frontmatter.kind, KNOWLEDGE_PAGE_KIND)
            self.assertEqual(parsed.frontmatter.project_id, PROJECT_ID)
            self.assertEqual(parsed.frontmatter.artifact_type, contract.artifact_type)
            self.assertEqual(page.artifact_type, contract.artifact_type)
            self.assertTrue(parsed.body.strip(), path)

    def test_missing_artifacts_are_nonempty_explicit_placeholders(self) -> None:
        pages = build_project_knowledge_pages(
            self.workspace,
            PROJECT_ID,
            rendered_at="2026-07-18T12:00:00Z",
            plan_date="2026-07-18",
            machine_artifacts={},
        )
        by_path = {page.path: page for page in pages}
        for path in (
            "project-map.md",
            "reproduction.md",
            "architecture.md",
            "papers/index.md",
            "methods/index.md",
            "datasets/index.md",
            "experiments/index.md",
            "results/index.md",
            "claims/index.md",
        ):
            self.assertEqual(by_path[path].completeness, "missing", path)
            self.assertIn("DRAFT", by_path[path].body, path)
            self.assertIn(by_path[path].reason_code, by_path[path].body, path)
        for page in pages:
            self.assertTrue(page.body.strip(), page.path)

    def test_curated_output_sanitizes_paths_hashes_and_nul_without_reading_source(self) -> None:
        dangerous = self.machine_fixture()
        dangerous["project_map"]["candidates"]["entrypoints"].append(
            {"path": str(self.source / "secret.txt"), "reason_codes": ["absolute"]}
        )
        dangerous["project_map"]["candidates"]["key_files"].append(
            {"path": "/private/secret.txt", "reason_codes": ["absolute"]}
        )
        dangerous["execution_flow"]["summary"] = f"secret\x00 {self.source} {MANIFEST_HASH}"
        dangerous["research_linkage"]["entities"][0]["summary"] = f"{self.source} {MANIFEST_HASH}"
        pages = build_project_knowledge_pages(
            self.workspace,
            PROJECT_ID,
            rendered_at="2026-07-18T12:00:00Z",
            machine_artifacts=dangerous,
        )
        all_bytes = b"\n".join(page.payload for page in pages)
        all_text = all_bytes.decode("utf-8")
        self.assertNotIn(str(self.source), all_text)
        self.assertNotIn(MANIFEST_HASH, all_text)
        self.assertNotIn("\x00", all_text)
        self.assertEqual(self.source_snapshot_before, self.source_snapshot())

    def test_renderer_does_not_write_source_and_supports_custom_knowledge_root(self) -> None:
        result = render_project_knowledge(
            self.workspace,
            PROJECT_ID,
            **self.render_kwargs(),
            persist=False,
        )
        self.assertEqual(result.status, "succeeded")
        self.assertTrue(result.knowledge_root is None or "research-project" not in result.knowledge_root)
        self.assertEqual(self.source_snapshot_before, self.source_snapshot())
        self.assertFalse((self.source / "wiki").exists())
        self.assertFalse((self.source / f"{self.source.name}-wiki").exists())
        self.assertFalse((self.workspace / "wiki").exists())
        self.assertFalse(list(self.registration.layout.knowledge_root.rglob("*.md")))

    def _host_context(self, session: str = "session-1") -> TrustedHostSessionContext:
        return TrustedHostSessionContext(
            host_id="codex",
            actor_type="host-agent",
            actor_id="research-agent",
            session_id=session,
        )

    def _persist(self, *, decision_prefix: str = "render-1", host_data: dict[str, Any] | None = None):
        return render_project_knowledge(
            self.workspace,
            PROJECT_ID,
            **self.render_kwargs(),
            host_data=host_data,
            persist=True,
            host_context=self._host_context(),
            decision_id_prefix=decision_prefix,
            authorized_at="2026-07-18T12:00:00Z",
        )

    def test_generated_pages_update_and_f05b_audit_path_is_used(self) -> None:
        first = self._persist()
        self.assertEqual(first.status, "succeeded")
        self.assertEqual(len(first.changed_paths), len(first.pages))
        overview = self.registration.layout.knowledge_root / "overview.md"
        before = overview.read_text(encoding="utf-8")
        host_data = {
            "pages": {
                "overview.md": {
                    "body": "# Regenerated overview\n\nA new bounded host observation.\n",
                    "reason_code": "host-refresh",
                }
            }
        }
        second = self._persist(decision_prefix="render-2", host_data=host_data)
        observation = next(item for item in second.pages if item.path == "overview.md")
        self.assertEqual(observation.action, "written")
        self.assertNotEqual(before, overview.read_text(encoding="utf-8"))
        audit = self.registration.layout.indexes_dir / CONTROLLED_MARKDOWN_AUDIT_FILE
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertIn("prepared", [record["phase"] for record in records])
        self.assertIn("committed", [record["phase"] for record in records])
        self.assertEqual(self.source_snapshot_before, self.source_snapshot())

    def test_mixed_user_region_bytes_survive_regeneration(self) -> None:
        self._persist()
        goals = self.registration.layout.knowledge_root / "goals.md"
        current = parse_knowledge_page(goals.read_bytes(), path="goals.md")
        generated = "# Changed generated goals\n\n## User-confirmed goals\n\n"
        exact_user = "USER bytes: 你好\r\nline two\r\n"
        mixed_body = (
            generated
            + '<!-- llmwiki:user-region:start id="user-goals" -->\n'
            + exact_user
            + '<!-- llmwiki:user-region:end id="user-goals" -->\n'
        )
        goals.write_bytes(
            (serialize_knowledge_frontmatter(current.frontmatter.as_dict(), path="goals.md") + mixed_body).encode("utf-8")
        )
        host_data = {
            "pages": {
                "goals.md": {
                    "body": '# New generated skeleton\n\n<!-- llmwiki:user-region:start id="user-goals" -->\nproposed\n<!-- llmwiki:user-region:end id="user-goals" -->\n',
                    "ownership": "mixed",
                }
            }
        }
        result = self._persist(decision_prefix="render-mixed", host_data=host_data)
        observation = next(item for item in result.pages if item.path == "goals.md")
        self.assertEqual(observation.action, "written")
        output = goals.read_bytes().decode("utf-8")
        self.assertIn(exact_user, output)
        self.assertNotIn("proposed", output)
        self.assertIn("# New generated skeleton", output)

    def test_user_owned_page_is_protected(self) -> None:
        self._persist()
        overview = self.registration.layout.knowledge_root / "overview.md"
        current = parse_knowledge_page(overview.read_bytes(), path="overview.md")
        frontmatter = current.frontmatter.as_dict()
        frontmatter["ownership"] = "user"
        user_bytes = (serialize_knowledge_frontmatter(frontmatter, path="overview.md") + "# User page\n").encode("utf-8")
        overview.write_bytes(user_bytes)
        result = render_project_knowledge(
            self.workspace,
            PROJECT_ID,
            **self.render_kwargs(),
            persist=False,
        )
        observation = next(item for item in result.pages if item.path == "overview.md")
        self.assertEqual(observation.action, "protected")
        self.assertEqual(observation.reason_code, "user-owned-page")
        self.assertEqual(overview.read_bytes(), user_bytes)

    def test_legacy_future_and_malformed_pages_are_protected(self) -> None:
        self._persist()
        root = self.registration.layout.knowledge_root
        legacy = {
            "schema_version": 1,
            "kind": KNOWLEDGE_PAGE_KIND,
            "project_id": PROJECT_ID,
            "artifact_type": "overview",
            "title": "Legacy",
            "status": "draft",
            "ownership": "generated",
            "source_ids": [],
            "evidence_ids": [],
            "generated_at": "2026-07-18T12:00:00Z",
            "updated_at": "2026-07-18T12:00:00Z",
            "last_verified_at": None,
        }
        future = dict(legacy)
        future["schema_version"] = 99
        paths = {
            "overview.md": (self._raw_page(legacy, "# legacy\n"), "legacy-schema-v1-read-only"),
            "project-map.md": (self._raw_page(future, "# future\n"), "unsupported-or-future-schema"),
            "reproduction.md": (b"not markdown frontmatter\n", "malformed-knowledge-page"),
        }
        for path, (payload, reason) in paths.items():
            (root / path).write_bytes(payload)
        result = render_project_knowledge(
            self.workspace,
            PROJECT_ID,
            **self.render_kwargs(),
            persist=False,
        )
        for path, (_payload, reason) in paths.items():
            observation = next(item for item in result.pages if item.path == path)
            self.assertEqual(observation.action, "protected", path)
            self.assertEqual(observation.reason_code, reason, path)

    @staticmethod
    def _raw_page(frontmatter: dict[str, Any], body: str) -> bytes:
        dumped = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False, width=1000)
        return ("---\n" + dumped + "---\n" + body).encode("utf-8")

    def test_non_draft_lifecycle_page_is_protected(self) -> None:
        self._persist()
        overview = self.registration.layout.knowledge_root / "overview.md"
        current = parse_knowledge_page(overview.read_bytes(), path="overview.md")
        frontmatter = current.frontmatter.as_dict()
        frontmatter["status"] = "stale"
        stale_bytes = (serialize_knowledge_frontmatter(frontmatter, path="overview.md") + current.body).encode("utf-8")
        overview.write_bytes(stale_bytes)
        result = render_project_knowledge(
            self.workspace,
            PROJECT_ID,
            **self.render_kwargs(),
            persist=False,
        )
        observation = next(item for item in result.pages if item.path == "overview.md")
        self.assertEqual(observation.action, "protected")
        self.assertEqual(observation.reason_code, "non-draft-lifecycle-page")

    def test_unchanged_render_is_idempotent(self) -> None:
        self._persist(decision_prefix="render-idempotent-1")
        result = render_project_knowledge(
            self.workspace,
            PROJECT_ID,
            rendered_at="2026-07-18T12:05:00Z",
            plan_date="2026-07-18",
            machine_artifacts=self.machine_fixture(),
            persist=False,
        )
        self.assertEqual(result.status, "succeeded")
        self.assertFalse(result.changed_paths)
        self.assertTrue(all(item.action == "unchanged" for item in result.pages))

    def test_persist_requires_explicit_host_authorization(self) -> None:
        with self.assertRaises(ValueError):
            render_project_knowledge(
                self.workspace,
                PROJECT_ID,
                **self.render_kwargs(),
                persist=True,
            )

    def test_same_title_entities_keep_distinct_identity_bound_detail_pages(self) -> None:
        pages = build_project_knowledge_pages(
            self.workspace, PROJECT_ID, **self.render_kwargs()
        )
        same_title = [page.path for page in pages if page.path.startswith("papers/") and page.path != "papers/index.md"]
        self.assertEqual(len(same_title), 2)
        self.assertNotEqual(*same_title)

    def test_c07_remains_deferred_and_not_claimed_processed(self) -> None:
        pages = build_project_knowledge_pages(
            self.workspace,
            PROJECT_ID,
            rendered_at="2026-07-18T12:00:00Z",
            machine_artifacts={},
        )
        risks = next(page for page in pages if page.path == "risks.md")
        self.assertIn("C-07", risks.body)
        self.assertIn("deferred", risks.body)
        self.assertIn("not claimed as processed", risks.body)
        self.assertNotIn("C-07: complete", risks.body)
        self.assertNotIn("C-07: processed", risks.body)

    def test_core_facade_exposes_knowledge_render(self) -> None:
        service = ResearchCoreService(self.workspace)
        result = service.knowledge_render(
            PROJECT_ID,
            rendered_at="2026-07-18T12:00:00Z",
            machine_artifacts={},
            persist=False,
        )
        self.assertEqual(result.project_id, PROJECT_ID)
        self.assertEqual(result.status, "succeeded")
        self.assertIs(service.render_knowledge.__func__, service.knowledge_render.__func__)


if __name__ == "__main__":
    unittest.main()
