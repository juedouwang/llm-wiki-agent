from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import unittest

from tools.evidence_registry import register_evidence
from tools.extraction_schema import LineRangeLocator
from tools.file_state import FileState, file_state_from_dict
from tools.knowledge_artifacts import (
    KNOWLEDGE_PAGE_KIND,
    KNOWLEDGE_SCHEMA_VERSION,
    serialize_knowledge_frontmatter,
)
from tools.knowledge_renderer import PRODUCT_ARTIFACT_SPECS
from tools.project_registry import load_registered_project, register_project
from tools.research_cockpit import (
    CockpitAssetError,
    ResearchCockpit,
    ResearchCockpitError,
    create_server,
    make_handler,
    request_host_allowed,
    validate_bind_host,
)
from tools.research_core import ResearchCoreService
from tools.source_registry import load_source_registry, sync_source_registry


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "minimal_research_project"


def source_snapshot(
    root: Path,
) -> tuple[
    dict[str, tuple[int, int]],
    dict[str, tuple[str, int, int, int]],
]:
    directories: dict[str, tuple[int, int]] = {}
    files: dict[str, tuple[str, int, int, int]] = {}
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        base = Path(directory)
        base_stat = base.stat()
        relative_directory = "." if base == root else base.relative_to(root).as_posix()
        directories[relative_directory] = (
            base_stat.st_mtime_ns,
            stat.S_IMODE(base_stat.st_mode),
        )
        for filename in filenames:
            path = base / filename
            metadata = path.stat()
            files[path.relative_to(root).as_posix()] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                metadata.st_size,
                metadata.st_mtime_ns,
                stat.S_IMODE(metadata.st_mode),
            )
    return directories, files


def canonical_jsonl(rows: list[dict[str, object]]) -> bytes:
    return b"".join(
        (
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        for row in rows
    )


def set_manifest_file_state(
    manifest_file: Path,
    relative_path: str,
    replacement: FileState,
) -> None:
    rows = [
        json.loads(line)
        for line in manifest_file.read_text(encoding="utf-8").splitlines()
    ]
    target = next(row for row in rows if row.get("path") == relative_path)
    previous = file_state_from_dict(target["file_state"])
    target["file_state"] = replacement.as_dict()
    summary = rows[0]["file_state_summary"]
    for field, old_value, new_value in (
        (
            "processing_statuses",
            previous.processing_status,
            replacement.processing_status,
        ),
        ("read_depths", previous.read_depth, replacement.read_depth),
        ("reasons", previous.reason_code, replacement.reason_code),
    ):
        counts = summary[field]
        counts[old_value] -= 1
        if counts[old_value] == 0:
            del counts[old_value]
        counts[new_value] = counts.get(new_value, 0) + 1
    manifest_file.write_bytes(canonical_jsonl(rows))


def write_claim_page(
    path: Path,
    *,
    project_id: str,
    source_id: str,
    evidence_id: str,
    body: str,
) -> None:
    relative_path = f"claims/{path.name}"
    frontmatter = {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "kind": KNOWLEDGE_PAGE_KIND,
        "project_id": project_id,
        "artifact_type": "claim",
        "title": "Cockpit Evidence Trace",
        "status": "draft",
        "ownership": "generated",
        "source_ids": [source_id],
        "evidence_refs": [
            {
                "evidence_id": evidence_id,
                "stance": "supporting",
            }
        ],
        "generated_at": "2026-07-19T00:00:00Z",
        "updated_at": "2026-07-19T00:00:00Z",
        "last_verified_at": None,
    }
    page = serialize_knowledge_frontmatter(frontmatter, path=relative_path) + body
    path.write_text(page, encoding="utf-8", newline="\n")


class RegisteredOnlyCockpitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.knowledge_parent = self.root / "knowledge"
        self.source = self.root / "source"
        shutil.copytree(FIXTURE_ROOT, self.source)
        self.before = source_snapshot(self.source)
        self.registration = register_project(
            self.workspace,
            self.source,
            project_id="registered-only-study",
            name="Registered Only Study",
            knowledge_root=self.knowledge_parent,
        )
        self.cockpit = ResearchCockpit(self.workspace)

    def test_registration_only_exposes_all_fifteen_artifacts_as_explicit_draft_gaps(
        self,
    ) -> None:
        payload = self.cockpit.project_snapshot(self.registration.project_id)

        self.assertEqual(len(payload["artifacts"]), len(PRODUCT_ARTIFACT_SPECS))
        self.assertEqual(
            [item["path"] for item in payload["artifacts"]],
            [item["path"] for item in PRODUCT_ARTIFACT_SPECS],
        )
        self.assertTrue(
            all(
                item["availability"] == "missing"
                and item["status"] == "DRAFT"
                and item["reason_code"] == "knowledge-missing"
                for item in payload["artifacts"]
            )
        )
        self.assertIsNone(payload["coverage"])
        self.assertEqual(payload["files"]["count"], 0)
        self.assertEqual(payload["planning"]["tasks"], [])
        self.assertIsNone(payload["planning"]["goal"])
        self.assertEqual(
            {item["reason_code"] for item in payload["planning"]["gaps"]},
            {"machine-artifact-missing"},
        )
        self.assertEqual(payload["daily_plans"]["today"]["status"], "DRAFT")
        self.assertEqual(payload["runs"]["items"], [])
        self.assertEqual(self.before, source_snapshot(self.source))

    def test_capability_contract_keeps_query_and_c07_unavailable(self) -> None:
        capabilities = self.cockpit.project_snapshot(self.registration.project_id)[
            "capabilities"
        ]
        self.assertTrue(capabilities["read_only"])
        self.assertFalse(capabilities["editing"]["available"])
        self.assertEqual(capabilities["editing"]["available_after"], "J-04")
        self.assertEqual(
            capabilities["query"],
            {
                "available": False,
                "error": "capability-unavailable",
                "available_after": "G-04",
            },
        )
        self.assertEqual(capabilities["c07"], "deferred/not_started")
        self.assertFalse(capabilities["source_bytes_reopened"])


class CompleteCockpitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp_dir.name)
        cls.workspace = cls.root / "workspace"
        cls.knowledge_parent = cls.root / "knowledge"
        cls.source = cls.root / "minimal-research-project"
        shutil.copytree(FIXTURE_ROOT, cls.source)
        cls.before = source_snapshot(cls.source)
        cls.result = ResearchCoreService(cls.workspace).project_understand(
            cls.source,
            project_id="cockpit-complete-study",
            name="Cockpit Complete Study",
            knowledge_root=cls.knowledge_parent,
            final_goal="Reproduce the synthetic fixture deterministically",
            current_stage="analysis",
            important_question="Which local evidence supports the result?",
            deadline="2026-08-31",
            daily_available_hours=2.0,
        )
        cls.registration = load_registered_project(cls.workspace, cls.result.project_id)

        sync_source_registry(cls.workspace, cls.registration.project_id)
        sources = load_source_registry(cls.workspace, cls.registration.project_id)
        cls.source_record = sources.current_by_path["README.md"]
        excerpt = (
            cls.source.joinpath("README.md")
            .read_text(encoding="utf-8")
            .splitlines(keepends=True)[0]
        )
        cls.evidence = register_evidence(
            cls.workspace,
            cls.registration.project_id,
            source_id=cls.source_record.source_id,
            content_hash=cls.source_record.current_content_hash,
            locator=LineRangeLocator(1, 1),
            excerpt=excerpt,
        ).evidence
        cls.claim_path = "claims/cockpit-evidence-trace.md"
        root_text = "\n".join(
            (
                f"Source root: {cls.source}",
                f"Workspace root: {cls.workspace}",
                f"Machine root: {cls.registration.layout.machine_root}",
                f"Knowledge root: {cls.registration.layout.knowledge_root}",
            )
        )
        if os.name == "nt":
            root_text += "\nMixed root: " + str(cls.source).upper().replace("\\", "/")
        write_claim_page(
            cls.registration.layout.knowledge_root / cls.claim_path,
            project_id=cls.registration.project_id,
            source_id=cls.source_record.source_id,
            evidence_id=cls.evidence.evidence_id,
            body=f"# Evidence trace\n\n{root_text}\n",
        )
        set_manifest_file_state(
            cls.registration.layout.manifest_file,
            "README.md",
            FileState(
                processing_status="failed",
                read_depth="normal_read",
                reason_code="synthetic-extraction-failed",
                reason="Synthetic deterministic extraction failure for cockpit coverage.",
            ),
        )
        cls.cockpit = ResearchCockpit(cls.workspace)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            if cls.before != source_snapshot(cls.source):
                raise AssertionError(
                    "cockpit tests modified the registered source project"
                )
        finally:
            cls.temp_dir.cleanup()

    def test_inventory_coverage_and_truthful_file_state_are_visible(self) -> None:
        payload = self.cockpit.project_snapshot(self.registration.project_id)

        self.assertEqual(len(payload["artifacts"]), 15)
        self.assertTrue(
            all(item["availability"] == "available" for item in payload["artifacts"])
        )
        self.assertIsNotNone(payload["coverage"])
        self.assertEqual(payload["coverage"]["totals"]["file_count"], 8)
        self.assertEqual(payload["files"]["count"], 8)
        failed = [
            item
            for item in payload["files"]["preview"]
            if item["processing_status"] == "failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["path"], "README.md")
        self.assertEqual(failed[0]["read_depth"], "normal_read")
        self.assertEqual(failed[0]["reason_code"], "synthetic-extraction-failed")
        self.assertTrue(failed[0]["reason"])
        filtered = self.cockpit.project_files(
            self.registration.project_id,
            status="failed",
        )
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["items"][0]["path"], "README.md")

    def test_claim_evidence_source_and_locator_trace_is_navigable(self) -> None:
        claims = self.cockpit.project_claims(self.registration.project_id)
        claim = next(
            item for item in claims["items"] if item["path"] == self.claim_path
        )
        self.assertEqual(
            claim["evidence_refs"],
            [{"evidence_id": self.evidence.evidence_id, "stance": "supporting"}],
        )
        trace = claim["evidence_trace"][0]
        self.assertEqual(trace["evidence_id"], self.evidence.evidence_id)
        self.assertEqual(trace["source"]["source_id"], self.source_record.source_id)
        self.assertEqual(trace["source"]["current_path"], "README.md")
        self.assertEqual(trace["locator"]["locator_type"], "line_range")
        self.assertEqual(trace["locator"]["start_line"], 1)
        self.assertEqual(trace["registry_binding"], "registry-current")

        evidence = self.cockpit.project_evidence(
            self.registration.project_id,
            self.evidence.evidence_id,
        )
        source = self.cockpit.project_source(
            self.registration.project_id,
            self.source_record.source_id,
        )
        self.assertFalse(evidence["source_bytes_reopened"])
        self.assertFalse(source["source_bytes_reopened"])
        self.assertEqual(evidence["evidence"]["recorded_path"], "README.md")
        self.assertEqual(source["source"]["current_path"], "README.md")
        self.assertEqual(source["evidence_count"], 1)

    def test_goal_tasks_initial_plan_daily_plan_and_run_history_are_visible(
        self,
    ) -> None:
        payload = self.cockpit.project_snapshot(self.registration.project_id)
        planning = payload["planning"]
        self.assertEqual(
            planning["goal"]["goal"],
            "Reproduce the synthetic fixture deterministically",
        )
        self.assertGreaterEqual(len(planning["tasks"]), 1)
        self.assertIsNotNone(planning["initial_plan"])
        self.assertIsNotNone(planning["project_state"])
        self.assertEqual(planning["gaps"], [])
        self.assertGreaterEqual(payload["daily_plans"]["count"], 1)
        self.assertEqual(payload["daily_plans"]["today"]["availability"], "available")

        self.assertGreaterEqual(payload["runs"]["count"], 1)
        run = next(
            item
            for item in payload["runs"]["items"]
            if item["run_id"] == self.result.run_id
        )
        self.assertEqual(run["status"], "succeeded")
        self.assertIsInstance(run["duration_ms"], int)
        self.assertGreaterEqual(run["duration_ms"], 0)
        self.assertIn("estimated_cost", run["usage"])
        self.assertIn("errors", run)
        self.assertIsInstance(run["errors"], list)
        attempts = [attempt for stage in run["stages"] for attempt in stage["attempts"]]
        self.assertTrue(attempts)
        self.assertTrue(all("input_tokens" in attempt for attempt in attempts))
        self.assertTrue(all("estimated_cost" in attempt for attempt in attempts))
        self.assertTrue(all("error" in attempt for attempt in attempts))

    def test_validated_knowledge_and_every_api_view_redact_absolute_roots(self) -> None:
        payloads = [
            self.cockpit.project_snapshot(self.registration.project_id),
            self.cockpit.project_files(self.registration.project_id),
            self.cockpit.project_claims(self.registration.project_id),
            self.cockpit.project_evidence(
                self.registration.project_id,
                self.evidence.evidence_id,
            ),
            self.cockpit.project_source(
                self.registration.project_id,
                self.source_record.source_id,
            ),
            self.cockpit.project_knowledge(
                self.registration.project_id,
                self.claim_path,
            ),
        ]
        serialized = json.dumps(payloads, ensure_ascii=False)
        forbidden = (
            self.workspace,
            self.source,
            self.registration.layout.machine_root,
            self.registration.layout.knowledge_root,
        )
        for root in forbidden:
            with self.subTest(root=root):
                self.assertNotIn(str(root), serialized)
                self.assertNotIn(str(root).replace("\\", "/"), serialized)
                if os.name == "nt":
                    self.assertNotIn(str(root).upper().replace("\\", "/"), serialized)
        knowledge = payloads[-1]
        self.assertEqual(knowledge["availability"], "available")
        self.assertIn("[source-project-root]", knowledge["body"])
        self.assertIn("[machine-state-root]", knowledge["body"])
        self.assertIn("[knowledge-root]", knowledge["body"])
        self.assertIn("[workspace-root]", knowledge["body"])

    def test_source_project_bytes_hash_mtime_and_mode_remain_unchanged(self) -> None:
        self.assertEqual(self.before, source_snapshot(self.source))


class ArtifactGapCockpitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.source = self.root / "source"
        self.source.mkdir()
        self.registration = register_project(
            self.workspace,
            self.source,
            project_id="artifact-gap-study",
            knowledge_root=self.root / "knowledge",
        )
        self.cockpit = ResearchCockpit(self.workspace)

    def test_missing_malformed_and_future_artifacts_are_explicit_gaps(self) -> None:
        layout = self.registration.layout
        layout.knowledge_root.joinpath("overview.md").write_bytes(
            b"---\nschema_version: 3\nkind: llmwiki-knowledge-page\n---\n"
        )
        layout.knowledge_root.joinpath("project-map.md").write_bytes(b"\xff\xfe")
        layout.goals_file.write_bytes(b'{"schema_version":999}\n')
        layout.manifest_file.write_bytes(b'{"schema_version":999}\n')

        future = self.cockpit.project_knowledge(
            self.registration.project_id,
            "overview.md",
        )
        malformed = self.cockpit.project_knowledge(
            self.registration.project_id,
            "project-map.md",
        )
        missing = self.cockpit.project_knowledge(
            self.registration.project_id,
            "reproduction.md",
        )
        snapshot = self.cockpit.project_snapshot(self.registration.project_id)

        self.assertEqual(
            (future["availability"], future["reason_code"]),
            ("invalid", "knowledge-invalid"),
        )
        self.assertEqual(
            (malformed["availability"], malformed["reason_code"]),
            ("invalid", "knowledge-invalid"),
        )
        self.assertEqual(
            (missing["availability"], missing["reason_code"]),
            ("missing", "knowledge-missing"),
        )
        self.assertIn(
            "manifest-invalid",
            {item["reason_code"] for item in snapshot["gaps"]},
        )
        goal_gap = next(
            item
            for item in snapshot["planning"]["gaps"]
            if item["path"] == "goals.json"
        )
        self.assertEqual(goal_gap["reason_code"], "machine-artifact-invalid")


class CockpitHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.workspace = Path(self.temp_dir.name) / "workspace"
        self.workspace.mkdir()
        self.server = create_server(ResearchCockpit(self.workspace), port=0)
        self.server.RequestHandlerClass.log_message = lambda *_args: None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]
        self.addCleanup(self._close_server)

    def _close_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)

    def request(
        self,
        method: str,
        target: str,
        *,
        host_header: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5.0)
        try:
            connection.putrequest(method, target, skip_host=True)
            connection.putheader(
                "Host",
                host_header or f"127.0.0.1:{self.port}",
            )
            connection.endheaders()
            response = connection.getresponse()
            body = response.read()
            return (
                response.status,
                {key: value for key, value in response.getheaders()},
                body,
            )
        finally:
            connection.close()

    def test_bind_and_host_validation_are_loopback_only(self) -> None:
        self.assertEqual(validate_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_bind_host("::1"), "::1")
        for value in ("0.0.0.0", "192.0.2.1", "example.invalid"):
            with self.subTest(value=value):
                with self.assertRaises(ResearchCockpitError):
                    validate_bind_host(value)
        self.assertTrue(request_host_allowed(f"127.0.0.1:{self.port}"))
        self.assertTrue(request_host_allowed(f"[::1]:{self.port}"))
        self.assertFalse(request_host_allowed("example.com"))
        self.assertFalse(request_host_allowed("127.0.0.1@example.com"))

        status, _headers, body = self.request(
            "GET",
            "/api/health",
            host_header="example.com",
        )
        self.assertEqual(status, 421)
        self.assertEqual(json.loads(body)["error"], "misdirected_request")

    def test_every_non_get_or_head_method_returns_405_including_unknown_methods(
        self,
    ) -> None:
        for method in (
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "OPTIONS",
            "TRACE",
            "CONNECT",
            "PROPFIND",
            "BREW",
        ):
            with self.subTest(method=method):
                status, headers, body = self.request(method, "/api/health")
                self.assertEqual(status, 405)
                self.assertEqual(json.loads(body)["error"], "read_only")
                self.assertIn("Content-Security-Policy", headers)

    def test_assets_and_api_send_strict_security_headers(self) -> None:
        for target in ("/", "/assets/app.js", "/api/health"):
            with self.subTest(target=target):
                status, headers, body = self.request("GET", target)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")
                self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
                self.assertIn("object-src 'none'", headers["Content-Security-Policy"])
                self.assertIn(
                    "frame-ancestors 'none'", headers["Content-Security-Policy"]
                )
                self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        status, headers, body = self.request("HEAD", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers["Content-Length"]), 0)

    def test_encoded_separators_traversal_and_untrusted_query_names_are_rejected(
        self,
    ) -> None:
        cases = (
            "/api/projects/%2e%2e",
            "/api/projects/example%2Fchild",
            "/api/projects/example%5Cchild",
            "/api/projects/example/knowledge?path=..%2Foverview.md",
            "/api/health?bad=%ZZ",
            "/api/snapshot?project_id=one&project_id=two",
        )
        for target in cases:
            with self.subTest(target=target):
                status, _headers, body = self.request("GET", target)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"], "invalid_request")

        status, _headers, body = self.request(
            "GET",
            "/api/health?caller_secret_name=caller_secret_value",
        )
        self.assertEqual(status, 400)
        decoded = body.decode("utf-8")
        self.assertNotIn("caller_secret_name", decoded)
        self.assertNotIn("caller_secret_value", decoded)


class CockpitRedirectSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.source = self.root / "source"
        self.source.mkdir()
        self.registration = register_project(
            self.workspace,
            self.source,
            project_id="redirect-safety-study",
            knowledge_root=self.root / "knowledge",
        )

    def symlink_or_skip(
        self,
        target: Path,
        link: Path,
        *,
        directory: bool = False,
    ) -> None:
        try:
            os.symlink(target, link, target_is_directory=directory)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symbolic links unavailable on this host: {exc}")

    def test_asset_root_must_be_a_real_directory_and_not_a_redirect(self) -> None:
        plain_file = self.root / "asset-file"
        plain_file.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(CockpitAssetError):
            make_handler(ResearchCockpit(self.workspace), plain_file)

        real_assets = self.root / "real-assets"
        shutil.copytree(Path("assets/research-cockpit"), real_assets)
        redirected_assets = self.root / "redirected-assets"
        self.symlink_or_skip(real_assets, redirected_assets, directory=True)
        with self.assertRaises(CockpitAssetError):
            make_handler(ResearchCockpit(self.workspace), redirected_assets)

    def test_knowledge_file_redirect_is_reported_unsafe_without_reading_target(
        self,
    ) -> None:
        outside = self.root / "outside-overview.md"
        outside.write_text("outside bytes must not be parsed", encoding="utf-8")
        target = self.registration.layout.knowledge_root / "overview.md"
        self.symlink_or_skip(outside, target)

        payload = ResearchCockpit(self.workspace).project_knowledge(
            self.registration.project_id,
            "overview.md",
        )
        self.assertEqual(payload["availability"], "unsafe")
        self.assertEqual(payload["reason_code"], "knowledge-unsafe")
        self.assertIsNone(payload["body"])

    def test_redirected_daily_run_and_project_directories_are_not_traversed(
        self,
    ) -> None:
        layout = self.registration.layout
        outside_daily = self.root / "outside-daily"
        outside_runs = self.root / "outside-runs"
        outside_project = self.root / "outside-project"
        outside_daily.mkdir()
        outside_runs.mkdir()
        outside_project.mkdir()
        daily_plans_dir = layout.knowledge_root / "plans" / "daily"
        daily_plans_dir.rmdir()
        layout.runs_dir.rmdir()
        self.symlink_or_skip(outside_daily, daily_plans_dir, directory=True)
        self.symlink_or_skip(outside_runs, layout.runs_dir, directory=True)
        redirected_project = layout.machine_root.parent / "redirected-study"
        self.symlink_or_skip(outside_project, redirected_project, directory=True)

        cockpit = ResearchCockpit(self.workspace)
        payload = cockpit.project_snapshot(self.registration.project_id)
        self.assertIn(
            "daily-plan-directory-unsafe",
            {item["reason_code"] for item in payload["daily_plans"]["gaps"]},
        )
        self.assertIn(
            "runs-directory-unsafe",
            {item["reason_code"] for item in payload["runs"]["gaps"]},
        )
        self.assertNotIn(
            "redirected-study",
            {item["project_id"] for item in cockpit.list_projects()["projects"]},
        )


if __name__ == "__main__":
    unittest.main()
