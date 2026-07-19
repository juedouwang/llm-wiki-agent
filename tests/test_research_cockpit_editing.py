from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
import unittest
from unittest import mock

import yaml

from tools import controlled_markdown_persistence as persistence
from tools.controlled_markdown import parse_mixed_markdown_body
from tools.controlled_markdown_persistence import (
    CONTROLLED_MARKDOWN_AUDIT_FILE,
    ControlledMarkdownCommitAuditUnknownError,
    ControlledMarkdownCommitUnknownError,
    ControlledMarkdownWriteConflictError,
    TrustedHostSessionContext,
)
from tools.knowledge_artifacts import (
    LEGACY_KNOWLEDGE_SCHEMA_VERSION,
    parse_knowledge_page,
    serialize_knowledge_frontmatter,
)
from tools.project_registry import load_registered_project
from tools.research_cockpit import (
    CONTROLLED_EDIT_TARGETS,
    EDIT_TOKEN_HEADER,
    MAX_EDIT_REQUEST_BYTES,
    CockpitEditConflict,
    CockpitEditRequestError,
    CockpitEditTargetUnavailable,
    CockpitEditingUnavailable,
    ResearchCockpit,
    create_server,
)
from tools.research_core import ResearchCoreService


ASSET_ROOT = Path(__file__).parents[1] / "assets" / "research-cockpit"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "minimal_research_project"
EXPECTED_TARGETS = (
    ("goal", "goals.md", "user-goals"),
    ("backlog", "plans/backlog.md", "user-backlog"),
    ("project_status", "status.md", "user-status"),
    (
        "user_confirmed_conclusions",
        "claims/index.md",
        "user-confirmed-claims",
    ),
)


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
        metadata = base.stat()
        relative = "." if base == root else base.relative_to(root).as_posix()
        directories[relative] = (
            metadata.st_mtime_ns,
            stat.S_IMODE(metadata.st_mode),
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


def canonical_frontmatter(mapping: dict[str, object]) -> str:
    return (
        "---\n"
        + yaml.safe_dump(
            mapping,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=1000,
            line_break="\n",
        )
        + "---\n"
    )


class ControlledEditingFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp_dir.name)
        cls.workspace = cls.root / "workspace"
        cls.source = cls.root / "minimal-research-project"
        cls.knowledge_parent = cls.root / "knowledge"
        shutil.copytree(FIXTURE_ROOT, cls.source)
        cls.source_before = source_snapshot(cls.source)
        result = ResearchCoreService(cls.workspace).project_understand(
            cls.source,
            project_id="cockpit-editing-study",
            name="Cockpit Editing Study",
            knowledge_root=cls.knowledge_parent,
            final_goal="Validate bounded cockpit editing",
            current_stage="analysis",
            important_question="Can edits preserve generated knowledge?",
            deadline="2026-08-31",
            daily_available_hours=2.0,
        )
        cls.project_id = result.project_id
        cls.registration = load_registered_project(cls.workspace, cls.project_id)
        cls.original_pages = {
            path: (cls.registration.layout.knowledge_root / path).read_bytes()
            for _key, path, _region in EXPECTED_TARGETS
        }
        cls.audit_path = (
            cls.registration.layout.indexes_dir / CONTROLLED_MARKDOWN_AUDIT_FILE
        )

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            if source_snapshot(cls.source) != cls.source_before:
                raise AssertionError(
                    "controlled cockpit editing modified source project"
                )
        finally:
            cls.temp_dir.cleanup()

    def setUp(self) -> None:
        for relative, payload in self.original_pages.items():
            (self.registration.layout.knowledge_root / relative).write_bytes(payload)
        if self.audit_path.exists():
            self.audit_path.unlink()
        self.host_context = TrustedHostSessionContext(
            host_id="research-cockpit-test",
            actor_type="user",
            actor_id="test-user",
            session_id="test-session",
        )
        self.cockpit = ResearchCockpit(
            self.workspace,
            edit_context=self.host_context,
        )

    def page_path(self, relative: str) -> Path:
        return self.registration.layout.knowledge_root / relative

    def audit_records(self) -> list[dict[str, object]]:
        if not self.audit_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]


class ControlledEditingCoreTests(ControlledEditingFixture):
    def test_four_targets_are_exact_and_arbitrary_paths_are_not_exposed(self) -> None:
        actual = tuple(
            (item.key, item.path, item.region_id) for item in CONTROLLED_EDIT_TARGETS
        )
        self.assertEqual(actual, EXPECTED_TARGETS)
        session = self.cockpit.edit_session()
        self.assertEqual(
            tuple(
                (item["key"], item["path"], item["region_id"])
                for item in session["targets"]
            ),
            EXPECTED_TARGETS,
        )
        self.assertFalse(session["arbitrary_paths"])
        self.assertEqual(
            session["query"],
            {
                "available": False,
                "error": "capability-unavailable",
                "available_after": "G-04",
            },
        )
        self.assertEqual(session["c07"], "deferred/not_started")
        with self.assertRaises(KeyError):
            self.cockpit.project_edit_snapshot(self.project_id, "open_question")

    def test_default_cockpit_remains_read_only_with_exact_query_contract(self) -> None:
        cockpit = ResearchCockpit(self.workspace)
        health = cockpit.health()
        self.assertTrue(health["read_only"])
        self.assertFalse(health["capabilities"]["editing"]["available"])
        self.assertEqual(
            health["capabilities"]["query"],
            {
                "available": False,
                "error": "capability-unavailable",
                "available_after": "G-04",
            },
        )
        self.assertEqual(health["capabilities"]["c07"], "deferred/not_started")
        with self.assertRaises(CockpitEditingUnavailable):
            cockpit.project_edit_snapshot(self.project_id, "goal")

    def test_snapshot_revision_is_full_page_hash_and_stale_hash_never_writes(
        self,
    ) -> None:
        page_path = self.page_path("goals.md")
        current = page_path.read_bytes()
        snapshot = self.cockpit.project_edit_snapshot(self.project_id, "goal")
        self.assertEqual(
            snapshot["current_sha256"], hashlib.sha256(current).hexdigest()
        )
        self.assertEqual(
            snapshot["content_sha256"],
            hashlib.sha256(snapshot["content"].encode("utf-8")).hexdigest(),
        )
        stale = "0" * 64
        with self.assertRaises(CockpitEditConflict) as raised:
            self.cockpit.project_edit(
                self.project_id,
                "goal",
                expected_current_sha256=stale,
                content="User goal\n",
            )
        self.assertEqual(raised.exception.expected_current_sha256, stale)
        self.assertEqual(page_path.read_bytes(), current)
        self.assertEqual(self.audit_records(), [])

    def test_successful_write_preserves_frontmatter_skeleton_markers_and_other_pages(
        self,
    ) -> None:
        target_path = self.page_path("goals.md")
        before_bytes = target_path.read_bytes()
        before_page = parse_knowledge_page(before_bytes, path="goals.md")
        before_body = parse_mixed_markdown_body(before_page.body)
        other_pages = {
            relative: self.page_path(relative).read_bytes()
            for _key, relative, _region in EXPECTED_TARGETS
            if relative != "goals.md"
        }
        content = "- Confirm reproducibility\n- Preserve local-only operation\n"
        result = self.cockpit.project_edit(
            self.project_id,
            "goal",
            expected_current_sha256=hashlib.sha256(before_bytes).hexdigest(),
            content=content,
        )
        after_bytes = target_path.read_bytes()
        after_page = parse_knowledge_page(after_bytes, path="goals.md")
        after_body = parse_mixed_markdown_body(after_page.body)

        before_frontmatter = before_page.frontmatter.as_dict()
        after_frontmatter = after_page.frontmatter.as_dict()
        before_updated = before_frontmatter.pop("updated_at")
        after_updated = after_frontmatter.pop("updated_at")
        self.assertEqual(after_frontmatter, before_frontmatter)
        self.assertGreater(after_updated, before_updated)
        self.assertEqual(
            after_body.generated_skeleton(), before_body.generated_skeleton()
        )
        self.assertEqual(after_body.regions[0].content, content)
        self.assertEqual(
            result["current_sha256"], hashlib.sha256(after_bytes).hexdigest()
        )
        self.assertFalse(result["body_returned"])
        self.assertNotIn("content", result)
        self.assertNotIn("body", result)
        for relative, payload in other_pages.items():
            self.assertEqual(self.page_path(relative).read_bytes(), payload)

        records = self.audit_records()
        self.assertEqual(
            [record["phase"] for record in records], ["prepared", "committed"]
        )
        audit_text = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn("Confirm reproducibility", audit_text)
        self.assertNotIn("Preserve local-only operation", audit_text)
        self.assertNotIn('"content"', audit_text)
        self.assertNotIn('"body"', audit_text)

    def test_regeneration_preserves_the_web_edited_user_region(self) -> None:
        target = self.page_path("plans/backlog.md")
        snapshot = self.cockpit.project_edit_snapshot(self.project_id, "backlog")
        content = "- [ ] Preserve this user-maintained task after regeneration\n"
        self.cockpit.project_edit(
            self.project_id,
            "backlog",
            expected_current_sha256=snapshot["current_sha256"],
            content=content,
        )

        result = ResearchCoreService(self.workspace).knowledge_render(
            self.project_id,
            rendered_at="2026-07-20T00:00:00Z",
            plan_date="2026-07-20",
            host_context=self.host_context,
            decision_id_prefix="j04-regeneration",
            authorized_at="2026-07-20T00:00:01Z",
        )

        self.assertEqual(result.status, "succeeded")
        page = parse_knowledge_page(target.read_bytes(), path="plans/backlog.md")
        mixed = parse_mixed_markdown_body(page.body)
        self.assertEqual(mixed.regions[0].content, content)

    def test_target_validation_fails_closed_for_legacy_future_malformed_and_wrong_policy(
        self,
    ) -> None:
        relative = "goals.md"
        path = self.page_path(relative)
        original = path.read_bytes()
        parsed = parse_knowledge_page(original, path=relative)
        frontmatter = parsed.frontmatter.as_dict()

        legacy = dict(frontmatter)
        legacy["schema_version"] = LEGACY_KNOWLEDGE_SCHEMA_VERSION
        legacy["evidence_ids"] = [
            item["evidence_id"] for item in legacy.pop("evidence_refs")
        ]
        cases: list[tuple[str, bytes]] = [
            ("legacy", (canonical_frontmatter(legacy) + parsed.body).encode("utf-8")),
        ]
        future = dict(frontmatter)
        future["schema_version"] = 3
        cases.append(
            ("future", (canonical_frontmatter(future) + parsed.body).encode("utf-8"))
        )
        cases.append(("malformed", b"---\nschema_version: [\n---\n"))
        for ownership in ("generated", "user"):
            changed = dict(frontmatter)
            changed["ownership"] = ownership
            cases.append(
                (
                    ownership,
                    (
                        serialize_knowledge_frontmatter(changed, path=relative)
                        + parsed.body
                    ).encode("utf-8"),
                )
            )
        stale = dict(frontmatter)
        stale["status"] = "stale"
        cases.append(
            (
                "non-draft",
                (
                    serialize_knowledge_frontmatter(stale, path=relative) + parsed.body
                ).encode("utf-8"),
            )
        )
        cases.append(
            (
                "wrong-region",
                original.replace(b'id="user-goals"', b'id="user-status"'),
            )
        )

        for label, payload in cases:
            with self.subTest(label=label):
                path.write_bytes(payload)
                with self.assertRaises(CockpitEditTargetUnavailable):
                    self.cockpit.project_edit_snapshot(self.project_id, "goal")
                self.assertEqual(self.audit_records(), [])
                path.write_bytes(original)

    def test_invalid_content_and_local_roots_are_rejected_before_audit(self) -> None:
        snapshot = self.cockpit.project_edit_snapshot(self.project_id, "goal")
        invalid = (
            "missing final newline",
            '<!-- llmwiki:user-region:start id="forged" -->\n',
            f"source is {self.source}\n",
            "nul\x00byte\n",
        )
        for content in invalid:
            with self.subTest(content=content[:24]):
                with self.assertRaises(CockpitEditRequestError):
                    self.cockpit.project_edit(
                        self.project_id,
                        "goal",
                        expected_current_sha256=snapshot["current_sha256"],
                        content=content,
                    )
                self.assertEqual(self.audit_records(), [])

    def test_cas_race_records_conflict_and_does_not_overwrite_competing_bytes(
        self,
    ) -> None:
        target = self.page_path("goals.md")
        before = target.read_bytes()
        expected = hashlib.sha256(before).hexdigest()
        competing = before.replace(
            b"## User-confirmed goals and milestones\n",
            b"## User-confirmed goals and milestones\n\nCompeting writer won.\n",
        )
        real_compare_and_swap = persistence.compare_and_swap_atomic_stable_file

        def race(
            trusted_root: Path,
            path: Path,
            payload: bytes,
            *,
            expected_current_sha256: str | None,
            root_lease: object = None,
        ) -> object:
            target.write_bytes(competing)
            return real_compare_and_swap(
                trusted_root,
                path,
                payload,
                expected_current_sha256=expected_current_sha256,
                root_lease=root_lease,
            )

        with mock.patch.object(
            persistence,
            "compare_and_swap_atomic_stable_file",
            side_effect=race,
        ):
            with self.assertRaises(ControlledMarkdownWriteConflictError):
                self.cockpit.project_edit(
                    self.project_id,
                    "goal",
                    expected_current_sha256=expected,
                    content="Web proposal must lose the race.\n",
                )
        self.assertEqual(target.read_bytes(), competing)
        records = self.audit_records()
        self.assertEqual(
            [record["phase"] for record in records], ["prepared", "conflict"]
        )
        audit_text = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn("Web proposal must lose the race", audit_text)
        self.assertNotIn("Competing writer won", audit_text)

    def test_source_project_hash_mtime_and_mode_never_change(self) -> None:
        snapshot = self.cockpit.project_edit_snapshot(self.project_id, "project_status")
        self.cockpit.project_edit(
            self.project_id,
            "project_status",
            expected_current_sha256=snapshot["current_sha256"],
            content="Status confirmed by the local user.\n",
        )
        self.assertEqual(source_snapshot(self.source), self.source_before)


class ControlledEditingHttpTests(ControlledEditingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.server = create_server(self.cockpit, port=0)
        self.server.RequestHandlerClass.log_message = lambda *_args: None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]
        self.authority = f"{self.host}:{self.port}"
        self.origin = f"http://{self.authority}"
        self.addCleanup(self._close_server)
        status, _headers, session = self.request_json(
            "GET",
            "/api/edit-session",
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 200)
        self.session = session
        self.token = session["edit_token"]

    def _close_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def raw_request(
        self,
        method: str,
        target: str,
        *,
        body: bytes = b"",
        headers: list[tuple[str, str]] | None = None,
        host_header: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.putrequest(
                method, target, skip_host=True, skip_accept_encoding=True
            )
            connection.putheader("Host", host_header or self.authority)
            for key, value in headers or []:
                connection.putheader(key, value)
            connection.endheaders(body)
            response = connection.getresponse()
            payload = response.read()
            return (
                response.status,
                {key: value for key, value in response.getheaders()},
                payload,
            )
        finally:
            connection.close()

    def request_json(
        self,
        method: str,
        target: str,
        *,
        payload: dict[str, object] | None = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
        authorize: bool = False,
        host_header: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, object]]:
        body = raw_body
        if body is None:
            body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = list((headers or {}).items())
        names = {key.casefold() for key, _value in request_headers}
        if authorize:
            defaults = {
                "Origin": self.origin,
                "Sec-Fetch-Site": "same-origin",
                EDIT_TOKEN_HEADER: self.token,
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
            }
            for key, value in defaults.items():
                if key.casefold() not in names:
                    request_headers.append((key, value))
        status, response_headers, response_body = self.raw_request(
            method,
            target,
            body=body,
            headers=request_headers,
            host_header=host_header,
        )
        return status, response_headers, json.loads(response_body or b"{}")

    def valid_request(
        self, *, content: str = "Confirmed in Web UI.\n"
    ) -> dict[str, object]:
        snapshot = self.cockpit.project_edit_snapshot(self.project_id, "goal")
        return {
            "schema_version": 1,
            "expected_current_sha256": snapshot["current_sha256"],
            "content": content,
        }

    def test_session_mode_and_fixed_target_get_are_available_only_when_enabled(
        self,
    ) -> None:
        self.assertTrue(self.session["editing"])
        self.assertEqual(
            tuple(
                (item["key"], item["path"], item["region_id"])
                for item in self.session["targets"]
            ),
            EXPECTED_TARGETS,
        )
        self.assertEqual(self.session["query"]["error"], "capability-unavailable")
        self.assertEqual(self.session["query"]["available_after"], "G-04")
        self.assertEqual(self.session["c07"], "deferred/not_started")

        status, _headers, payload = self.request_json(
            "GET",
            f"/api/projects/{self.project_id}/edits/goal",
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["target"]["path"], "goals.md")
        self.assertIn("content", payload)
        self.assertFalse(payload["arbitrary_paths"])

        read_only = ResearchCockpit(self.workspace)
        server = create_server(read_only, port=0)
        server.RequestHandlerClass.log_message = lambda *_args: None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        ro_host, ro_port = server.server_address[:2]
        try:
            connection = http.client.HTTPConnection(ro_host, ro_port, timeout=10)
            connection.request("GET", "/api/edit-session")
            response = connection.getresponse()
            body = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 404)
            self.assertEqual(body["error"], "not_found")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_valid_post_commits_then_refetches_new_full_page_revision(self) -> None:
        request = self.valid_request(content="User-confirmed cockpit conclusion.\n")
        status, _headers, result = self.request_json(
            "POST",
            f"/api/projects/{self.project_id}/edits/goal",
            payload=request,
            authorize=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["outcome"], "committed")
        self.assertFalse(result["body_returned"])
        self.assertNotIn("content", result)
        self.assertNotIn("body", result)

        status, _headers, refreshed = self.request_json(
            "GET",
            f"/api/projects/{self.project_id}/edits/goal",
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(refreshed["content"], "User-confirmed cockpit conclusion.\n")
        self.assertEqual(refreshed["current_sha256"], result["current_sha256"])

    def test_unknown_target_and_arbitrary_edit_paths_are_404(self) -> None:
        for target in (
            f"/api/projects/{self.project_id}/edits/open_question",
            f"/api/projects/{self.project_id}/edits/goal/extra",
            f"/api/projects/{self.project_id}/edits?path=goals.md",
        ):
            with self.subTest(target=target):
                status, _headers, payload = self.request_json(
                    "POST",
                    target,
                    payload=self.valid_request(),
                    authorize=True,
                )
                self.assertEqual(status, 404)
                self.assertEqual(payload["error"], "not_found")

    def test_token_origin_fetch_site_and_host_are_required(self) -> None:
        target = f"/api/projects/{self.project_id}/edits/goal"
        request = self.valid_request()
        cases = (
            ("missing-token", {"Origin": self.origin, "Sec-Fetch-Site": "same-origin"}),
            (
                "wrong-token",
                {
                    "Origin": self.origin,
                    "Sec-Fetch-Site": "same-origin",
                    EDIT_TOKEN_HEADER: "wrong",
                },
            ),
            (
                "wrong-origin",
                {
                    "Origin": "http://example.invalid",
                    "Sec-Fetch-Site": "same-origin",
                    EDIT_TOKEN_HEADER: self.token,
                },
            ),
            (
                "cross-site",
                {
                    "Origin": self.origin,
                    "Sec-Fetch-Site": "cross-site",
                    EDIT_TOKEN_HEADER: self.token,
                },
            ),
        )
        for label, auth_headers in cases:
            headers = {
                **auth_headers,
                "Content-Type": "application/json",
            }
            raw = json.dumps(request).encode("utf-8")
            headers["Content-Length"] = str(len(raw))
            with self.subTest(label=label):
                status, _response_headers, payload = self.request_json(
                    "POST",
                    target,
                    raw_body=raw,
                    headers=headers,
                )
                self.assertEqual(status, 403)
                self.assertEqual(payload["error"], "forbidden")

        status, _headers, payload = self.request_json(
            "POST",
            target,
            payload=request,
            authorize=True,
            host_header="example.invalid",
        )
        self.assertEqual(status, 421)
        self.assertEqual(payload["error"], "misdirected_request")
        self.assertEqual(self.audit_records(), [])

    def test_json_framing_and_schema_reject_unsafe_requests(self) -> None:
        target = f"/api/projects/{self.project_id}/edits/goal"
        valid = self.valid_request()
        valid_raw = json.dumps(valid).encode("utf-8")
        duplicate = (
            b'{"schema_version":1,"schema_version":1,'
            b'"expected_current_sha256":"'
            + str(valid["expected_current_sha256"]).encode("ascii")
            + b'","content":"x\\n"}'
        )
        forged = dict(valid)
        forged.update(
            {
                "actor_id": "forged",
                "session_id": "forged",
                "decision_id": "forged",
                "path": "goals.md",
            }
        )
        cases = (
            ("duplicate-json-key", duplicate, {}, 400),
            ("unknown-fields", json.dumps(forged).encode("utf-8"), {}, 400),
            (
                "nan",
                valid_raw.replace(b'"content": "', b'"extra": NaN, "content": "'),
                {},
                400,
            ),
            ("wrong-content-type", valid_raw, {"Content-Type": "text/plain"}, 415),
            (
                "duplicate-charset",
                valid_raw,
                {"Content-Type": "application/json; charset=utf-8; charset=utf-8"},
                415,
            ),
        )
        for label, raw, extra_headers, expected_status in cases:
            with self.subTest(label=label):
                status, _headers, _payload = self.request_json(
                    "POST",
                    target,
                    raw_body=raw,
                    headers=extra_headers,
                    authorize=True,
                )
                self.assertEqual(status, expected_status)

        headers = [
            ("Origin", self.origin),
            ("Sec-Fetch-Site", "same-origin"),
            (EDIT_TOKEN_HEADER, self.token),
            ("Content-Type", "application/json"),
            ("Content-Length", str(MAX_EDIT_REQUEST_BYTES + 1)),
        ]
        status, _headers, payload = self.raw_request("POST", target, headers=headers)
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(payload)["reason_code"], "edit-request-too-large")

        headers = [
            ("Origin", self.origin),
            ("Sec-Fetch-Site", "same-origin"),
            (EDIT_TOKEN_HEADER, self.token),
            ("Content-Type", "application/json"),
            ("Content-Length", "0"),
            ("Transfer-Encoding", "chunked"),
        ]
        status, _headers, payload = self.raw_request("POST", target, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(
            json.loads(payload)["reason_code"], "transfer-encoding-forbidden"
        )
        self.assertEqual(self.audit_records(), [])

    def test_stale_revision_is_409_and_never_overwrites(self) -> None:
        target_path = self.page_path("goals.md")
        before = target_path.read_bytes()
        request = self.valid_request()
        request["expected_current_sha256"] = "0" * 64
        status, _headers, payload = self.request_json(
            "POST",
            f"/api/projects/{self.project_id}/edits/goal",
            payload=request,
            authorize=True,
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["reason_code"], "revision-conflict")
        self.assertEqual(payload["page_commit_state"], "not-committed")
        self.assertEqual(target_path.read_bytes(), before)
        self.assertEqual(self.audit_records(), [])

    def test_commit_and_audit_unknown_are_body_free_503_states(self) -> None:
        target = f"/api/projects/{self.project_id}/edits/goal"
        transaction_id = "cmtx-" + "a" * 64
        cases = (
            ControlledMarkdownCommitUnknownError(
                "page state unknown",
                transaction_id=transaction_id,
            ),
            ControlledMarkdownCommitAuditUnknownError(
                "audit state unknown",
                transaction_id=transaction_id,
            ),
        )
        for exc in cases:
            with self.subTest(exception=type(exc).__name__):
                with mock.patch(
                    "tools.research_cockpit.persist_controlled_markdown_update",
                    side_effect=exc,
                ):
                    status, _headers, payload = self.request_json(
                        "POST",
                        target,
                        payload=self.valid_request(content="secret response body\n"),
                        authorize=True,
                    )
                self.assertEqual(status, 503)
                self.assertEqual(payload["transaction_id"], transaction_id)
                self.assertNotIn("content", payload)
                self.assertNotIn("body", payload)
                self.assertNotIn("secret response body", json.dumps(payload))


class ControlledEditingFrontendContractTests(unittest.TestCase):
    def test_frontend_has_only_the_fixed_controlled_editor_contract(self) -> None:
        html = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
        javascript = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (ASSET_ROOT / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="editing-section"', html)
        self.assertIn('id="edit-target-list"', html)
        self.assertIn('id="edit-content"', html)
        self.assertNotIn('type="file"', html)
        self.assertNotRegex(html, r'<input[^>]+(?:name|id)="(?:path|edit-path)"')
        self.assertIn("Generated Markdown,", html)
        self.assertIn(
            "frontmatter, markers, and every other page remain protected", html
        )
        self.assertIn("Verified Query</strong> is unavailable until G-04", html)
        self.assertIn("C-07</strong> remains deferred / not started", html)

        self.assertIn("const tokenHeader = state.editSession.token_header", javascript)
        self.assertIn('"X-LLMWiki-Edit-Token"', javascript)
        self.assertIn('method: "POST"', javascript)
        self.assertIn(
            "expected_current_sha256: state.editSnapshot.current_sha256", javascript
        )
        self.assertIn("Your unsaved draft remains in the editor", javascript)
        save_start = javascript.index("async function saveEdit")
        save_end = javascript.index("async function loadEditSession", save_start)
        save_function = javascript[save_start:save_end]
        self.assertEqual(save_function.count("await loadEditTarget(targetKey"), 1)
        catch_start = save_function.index("} catch (error) {")
        finally_start = save_function.index("} finally {", catch_start)
        self.assertNotIn(
            "loadEditTarget",
            save_function[catch_start:finally_start],
        )
        self.assertLess(
            save_function.index("await loadEditTarget(targetKey"),
            save_function.index("await loadSnapshot();"),
        )
        self.assertLess(
            save_function.index("await loadSnapshot();"),
            save_function.index("await loadKnowledge(target.path)"),
        )

        for selector in (
            ".editing-panel",
            ".editing-grid",
            ".edit-target-button",
            ".edit-textarea",
            ".edit-message",
            ".mode-badge.editing-mode",
        ):
            self.assertIn(selector, styles)

    def test_frontend_dom_bindings_are_complete_and_assets_are_local(self) -> None:
        html = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
        javascript = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (ASSET_ROOT / "styles.css").read_text(encoding="utf-8")
        ids_match = re.search(
            r"const ids = \[(?P<body>.*?)\];",
            javascript,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(ids_match)
        assert ids_match is not None
        bound_ids = re.findall(r'"([a-z0-9-]+)"', ids_match.group("body"))
        html_ids = set(re.findall(r'id="([a-z0-9-]+)"', html))
        self.assertEqual(len(bound_ids), len(set(bound_ids)))
        self.assertTrue(set(bound_ids).issubset(html_ids))
        for payload in (html, javascript, styles):
            self.assertNotIn("http://", payload)
            self.assertNotIn("https://", payload)
            self.assertNotIn("//cdn", payload.casefold())


if __name__ == "__main__":
    unittest.main()
