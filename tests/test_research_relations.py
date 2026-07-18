from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from tools.knowledge_artifacts import (
    KNOWLEDGE_PAGE_KIND,
    KNOWLEDGE_SCHEMA_VERSION,
    artifact_contract_for_path,
    serialize_knowledge_frontmatter,
)
from tools.research_relations import (
    RESEARCH_ENTITY_TYPES,
    ResearchEntity,
    ResearchEntityBindingError,
    ResearchRelation,
    ResearchRelationConflictError,
    ResearchRelationError,
    ResearchRelationRegistry,
    UnsupportedResearchRelationSchemaVersionError,
    build_research_project_index,
    create_research_entity,
    create_research_relation,
    deserialize_research_relation_registry,
    research_entity_id_for,
    research_relation_id_for,
    validate_research_entity_bindings,
)

PROJECT_ID = "tiny-study-0123456789ab"
OTHER_PROJECT_ID = "other-study-abcdef012345"
EVIDENCE_A = "evd-" + "a" * 64
EVIDENCE_B = "evd-" + "b" * 64

DIRECT_LOCATIONS: dict[str, tuple[str, str]] = {
    "paper": ("papers/transformer.md", "Transformer Paper"),
    "method": ("methods/attention.md", "Attention Method"),
    "dataset": ("datasets/tiny-set.md", "Tiny Dataset"),
    "experiment": ("experiments/baseline.md", "Baseline Experiment"),
    "result": ("results/baseline.md", "Baseline Result"),
    "claim": ("claims/recall.md", "Recall Claim"),
    "decision": ("decisions/use-attention.md", "Use Attention"),
    "source": ("sources/paper-source.md", "Paper Source"),
}


def page_bytes(
    path: str,
    title: str,
    *,
    project_id: str = PROJECT_ID,
    schema_version: int = KNOWLEDGE_SCHEMA_VERSION,
    body: str = "# Body\n",
) -> bytes:
    contract = artifact_contract_for_path(path)
    frontmatter: dict[str, object] = {
        "schema_version": schema_version,
        "kind": KNOWLEDGE_PAGE_KIND,
        "project_id": project_id,
        "artifact_type": contract.artifact_type,
        "title": title,
        "status": "draft",
        "ownership": "generated",
        "source_ids": [],
        "evidence_refs": [],
        "generated_at": "2026-07-18T08:00:00Z",
        "updated_at": "2026-07-18T08:00:00Z",
        "last_verified_at": None,
    }
    if schema_version == KNOWLEDGE_SCHEMA_VERSION:
        rendered = serialize_knowledge_frontmatter(frontmatter, path=path)
    else:
        if schema_version == 1:
            frontmatter["evidence_ids"] = frontmatter.pop("evidence_refs")
        yaml_text = yaml.safe_dump(
            frontmatter,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            line_break="\n",
        )
        rendered = f"---\n{yaml_text}---\n"
    return (rendered + body).encode("utf-8")


def full_fixture() -> tuple[ResearchRelationRegistry, dict[str, bytes]]:
    entities: list[ResearchEntity] = []
    pages: dict[str, bytes] = {}
    by_type: dict[str, ResearchEntity] = {}
    for entity_type, (path, title) in DIRECT_LOCATIONS.items():
        entity = create_research_entity(
            PROJECT_ID,
            entity_type=entity_type,
            identity_key=f"{entity_type}:primary",
            title=title,
            knowledge_path=path,
        )
        entities.append(entity)
        by_type[entity_type] = entity
        pages[path] = page_bytes(path, title)

    metric = create_research_entity(
        PROJECT_ID,
        entity_type="metric",
        identity_key="metric:recall-at-10",
        title="Recall@10",
        knowledge_path="results/baseline.md",
        anchor="metric-recall-at-10",
    )
    question = create_research_entity(
        PROJECT_ID,
        entity_type="question",
        identity_key="question:data-gap",
        title="Does the result generalize?",
        knowledge_path="open-questions.md",
        anchor="question-data-gap",
    )
    entities.extend((metric, question))
    by_type["metric"] = metric
    by_type["question"] = question
    pages["open-questions.md"] = page_bytes(
        "open-questions.md",
        "Open Questions",
        body="# Open Questions\n\n## Does the result generalize?\n",
    )

    relations = (
        create_research_relation(
            PROJECT_ID,
            relation_type="evaluates-on",
            source_entity_id=by_type["method"].entity_id,
            target_entity_id=by_type["dataset"].entity_id,
            evidence_ids=(EVIDENCE_B, EVIDENCE_A),
        ),
        create_research_relation(
            PROJECT_ID,
            relation_type="produces",
            source_entity_id=by_type["experiment"].entity_id,
            target_entity_id=by_type["result"].entity_id,
        ),
        create_research_relation(
            PROJECT_ID,
            relation_type="measured-by",
            source_entity_id=by_type["result"].entity_id,
            target_entity_id=metric.entity_id,
        ),
        create_research_relation(
            PROJECT_ID,
            relation_type="raises",
            source_entity_id=by_type["claim"].entity_id,
            target_entity_id=question.entity_id,
        ),
    )
    return ResearchRelationRegistry(PROJECT_ID, tuple(entities), relations), pages


def canonical_rows(payload: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in payload.decode("utf-8").splitlines()]


def rows_bytes(rows: list[dict[str, object]]) -> bytes:
    return b"".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
        for row in rows
    )


class ResearchRelationIdentityTests(unittest.TestCase):
    def test_entity_identity_is_deterministic_and_not_title_derived(self) -> None:
        first = research_entity_id_for(PROJECT_ID, "paper", "doi:10.1/example")
        second = research_entity_id_for(PROJECT_ID, "paper", "doi:10.1/example")
        renamed = create_research_entity(
            PROJECT_ID,
            entity_type="paper",
            identity_key="doi:10.1/example",
            title="Renamed Paper",
        )
        self.assertEqual(first, second)
        self.assertEqual(renamed.entity_id, first)
        self.assertRegex(first, r"^ent-[0-9a-f]{64}$")

    def test_same_title_different_explicit_keys_remain_distinct(self) -> None:
        first = create_research_entity(
            PROJECT_ID,
            entity_type="dataset",
            identity_key="dataset:version-a",
            title="Shared Name",
        )
        second = create_research_entity(
            PROJECT_ID,
            entity_type="dataset",
            identity_key="dataset:version-b",
            title="Shared Name",
        )
        registry = ResearchRelationRegistry(PROJECT_ID, (first, second))
        self.assertNotEqual(first.entity_id, second.entity_id)
        self.assertEqual(len(registry.entities), 2)

    def test_project_and_entity_type_are_identity_bound(self) -> None:
        base = research_entity_id_for(PROJECT_ID, "paper", "shared-key")
        self.assertNotEqual(
            base,
            research_entity_id_for(OTHER_PROJECT_ID, "paper", "shared-key"),
        )
        self.assertNotEqual(
            base,
            research_entity_id_for(PROJECT_ID, "method", "shared-key"),
        )

    def test_all_roadmap_entity_types_have_explicit_locations(self) -> None:
        registry, _ = full_fixture()
        self.assertEqual(
            {entity.entity_type for entity in registry.entities},
            set(RESEARCH_ENTITY_TYPES),
        )
        metric = next(item for item in registry.entities if item.entity_type == "metric")
        question = next(
            item for item in registry.entities if item.entity_type == "question"
        )
        self.assertEqual(metric.knowledge_path, "results/baseline.md")
        self.assertEqual(metric.anchor, "metric-recall-at-10")
        self.assertEqual(question.knowledge_path, "open-questions.md")
        self.assertEqual(question.anchor, "question-data-gap")

    def test_unmaterialized_entities_are_allowed_without_inventing_paths(self) -> None:
        entity = create_research_entity(
            PROJECT_ID,
            entity_type="metric",
            identity_key="metric:not-yet-written",
            title="Future Metric",
        )
        self.assertIsNone(entity.knowledge_path)
        self.assertIsNone(entity.anchor)

    def test_entity_path_role_and_anchor_constraints_fail_closed(self) -> None:
        cases = (
            {
                "entity_type": "paper",
                "knowledge_path": "papers/index.md",
                "anchor": None,
                "message": "cannot bind",
            },
            {
                "entity_type": "paper",
                "knowledge_path": "papers/paper.md",
                "anchor": "section",
                "message": "not a body anchor",
            },
            {
                "entity_type": "metric",
                "knowledge_path": "results/result.md",
                "anchor": None,
                "message": "require an explicit body anchor",
            },
            {
                "entity_type": "metric",
                "knowledge_path": "methods/method.md",
                "anchor": "metric-one",
                "message": "cannot bind",
            },
            {
                "entity_type": "question",
                "knowledge_path": "open-questions.md",
                "anchor": "Question One",
                "message": "lowercase kebab-case",
            },
            {
                "entity_type": "question",
                "knowledge_path": None,
                "anchor": "question-one",
                "message": "unmaterialized",
            },
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case), self.assertRaisesRegex(
                ResearchRelationError, str(case["message"])
            ):
                create_research_entity(
                    PROJECT_ID,
                    entity_type=str(case["entity_type"]),
                    identity_key=f"case:{index}",
                    title="Entity",
                    knowledge_path=case["knowledge_path"],
                    anchor=case["anchor"],
                )

    def test_relation_identity_is_directed_and_not_evidence_derived(self) -> None:
        source = create_research_entity(
            PROJECT_ID,
            entity_type="method",
            identity_key="method:a",
            title="Method A",
        )
        target = create_research_entity(
            PROJECT_ID,
            entity_type="dataset",
            identity_key="dataset:b",
            title="Dataset B",
        )
        relation = create_research_relation(
            PROJECT_ID,
            relation_type="evaluates-on",
            source_entity_id=source.entity_id,
            target_entity_id=target.entity_id,
            evidence_ids=(EVIDENCE_B, EVIDENCE_A),
        )
        self.assertEqual(relation.evidence_ids, (EVIDENCE_A, EVIDENCE_B))
        self.assertEqual(
            relation.relation_id,
            research_relation_id_for(
                PROJECT_ID, "evaluates-on", source.entity_id, target.entity_id
            ),
        )
        self.assertNotEqual(
            relation.relation_id,
            research_relation_id_for(
                PROJECT_ID, "evaluates-on", target.entity_id, source.entity_id
            ),
        )

    def test_relation_evidence_iterables_fail_with_contract_errors(self) -> None:
        source = create_research_entity(
            PROJECT_ID,
            entity_type="method",
            identity_key="method:evidence-input",
            title="Evidence Input Method",
        )
        target = create_research_entity(
            PROJECT_ID,
            entity_type="dataset",
            identity_key="dataset:evidence-input",
            title="Evidence Input Dataset",
        )

        with self.assertRaisesRegex(ResearchRelationError, "evidence_ids entries"):
            create_research_relation(
                PROJECT_ID,
                relation_type="evaluates-on",
                source_entity_id=source.entity_id,
                target_entity_id=target.entity_id,
                evidence_ids=(EVIDENCE_A, 1),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ResearchRelationError, "iterable"):
            create_research_relation(
                PROJECT_ID,
                relation_type="evaluates-on",
                source_entity_id=source.entity_id,
                target_entity_id=target.entity_id,
                evidence_ids=None,  # type: ignore[arg-type]
            )

        relation_id = research_relation_id_for(
            PROJECT_ID, "evaluates-on", source.entity_id, target.entity_id
        )
        with self.assertRaisesRegex(ResearchRelationError, "iterable"):
            ResearchRelation(
                project_id=PROJECT_ID,
                relation_id=relation_id,
                relation_type="evaluates-on",
                source_entity_id=source.entity_id,
                target_entity_id=target.entity_id,
                evidence_ids=None,  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ResearchRelationError, "canonical sorted order"):
            ResearchRelation(
                project_id=PROJECT_ID,
                relation_id=relation_id,
                relation_type="evaluates-on",
                source_entity_id=source.entity_id,
                target_entity_id=target.entity_id,
                evidence_ids=(EVIDENCE_B, EVIDENCE_A),
            )

    def test_relation_labels_are_open_but_normalized_without_semantic_matrix(self) -> None:
        question = create_research_entity(
            PROJECT_ID,
            entity_type="question",
            identity_key="question:q",
            title="Question",
        )
        paper = create_research_entity(
            PROJECT_ID,
            entity_type="paper",
            identity_key="paper:p",
            title="Paper",
        )
        relation = create_research_relation(
            PROJECT_ID,
            relation_type="challenges-assumption-in",
            source_entity_id=question.entity_id,
            target_entity_id=paper.entity_id,
        )
        registry = ResearchRelationRegistry(PROJECT_ID, (question, paper), (relation,))
        self.assertEqual(registry.relations[0].relation_type, "challenges-assumption-in")
        with self.assertRaisesRegex(ResearchRelationError, "lowercase kebab-case"):
            create_research_relation(
                PROJECT_ID,
                relation_type="Challenges Assumption",
                source_entity_id=question.entity_id,
                target_entity_id=paper.entity_id,
            )

    def test_self_loop_duplicate_and_unknown_endpoint_relations_are_rejected(self) -> None:
        source = create_research_entity(
            PROJECT_ID,
            entity_type="paper",
            identity_key="paper:a",
            title="Paper A",
        )
        target = create_research_entity(
            PROJECT_ID,
            entity_type="claim",
            identity_key="claim:b",
            title="Claim B",
        )
        with self.assertRaisesRegex(ResearchRelationConflictError, "itself"):
            create_research_relation(
                PROJECT_ID,
                relation_type="supports",
                source_entity_id=source.entity_id,
                target_entity_id=source.entity_id,
            )
        relation = create_research_relation(
            PROJECT_ID,
            relation_type="supports",
            source_entity_id=source.entity_id,
            target_entity_id=target.entity_id,
        )
        with self.assertRaisesRegex(ResearchRelationConflictError, "duplicate"):
            ResearchRelationRegistry(
                PROJECT_ID, (source, target), (relation, relation)
            )
        unknown = create_research_entity(
            PROJECT_ID,
            entity_type="result",
            identity_key="result:unknown",
            title="Unknown Result",
        )
        missing_relation = create_research_relation(
            PROJECT_ID,
            relation_type="supports",
            source_entity_id=source.entity_id,
            target_entity_id=unknown.entity_id,
        )
        with self.assertRaisesRegex(ResearchRelationConflictError, "unknown entity"):
            ResearchRelationRegistry(
                PROJECT_ID, (source, target), (missing_relation,)
            )

    def test_cross_project_records_and_duplicate_locations_are_rejected(self) -> None:
        local = create_research_entity(
            PROJECT_ID,
            entity_type="paper",
            identity_key="paper:local",
            title="Local",
            knowledge_path="papers/local.md",
        )
        remote = create_research_entity(
            OTHER_PROJECT_ID,
            entity_type="paper",
            identity_key="paper:remote",
            title="Remote",
        )
        with self.assertRaisesRegex(ResearchRelationConflictError, "different project"):
            ResearchRelationRegistry(PROJECT_ID, (local, remote))
        duplicate_location = create_research_entity(
            PROJECT_ID,
            entity_type="paper",
            identity_key="paper:other-identity",
            title="Other title",
            knowledge_path="papers/local.md",
        )
        with self.assertRaisesRegex(ResearchRelationConflictError, "Markdown location"):
            ResearchRelationRegistry(PROJECT_ID, (local, duplicate_location))

    def test_registry_rejects_non_record_values_with_contract_error(self) -> None:
        with self.assertRaisesRegex(ResearchRelationError, "ResearchEntity"):
            ResearchRelationRegistry(PROJECT_ID, (object(),))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ResearchRelationError, "ResearchRelation"):
            ResearchRelationRegistry(PROJECT_ID, (), (object(),))  # type: ignore[arg-type]


class ResearchRelationSerializationTests(unittest.TestCase):
    def test_canonical_jsonl_round_trip_is_exact(self) -> None:
        registry, _ = full_fixture()
        payload = registry.serialized_bytes()
        parsed = deserialize_research_relation_registry(payload, project_id=PROJECT_ID)
        self.assertEqual(parsed, registry)
        self.assertEqual(parsed.serialized_bytes(), payload)
        self.assertTrue(payload.endswith(b"\n"))
        self.assertNotIn(b"\r", payload)

    def test_unicode_line_separators_inside_json_strings_round_trip(self) -> None:
        entity = create_research_entity(
            PROJECT_ID,
            entity_type="paper",
            identity_key="paper:line\u2028paragraph\u2029separator",
            title="Line\u2028Paragraph\u2029Separator",
        )
        registry = ResearchRelationRegistry(PROJECT_ID, (entity,))
        payload = registry.serialized_bytes()

        self.assertIn("\u2028".encode(), payload)
        self.assertIn("\u2029".encode(), payload)
        parsed = deserialize_research_relation_registry(payload, project_id=PROJECT_ID)
        self.assertEqual(parsed, registry)
        self.assertEqual(parsed.serialized_bytes(), payload)

    def test_empty_registry_is_a_valid_canonical_summary(self) -> None:
        registry = ResearchRelationRegistry(PROJECT_ID)
        parsed = deserialize_research_relation_registry(registry.serialized_bytes())
        self.assertEqual(parsed, registry)

    def test_strict_utf8_line_and_canonical_byte_rules(self) -> None:
        registry, _ = full_fixture()
        payload = registry.serialized_bytes()
        invalid_payloads = (
            (b"\xff", "strict UTF-8"),
            (payload.rstrip(b"\n"), "end with LF"),
            (payload.replace(b"\n", b"\r\n"), "canonically serialized"),
            (payload + b"\n", "blank rows"),
            (payload.replace(b":", b": ", 1), "canonically serialized"),
        )
        for candidate, message in invalid_payloads:
            with self.subTest(message=message), self.assertRaisesRegex(
                ResearchRelationError, message
            ):
                deserialize_research_relation_registry(candidate)

    def test_duplicate_json_keys_and_non_finite_values_are_rejected(self) -> None:
        registry, _ = full_fixture()
        lines = registry.serialized_bytes().decode("utf-8").splitlines()
        duplicate = lines.copy()
        duplicate[0] = duplicate[0].replace(
            '"schema_version":1',
            '"schema_version":1,"schema_version":1',
            1,
        )
        with self.assertRaisesRegex(ResearchRelationError, "duplicate JSON key"):
            deserialize_research_relation_registry(
                ("\n".join(duplicate) + "\n").encode("utf-8")
            )
        non_finite = lines.copy()
        non_finite[0] = non_finite[0].replace('"entity_count":10', '"entity_count":NaN')
        with self.assertRaisesRegex(ResearchRelationError, "non-finite"):
            deserialize_research_relation_registry(
                ("\n".join(non_finite) + "\n").encode("utf-8")
            )

    def test_unknown_missing_and_future_schema_fields_fail_closed(self) -> None:
        registry, _ = full_fixture()
        rows = canonical_rows(registry.serialized_bytes())

        unknown = deepcopy(rows)
        unknown[0]["extra"] = True
        with self.assertRaisesRegex(ResearchRelationError, "unknown=.*extra"):
            deserialize_research_relation_registry(rows_bytes(unknown))

        missing = deepcopy(rows)
        del missing[1]["title"]
        with self.assertRaisesRegex(ResearchRelationError, "missing=.*title"):
            deserialize_research_relation_registry(rows_bytes(missing))

        future = deepcopy(rows)
        future[0]["schema_version"] = 2
        with self.assertRaises(UnsupportedResearchRelationSchemaVersionError):
            deserialize_research_relation_registry(rows_bytes(future))

        legacy = deepcopy(rows)
        legacy[0]["schema_version"] = 0
        with self.assertRaisesRegex(ResearchRelationError, "must equal 1"):
            deserialize_research_relation_registry(rows_bytes(legacy))

    def test_counts_record_order_and_project_binding_are_enforced(self) -> None:
        registry, _ = full_fixture()
        rows = canonical_rows(registry.serialized_bytes())

        bad_count = deepcopy(rows)
        bad_count[0]["entity_count"] = 999
        with self.assertRaisesRegex(ResearchRelationError, "row counts"):
            deserialize_research_relation_registry(rows_bytes(bad_count))

        swapped = deepcopy(rows)
        swapped[1], swapped[2] = swapped[2], swapped[1]
        with self.assertRaisesRegex(ResearchRelationError, "canonical entity_id order"):
            deserialize_research_relation_registry(rows_bytes(swapped))

        wrong_project = deepcopy(rows)
        wrong_project[1]["project_id"] = OTHER_PROJECT_ID
        with self.assertRaisesRegex(ResearchRelationConflictError, "project_id"):
            deserialize_research_relation_registry(rows_bytes(wrong_project))

        with self.assertRaisesRegex(ResearchRelationConflictError, "requested project_id"):
            deserialize_research_relation_registry(
                registry.serialized_bytes(), project_id=OTHER_PROJECT_ID
            )

    def test_recomputed_entity_relation_ids_and_evidence_order_are_enforced(self) -> None:
        registry, _ = full_fixture()
        rows = canonical_rows(registry.serialized_bytes())

        bad_entity = deepcopy(rows)
        bad_entity[1]["entity_id"] = "ent-" + "f" * 64
        with self.assertRaisesRegex(ResearchEntityBindingError, "entity_id does not match"):
            deserialize_research_relation_registry(rows_bytes(bad_entity))

        first_relation = 1 + int(rows[0]["entity_count"])
        bad_relation = deepcopy(rows)
        bad_relation[first_relation]["relation_id"] = "rel-" + "f" * 64
        with self.assertRaisesRegex(ResearchRelationConflictError, "relation_id"):
            deserialize_research_relation_registry(rows_bytes(bad_relation))

        unsorted_evidence = deepcopy(rows)
        relation_row = next(
            row for row in unsorted_evidence if row.get("evidence_ids") == [EVIDENCE_A, EVIDENCE_B]
        )
        relation_row["evidence_ids"] = [EVIDENCE_B, EVIDENCE_A]
        with self.assertRaisesRegex(ResearchRelationError, "canonical sorted order"):
            deserialize_research_relation_registry(rows_bytes(unsorted_evidence))

        duplicate_evidence = deepcopy(rows)
        relation_row = next(
            row for row in duplicate_evidence if row.get("evidence_ids") == [EVIDENCE_A, EVIDENCE_B]
        )
        relation_row["evidence_ids"] = [EVIDENCE_A, EVIDENCE_A]
        with self.assertRaisesRegex(ResearchRelationConflictError, "duplicate Evidence"):
            deserialize_research_relation_registry(rows_bytes(duplicate_evidence))


class ResearchRelationBindingAndIndexTests(unittest.TestCase):
    def test_all_page_backed_entities_bind_to_current_schema_v2(self) -> None:
        registry, pages = full_fixture()
        parsed = validate_research_entity_bindings(registry, knowledge_pages=pages)
        self.assertEqual(set(parsed), set(pages))
        self.assertEqual(
            parsed["papers/transformer.md"].frontmatter.title,
            "Transformer Paper",
        )

    def test_missing_stale_title_wrong_project_and_legacy_pages_are_rejected(self) -> None:
        registry, pages = full_fixture()

        missing = dict(pages)
        del missing["papers/transformer.md"]
        with self.assertRaisesRegex(ResearchEntityBindingError, "page is missing"):
            validate_research_entity_bindings(registry, knowledge_pages=missing)

        stale_title = dict(pages)
        stale_title["papers/transformer.md"] = page_bytes(
            "papers/transformer.md", "Renamed without registry update"
        )
        with self.assertRaisesRegex(ResearchEntityBindingError, "title is stale"):
            validate_research_entity_bindings(registry, knowledge_pages=stale_title)

        wrong_project = dict(pages)
        wrong_project["papers/transformer.md"] = page_bytes(
            "papers/transformer.md",
            "Transformer Paper",
            project_id=OTHER_PROJECT_ID,
        )
        with self.assertRaisesRegex(ResearchEntityBindingError, "another project"):
            validate_research_entity_bindings(registry, knowledge_pages=wrong_project)

        legacy = dict(pages)
        legacy["papers/transformer.md"] = page_bytes(
            "papers/transformer.md",
            "Transformer Paper",
            schema_version=1,
        )
        with self.assertRaisesRegex(ResearchEntityBindingError, "current Schema v2"):
            validate_research_entity_bindings(registry, knowledge_pages=legacy)

        future = dict(pages)
        future["papers/transformer.md"] = page_bytes(
            "papers/transformer.md",
            "Transformer Paper",
            schema_version=3,
        )
        with self.assertRaisesRegex(ResearchEntityBindingError, "newer than supported"):
            validate_research_entity_bindings(registry, knowledge_pages=future)

    def test_noncanonical_mapping_key_and_wrong_page_type_fail_closed(self) -> None:
        registry, pages = full_fixture()
        noncanonical = dict(pages)
        noncanonical["papers\\transformer.md"] = noncanonical.pop(
            "papers/transformer.md"
        )
        with self.assertRaises(ResearchEntityBindingError):
            validate_research_entity_bindings(registry, knowledge_pages=noncanonical)

        wrong_type = dict(pages)
        wrong_type["papers/transformer.md"] = page_bytes(
            "methods/attention.md", "Transformer Paper"
        )
        with self.assertRaisesRegex(ResearchEntityBindingError, "invalid"):
            validate_research_entity_bindings(registry, knowledge_pages=wrong_type)

    def test_project_index_has_deterministic_groups_and_bidirectional_backlinks(self) -> None:
        registry, pages = full_fixture()
        index = build_research_project_index(
            registry,
            knowledge_pages=pages,
            known_evidence_ids=(EVIDENCE_A, EVIDENCE_B),
        )
        payload = index.as_dict()
        self.assertEqual(payload["entity_count"], len(RESEARCH_ENTITY_TYPES))
        self.assertEqual(payload["relation_count"], 4)
        group_types = [group["entity_type"] for group in payload["entity_groups"]]
        self.assertEqual(group_types, sorted(RESEARCH_ENTITY_TYPES))

        entries = {entry.entity_id: entry for entry in index.entities}
        for relation in registry.relations:
            self.assertIn(
                relation.relation_id,
                entries[relation.source_entity_id].outgoing_relation_ids,
            )
            self.assertIn(
                relation.relation_id,
                entries[relation.target_entity_id].incoming_relation_ids,
            )
        self.assertEqual(index.serialized_bytes(), index.serialized_bytes())
        self.assertTrue(index.serialized_bytes().endswith(b"\n"))

    def test_project_index_rejects_unknown_or_malformed_evidence_ids(self) -> None:
        registry, pages = full_fixture()
        with self.assertRaisesRegex(ResearchRelationConflictError, "unknown Evidence"):
            build_research_project_index(
                registry,
                knowledge_pages=pages,
                known_evidence_ids=(EVIDENCE_A,),
            )
        with self.assertRaisesRegex(ResearchRelationError, "invalid ID"):
            build_research_project_index(
                registry,
                knowledge_pages=pages,
                known_evidence_ids=(EVIDENCE_A, "not-an-evidence-id"),
            )

    def test_unmaterialized_entities_need_no_page_but_remain_indexed(self) -> None:
        entity = create_research_entity(
            PROJECT_ID,
            entity_type="question",
            identity_key="question:future",
            title="Future Question",
        )
        registry = ResearchRelationRegistry(PROJECT_ID, (entity,))
        index = build_research_project_index(registry, knowledge_pages={})
        self.assertEqual(index.entities[0].entity_id, entity.entity_id)
        self.assertIsNone(index.entities[0].knowledge_path)

    def test_callers_inputs_are_unchanged_and_no_filesystem_access_occurs(self) -> None:
        registry, pages = full_fixture()
        original_pages = deepcopy(pages)
        original_registry_bytes = registry.serialized_bytes()
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            before = tuple(root.rglob("*"))
            with (
                patch("builtins.open", side_effect=AssertionError("unexpected open")),
                patch.object(Path, "open", side_effect=AssertionError("unexpected open")),
            ):
                index = build_research_project_index(
                    registry,
                    knowledge_pages=pages,
                    known_evidence_ids=(EVIDENCE_A, EVIDENCE_B),
                )
            after = tuple(root.rglob("*"))
        self.assertEqual(index.project_id, PROJECT_ID)
        self.assertEqual(pages, original_pages)
        self.assertEqual(registry.serialized_bytes(), original_registry_bytes)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
