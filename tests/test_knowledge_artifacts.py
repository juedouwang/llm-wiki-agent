from __future__ import annotations

import unittest
from copy import deepcopy

import yaml

from tools.knowledge_artifacts import (
    ARTIFACT_TYPES,
    EVIDENCE_STANCES,
    KNOWLEDGE_OWNERSHIPS,
    KNOWLEDGE_PAGE_KIND,
    KNOWLEDGE_SCHEMA_VERSION,
    KNOWLEDGE_STATUSES,
    LEGACY_KNOWLEDGE_SCHEMA_VERSION,
    KnowledgeArtifactPathError,
    KnowledgeFrontmatterError,
    UnsupportedKnowledgeSchemaVersionError,
    artifact_contract_for_path,
    parse_knowledge_page,
    serialize_knowledge_frontmatter,
    validate_knowledge_frontmatter,
)


SOURCE_ID = "src-0123456789abcdef0123456789abcdef"
EVIDENCE_ID = "evd-" + "a" * 64
SECOND_EVIDENCE_ID = "evd-" + "b" * 64
STRUCTURALLY_VALID_UNKNOWN_EVIDENCE_ID = "evd-" + "f" * 64


def evidence_ref(
    evidence_id: str = EVIDENCE_ID,
    stance: str = "context",
) -> dict[str, str]:
    return {"evidence_id": evidence_id, "stance": stance}


def valid_frontmatter(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "kind": KNOWLEDGE_PAGE_KIND,
        "project_id": "tiny-study-0123456789ab",
        "artifact_type": "overview",
        "title": "Tiny Study Overview",
        "status": "draft",
        "ownership": "generated",
        "source_ids": [SOURCE_ID],
        "evidence_refs": [evidence_ref()],
        "generated_at": "2026-07-17T08:00:00Z",
        "updated_at": "2026-07-17T09:00:00Z",
        "last_verified_at": None,
    }
    value.update(overrides)
    return value


def legacy_v1_frontmatter(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": LEGACY_KNOWLEDGE_SCHEMA_VERSION,
        "kind": KNOWLEDGE_PAGE_KIND,
        "project_id": "tiny-study-0123456789ab",
        "artifact_type": "overview",
        "title": "Legacy Tiny Study Overview",
        "status": "draft",
        "ownership": "generated",
        "source_ids": [SOURCE_ID],
        "evidence_ids": [EVIDENCE_ID],
        "generated_at": "2026-07-17T08:00:00Z",
        "updated_at": "2026-07-17T09:00:00Z",
        "last_verified_at": None,
    }
    value.update(overrides)
    return value


def page_bytes(frontmatter: dict[str, object], body: str = "# Body\n") -> bytes:
    return (serialize_knowledge_frontmatter(frontmatter) + body).encode("utf-8")


def raw_page_bytes(frontmatter: dict[str, object], body: str = "# Body\n") -> bytes:
    yaml_text = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
        line_break="\n",
    )
    return f"---\n{yaml_text}---\n{body}".encode("utf-8")


class KnowledgeArtifactPathTests(unittest.TestCase):
    def test_all_canonical_paths_have_the_expected_type_and_role(self) -> None:
        expected = {
            "index.md": ("project_index", "project_index"),
            "overview.md": ("overview", "singleton"),
            "project-map.md": ("project_map", "singleton"),
            "reproduction.md": ("reproduction", "singleton"),
            "architecture.md": ("architecture", "singleton"),
            "papers/index.md": ("paper", "collection_index"),
            "papers/attention-is-all-you-need.md": ("paper", "detail"),
            "methods/index.md": ("method", "collection_index"),
            "methods/contrastive-learning.md": ("method", "detail"),
            "datasets/index.md": ("dataset", "collection_index"),
            "datasets/cifar-10.md": ("dataset", "detail"),
            "experiments/index.md": ("experiment", "collection_index"),
            "experiments/baseline-run.md": ("experiment", "detail"),
            "results/index.md": ("result", "collection_index"),
            "results/top-1-accuracy.md": ("result", "detail"),
            "claims/index.md": ("claim", "collection_index"),
            "claims/model-improves-recall.md": ("claim", "detail"),
            "open-questions.md": ("open_question", "singleton"),
            "status.md": ("project_status", "singleton"),
            "risks.md": ("risk", "singleton"),
            "goals.md": ("goal", "singleton"),
            "plans/backlog.md": ("plan", "backlog"),
            "plans/daily/2026-07-17.md": ("plan", "daily_plan"),
            "decisions/use-local-evidence.md": ("decision", "detail"),
            "sources/project-readme.md": ("source", "detail"),
        }
        for path, (artifact_type, page_role) in expected.items():
            with self.subTest(path=path):
                contract = artifact_contract_for_path(path)
                self.assertEqual(contract.path, path)
                self.assertEqual(contract.artifact_type, artifact_type)
                self.assertEqual(contract.page_role, page_role)

        self.assertEqual(
            artifact_contract_for_path("papers/example.md").slug,
            "example",
        )
        self.assertEqual(
            artifact_contract_for_path("plans/daily/2026-07-17.md").plan_date,
            "2026-07-17",
        )

    def test_path_mapping_does_not_infer_semantics_from_arbitrary_names(self) -> None:
        rejected = (
            "metrics/accuracy.md",
            "results/metrics/accuracy.md",
            "paper.md",
            "papers.md",
            "goals/goal-one.md",
            "plans/tomorrow.md",
            "decisions/index.md",
            "sources/index.md",
            "papers/Index.md",
            "papers/not_kebab.md",
            "papers/-leading.md",
            "papers/trailing-.md",
            "papers/repeated--dash.md",
            "papers/readme.txt",
            "plans/daily/2026-02-30.md",
        )
        for path in rejected:
            with self.subTest(path=path):
                with self.assertRaises(KnowledgeArtifactPathError):
                    artifact_contract_for_path(path)

    def test_paths_must_be_canonical_project_relative_posix_paths(self) -> None:
        rejected: tuple[object, ...] = (
            None,
            1,
            "",
            " overview.md",
            "overview.md ",
            "/overview.md",
            "C:/overview.md",
            "C:\\overview.md",
            "papers\\example.md",
            "papers//example.md",
            "./overview.md",
            "papers/../overview.md",
            "papers/./example.md",
            "papers/:example.md",
            "papers/example.md/",
        )
        for path in rejected:
            with self.subTest(path=path):
                with self.assertRaises(KnowledgeArtifactPathError):
                    artifact_contract_for_path(path)

    def test_metric_is_a_reserved_semantic_type_without_a_project_path(self) -> None:
        self.assertIn("metric", ARTIFACT_TYPES)
        validated = validate_knowledge_frontmatter(
            valid_frontmatter(artifact_type="metric", title="Accuracy")
        )
        self.assertEqual(validated.artifact_type, "metric")
        with self.assertRaises(KnowledgeArtifactPathError):
            artifact_contract_for_path("metrics/accuracy.md")


class KnowledgeFrontmatterTests(unittest.TestCase):
    def test_type_status_ownership_and_stance_sets_are_closed(self) -> None:
        self.assertEqual(
            ARTIFACT_TYPES,
            {
                "overview",
                "project_map",
                "reproduction",
                "architecture",
                "paper",
                "method",
                "dataset",
                "experiment",
                "result",
                "claim",
                "open_question",
                "project_status",
                "risk",
                "goal",
                "plan",
                "project_index",
                "metric",
                "decision",
                "source",
            },
        )
        self.assertEqual(
            KNOWLEDGE_STATUSES,
            {"draft", "verified", "stale", "conflicting", "rejected"},
        )
        self.assertEqual(KNOWLEDGE_OWNERSHIPS, {"generated", "mixed", "user"})
        self.assertEqual(EVIDENCE_STANCES, {"supporting", "opposing", "context"})

    def test_valid_v2_frontmatter_is_canonicalized_without_semantic_inference(self) -> None:
        value = valid_frontmatter(
            generated_at="2026-07-17T16:00:00+08:00",
            updated_at="2026-07-17T17:00:00+08:00",
            ownership="mixed",
            evidence_refs=[
                evidence_ref(EVIDENCE_ID, "supporting"),
                evidence_ref(SECOND_EVIDENCE_ID, "opposing"),
            ],
        )
        validated = validate_knowledge_frontmatter(value, path="overview.md")
        self.assertEqual(validated.schema_version, 2)
        self.assertEqual(validated.project_id, "tiny-study-0123456789ab")
        self.assertEqual(validated.artifact_type, "overview")
        self.assertEqual(validated.ownership, "mixed")
        self.assertEqual(validated.source_ids, (SOURCE_ID,))
        self.assertIsNotNone(validated.evidence_refs)
        self.assertEqual(
            [(ref.evidence_id, ref.stance) for ref in validated.evidence_refs or ()],
            [(EVIDENCE_ID, "supporting"), (SECOND_EVIDENCE_ID, "opposing")],
        )
        self.assertEqual(validated.evidence_ids, (EVIDENCE_ID, SECOND_EVIDENCE_ID))
        self.assertEqual(validated.generated_at, "2026-07-17T08:00:00Z")
        self.assertEqual(validated.updated_at, "2026-07-17T09:00:00Z")

    def test_path_and_artifact_type_must_match(self) -> None:
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "does not match path"):
            validate_knowledge_frontmatter(
                valid_frontmatter(artifact_type="paper"),
                path="overview.md",
            )

    def test_v2_schema_is_closed_v1_is_read_only_and_v3_fails_closed(self) -> None:
        missing = valid_frontmatter()
        del missing["kind"]
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "missing fields: kind"):
            validate_knowledge_frontmatter(missing)

        unknown = valid_frontmatter(extra_field="not allowed")
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "unknown fields"):
            validate_knowledge_frontmatter(unknown)

        for version in (None, True, "2", 2.0, 0, -1):
            with self.subTest(version=version):
                with self.assertRaises(KnowledgeFrontmatterError):
                    validate_knowledge_frontmatter(
                        valid_frontmatter(schema_version=version)
                    )

        with self.assertRaisesRegex(KnowledgeFrontmatterError, "read-only"):
            validate_knowledge_frontmatter(legacy_v1_frontmatter())

        with self.assertRaises(UnsupportedKnowledgeSchemaVersionError):
            validate_knowledge_frontmatter(valid_frontmatter(schema_version=3))

    def test_fixed_kind_project_id_title_and_enums_are_validated(self) -> None:
        invalid_values = (
            {"kind": "other-kind"},
            {"project_id": "../escape"},
            {"project_id": "Uppercase"},
            {"title": ""},
            {"title": " outer whitespace "},
            {"title": "line\nbreak"},
            {"artifact_type": "paper_detail"},
            {"status": "complete"},
            {"ownership": "agent"},
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                with self.assertRaises(KnowledgeFrontmatterError):
                    validate_knowledge_frontmatter(valid_frontmatter(**overrides))

    def test_source_ids_are_strict_unique_yaml_sequences(self) -> None:
        invalid_values = (
            SOURCE_ID,
            (SOURCE_ID,),
            ["src-bad"],
            [SOURCE_ID, SOURCE_ID],
        )
        for source_ids in invalid_values:
            with self.subTest(source_ids=source_ids):
                with self.assertRaises(KnowledgeFrontmatterError):
                    validate_knowledge_frontmatter(
                        valid_frontmatter(source_ids=source_ids)
                    )

    def test_evidence_refs_are_strict_directional_and_duplicate_free(self) -> None:
        invalid_values: tuple[object, ...] = (
            EVIDENCE_ID,
            (evidence_ref(),),
            {"evidence_id": EVIDENCE_ID, "stance": "supporting"},
            [EVIDENCE_ID],
            [{"stance": "supporting"}],
            [{"evidence_id": EVIDENCE_ID}],
            [{"evidence_id": "evd-bad", "stance": "supporting"}],
            [{"evidence_id": EVIDENCE_ID, "stance": "supports"}],
            [
                {
                    "evidence_id": EVIDENCE_ID,
                    "stance": "supporting",
                    "confidence": 1,
                }
            ],
            [
                evidence_ref(EVIDENCE_ID, "supporting"),
                evidence_ref(EVIDENCE_ID, "opposing"),
            ],
        )
        for refs in invalid_values:
            with self.subTest(refs=refs):
                with self.assertRaises(KnowledgeFrontmatterError):
                    validate_knowledge_frontmatter(
                        valid_frontmatter(evidence_refs=refs)
                    )

    def test_timestamp_relations_and_verified_structure_are_enforced(self) -> None:
        invalid_values = (
            {"generated_at": "2026-07-17"},
            {"generated_at": "2026-07-17T08:00:00"},
            {"generated_at": "2026-13-17T08:00:00Z"},
            {"generated_at": "2026-07-17T08:00:00-00:00"},
            {
                "generated_at": "2026-07-17T10:00:00Z",
                "updated_at": "2026-07-17T09:00:00Z",
            },
            {"last_verified_at": "2026-07-17T07:59:59Z"},
            {"last_verified_at": "2026-07-17T09:00:01Z"},
            {"status": "verified", "last_verified_at": None},
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                with self.assertRaises(KnowledgeFrontmatterError):
                    validate_knowledge_frontmatter(valid_frontmatter(**overrides))

        verified = validate_knowledge_frontmatter(
            valid_frontmatter(
                status="verified",
                last_verified_at="2026-07-17T08:30:00Z",
            )
        )
        self.assertEqual(verified.status, "verified")
        self.assertEqual(verified.last_verified_at, "2026-07-17T08:30:00Z")

    def test_verified_key_claim_detail_requires_supporting_ref_and_verification_time(self) -> None:
        base = {
            "artifact_type": "claim",
            "title": "The model improves recall",
            "status": "verified",
            "last_verified_at": "2026-07-17T08:30:00Z",
        }
        for refs in (
            [],
            [evidence_ref(EVIDENCE_ID, "opposing")],
            [evidence_ref(EVIDENCE_ID, "context")],
            [
                evidence_ref(EVIDENCE_ID, "opposing"),
                evidence_ref(SECOND_EVIDENCE_ID, "context"),
            ],
        ):
            with self.subTest(refs=refs):
                with self.assertRaisesRegex(
                    KnowledgeFrontmatterError,
                    "supporting evidence_ref",
                ):
                    validate_knowledge_frontmatter(
                        valid_frontmatter(**base, evidence_refs=refs),
                        path="claims/model-improves-recall.md",
                    )

        with self.assertRaisesRegex(KnowledgeFrontmatterError, "last_verified_at"):
            validate_knowledge_frontmatter(
                valid_frontmatter(
                    **{
                        **base,
                        "evidence_refs": [evidence_ref(EVIDENCE_ID, "supporting")],
                        "last_verified_at": None,
                    }
                ),
                path="claims/model-improves-recall.md",
            )

        verified = validate_knowledge_frontmatter(
            valid_frontmatter(
                **base,
                evidence_refs=[
                    evidence_ref(EVIDENCE_ID, "opposing"),
                    evidence_ref(SECOND_EVIDENCE_ID, "supporting"),
                ],
            ),
            path="claims/model-improves-recall.md",
        )
        self.assertEqual(verified.status, "verified")
        self.assertEqual(verified.evidence_ids, (EVIDENCE_ID, SECOND_EVIDENCE_ID))

    def test_claims_index_is_not_key_but_pathless_claim_fails_closed_as_key(self) -> None:
        value = valid_frontmatter(
            artifact_type="claim",
            title="Claim Index",
            status="verified",
            evidence_refs=[],
            last_verified_at="2026-07-17T08:30:00Z",
        )
        index = validate_knowledge_frontmatter(value, path="claims/index.md")
        self.assertEqual(index.evidence_refs, ())

        with self.assertRaisesRegex(KnowledgeFrontmatterError, "supporting evidence_ref"):
            validate_knowledge_frontmatter(value)

    def test_structural_validation_does_not_claim_registry_or_source_currentness(self) -> None:
        validated = validate_knowledge_frontmatter(
            valid_frontmatter(
                artifact_type="claim",
                title="Structurally grounded only",
                status="verified",
                source_ids=[],
                evidence_refs=[
                    evidence_ref(
                        STRUCTURALLY_VALID_UNKNOWN_EVIDENCE_ID,
                        "supporting",
                    )
                ],
                last_verified_at="2026-07-17T08:30:00Z",
            ),
            path="claims/structurally-grounded-only.md",
        )
        self.assertEqual(
            validated.evidence_ids,
            (STRUCTURALLY_VALID_UNKNOWN_EVIDENCE_ID,),
        )


class KnowledgeSchemaV1CompatibilityTests(unittest.TestCase):
    def test_parser_reads_strict_v1_without_inventing_directional_stance(self) -> None:
        body = "# Legacy page\n\nNo migration occurred.\n"
        parsed = parse_knowledge_page(
            raw_page_bytes(legacy_v1_frontmatter(), body),
            path="overview.md",
        )
        self.assertEqual(parsed.frontmatter.schema_version, 1)
        self.assertEqual(parsed.frontmatter.evidence_ids, (EVIDENCE_ID,))
        self.assertIsNone(parsed.frontmatter.evidence_refs)
        self.assertEqual(parsed.frontmatter.as_dict(), legacy_v1_frontmatter())
        self.assertEqual(parsed.body, body)

    def test_v1_verified_claim_compatibility_does_not_apply_v2_stance_rules(self) -> None:
        parsed = parse_knowledge_page(
            raw_page_bytes(
                legacy_v1_frontmatter(
                    artifact_type="claim",
                    title="Legacy verified claim",
                    status="verified",
                    evidence_ids=[],
                    last_verified_at="2026-07-17T08:30:00Z",
                )
            ),
            path="claims/legacy-verified-claim.md",
        )
        self.assertEqual(parsed.frontmatter.status, "verified")
        self.assertEqual(parsed.frontmatter.evidence_ids, ())
        self.assertIsNone(parsed.frontmatter.evidence_refs)

    def test_v1_compatibility_remains_closed_and_duplicate_free(self) -> None:
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "unknown fields"):
            parse_knowledge_page(
                raw_page_bytes(
                    legacy_v1_frontmatter(
                        evidence_refs=[evidence_ref(EVIDENCE_ID, "supporting")]
                    )
                ),
                path="overview.md",
            )

        with self.assertRaisesRegex(KnowledgeFrontmatterError, "duplicates"):
            parse_knowledge_page(
                raw_page_bytes(
                    legacy_v1_frontmatter(evidence_ids=[EVIDENCE_ID, EVIDENCE_ID])
                ),
                path="overview.md",
            )

    def test_v1_cannot_be_current_validated_or_serialized(self) -> None:
        payload = raw_page_bytes(legacy_v1_frontmatter())
        parsed = parse_knowledge_page(payload, path="overview.md")

        with self.assertRaisesRegex(KnowledgeFrontmatterError, "read-only"):
            validate_knowledge_frontmatter(legacy_v1_frontmatter())
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "read-only"):
            serialize_knowledge_frontmatter(legacy_v1_frontmatter())
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "read-only"):
            serialize_knowledge_frontmatter(parsed.frontmatter)

    def test_v3_pages_fail_closed_before_field_reinterpretation(self) -> None:
        with self.assertRaises(UnsupportedKnowledgeSchemaVersionError):
            parse_knowledge_page(
                raw_page_bytes(valid_frontmatter(schema_version=3)),
                path="overview.md",
            )


class KnowledgePageParsingTests(unittest.TestCase):
    def test_parse_strict_utf8_v2_page_preserves_body(self) -> None:
        body = "# ????\n\nHuman and Agent content remains outside the parser.\n"
        parsed = parse_knowledge_page(
            page_bytes(valid_frontmatter(), body),
            path="overview.md",
        )
        self.assertEqual(parsed.frontmatter.title, "Tiny Study Overview")
        self.assertEqual(parsed.frontmatter.schema_version, 2)
        self.assertEqual(parsed.body, body)

    def test_parser_accepts_crlf_delimiters_and_normalizes_only_frontmatter_values(self) -> None:
        serialized = serialize_knowledge_frontmatter(valid_frontmatter())
        payload = (serialized.replace("\n", "\r\n") + "Body\r\n").encode("utf-8")
        parsed = parse_knowledge_page(payload, path="overview.md")
        self.assertEqual(parsed.body, "Body\r\n")

    def test_parser_requires_bytes_strict_utf8_and_exact_delimiters(self) -> None:
        with self.assertRaises(TypeError):
            parse_knowledge_page("---\n---\n", path="overview.md")
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "strict UTF-8"):
            parse_knowledge_page(b"---\n\xff\n---\n", path="overview.md")
        with self.assertRaises(KnowledgeFrontmatterError):
            parse_knowledge_page(b"no frontmatter\n", path="overview.md")
        with self.assertRaises(KnowledgeFrontmatterError):
            parse_knowledge_page(b"---\nschema_version: 2\n", path="overview.md")
        with self.assertRaises(KnowledgeFrontmatterError):
            parse_knowledge_page(b"--- \nschema_version: 2\n---\n", path="overview.md")
        with self.assertRaises(KnowledgeFrontmatterError):
            parse_knowledge_page(b"\xef\xbb\xbf---\n---\n", path="overview.md")
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "NUL"):
            parse_knowledge_page(b"---\nkind: x\n---\n\x00", path="overview.md")

    def test_safe_yaml_rejects_duplicates_aliases_merges_and_unsafe_tags(self) -> None:
        base = serialize_knowledge_frontmatter(valid_frontmatter())
        duplicate = base.replace(
            "schema_version: 2\n",
            "schema_version: 2\nschema_version: 2\n",
            1,
        )
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "duplicate YAML key"):
            parse_knowledge_page(duplicate.encode("utf-8"), path="overview.md")

        nested_duplicate = base.replace(
            "  stance: context\n",
            f"  stance: context\n  evidence_id: {SECOND_EVIDENCE_ID}\n",
            1,
        )
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "duplicate YAML key"):
            parse_knowledge_page(nested_duplicate.encode("utf-8"), path="overview.md")

        alias = base.replace(
            "title: Tiny Study Overview\n",
            "title: &title Tiny Study Overview\nstatus_copy: *title\n",
            1,
        )
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "aliases"):
            parse_knowledge_page(alias.encode("utf-8"), path="overview.md")

        merge = base.replace(
            "schema_version: 2\n",
            "schema_version: 2\n<<: {extra: value}\n",
            1,
        )
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "merge keys"):
            parse_knowledge_page(merge.encode("utf-8"), path="overview.md")

        unsafe = base.replace(
            "title: Tiny Study Overview",
            "title: !!python/object/apply:os.system ['echo unsafe']",
            1,
        )
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "invalid safe YAML"):
            parse_knowledge_page(unsafe.encode("utf-8"), path="overview.md")

    def test_yaml_timestamps_remain_strings_for_explicit_validation(self) -> None:
        payload = page_bytes(valid_frontmatter())
        parsed = parse_knowledge_page(payload, path="overview.md")
        self.assertIsInstance(parsed.frontmatter.generated_at, str)
        self.assertEqual(parsed.frontmatter.generated_at, "2026-07-17T08:00:00Z")

    def test_v2_serialization_is_deterministic_lf_only_and_round_trips(self) -> None:
        source = valid_frontmatter(
            title="??????",
            ownership="user",
            source_ids=[],
            evidence_refs=[],
            generated_at="2026-07-17T16:00:00+08:00",
            updated_at="2026-07-17T16:30:00+08:00",
        )
        source_before = deepcopy(source)
        first = serialize_knowledge_frontmatter(source, path="overview.md")
        second = serialize_knowledge_frontmatter(source, path="overview.md")
        self.assertEqual(first, second)
        self.assertEqual(source, source_before)
        self.assertTrue(first.startswith("---\nschema_version: 2\n"))
        self.assertTrue(first.endswith("---\n"))
        self.assertNotIn("\r", first)
        self.assertIn("evidence_refs: []", first)
        self.assertNotIn("evidence_ids:", first)
        self.assertIn("generated_at: '2026-07-17T08:00:00Z'", first)

        parsed = parse_knowledge_page((first + "Body\n").encode("utf-8"), path="overview.md")
        self.assertEqual(parsed.frontmatter.title, "??????")
        self.assertEqual(parsed.frontmatter.ownership, "user")
        self.assertEqual(parsed.frontmatter.evidence_ids, ())
        self.assertEqual(parsed.body, "Body\n")


if __name__ == "__main__":
    unittest.main()
