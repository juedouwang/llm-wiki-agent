from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tools.evidence_registry import (
    EVIDENCE_HASH_ALGORITHM,
    EVIDENCE_IDENTITY_VERSION,
    EVIDENCE_KIND,
    EVIDENCE_REGISTRY_KIND,
    EVIDENCE_REGISTRY_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_VERSION,
    EXCERPT_TEXT_ENCODING,
    EvidenceConflictError,
    EvidenceError,
    EvidenceMismatchError,
    evidence_id_for,
    excerpt_sha256,
    load_evidence_registry,
    register_evidence,
    validate_evidence,
)
from tools.extraction_schema import (
    EXTRACTION_SCHEMA_VERSION,
    LineRangeLocator,
    NotebookCellLocator,
    PdfPageLocator,
    SectionLocator,
    SymbolLocator,
    TableRangeLocator,
)
from tools.project_inventory import inventory_project
from tools.project_registry import register_project
from tools.source_registry import load_source_registry, sync_source_registry


class EvidenceSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.project = self.root / "research-project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "src" / "model.py").write_text(
            "VALUE = 1\nprint(VALUE)\n",
            encoding="utf-8",
            newline="\n",
        )
        self.registration = register_project(
            self.workspace,
            self.project,
            project_id="evidence-study",
        )
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)
        self.source_registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        self.source = self.source_registry.current_by_path["src/model.py"]
        self.content_hash = self.source.current_content_hash
        assert self.content_hash is not None

    def source_snapshot(self) -> tuple[tuple[str, ...], dict[str, tuple[str, int]]]:
        directories: list[str] = []
        files: dict[str, tuple[str, int]] = {}
        for directory, dirnames, filenames in os.walk(self.project):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            for name in dirnames:
                path = base / name
                if not path.is_symlink():
                    directories.append(path.relative_to(self.project).as_posix())
            for name in filenames:
                path = base / name
                if not path.is_symlink():
                    files[path.relative_to(self.project).as_posix()] = (
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        path.stat().st_mtime_ns,
                    )
        return tuple(sorted(directories)), files

    def register(
        self,
        locator: object = LineRangeLocator(1, 2),
        excerpt: str | bytes = "VALUE = 1\nprint(VALUE)\n",
    ):
        return register_evidence(
            self.workspace,
            self.registration.project_id,
            source_id=self.source.source_id,
            content_hash=self.content_hash,
            locator=locator,  # type: ignore[arg-type]
            excerpt=excerpt,
        )

    @staticmethod
    def encode_rows(rows: list[dict[str, object]]) -> bytes:
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

    def rows(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.registration.layout.evidence_file.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

    def test_all_locator_variants_round_trip_canonically(self) -> None:
        before = self.source_snapshot()
        locators = (
            LineRangeLocator(1, 2),
            PdfPageLocator(3),
            NotebookCellLocator(4, "cell-four"),
            TableRangeLocator("Results", "B2", "D8"),
            SectionLocator(("Methods", "Training"), 10, 27),
            SymbolLocator("train_model", 31, 56),
        )
        expected_ids = set()
        for index, locator in enumerate(locators):
            result = self.register(locator, f"exact excerpt {index}\n")
            expected_ids.add(result.evidence.evidence_id)
            self.assertTrue(result.wrote_registry)

        registry = load_evidence_registry(
            self.workspace,
            self.registration.project_id,
        )
        self.assertEqual(len(registry.records), len(locators))
        self.assertEqual(set(registry.by_evidence_id), expected_ids)
        self.assertEqual(
            [record.locator for record in registry.records],
            sorted(
                [record.locator for record in registry.records],
                key=lambda locator: evidence_id_for(
                    project_id=self.registration.project_id,
                    source_id=self.source.source_id,
                    content_hash=self.content_hash,
                    locator=locator,
                    excerpt_hash=next(
                        record.excerpt_hash
                        for record in registry.records
                        if record.locator == locator
                    ),
                ),
            ),
        )
        raw_lines = self.registration.layout.evidence_file.read_bytes().splitlines(
            keepends=True
        )
        for raw_line in raw_lines:
            decoded = json.loads(raw_line)
            self.assertEqual(raw_line, self.encode_rows([decoded]))
        self.assertEqual(before, self.source_snapshot())

    def test_stable_identity_and_exact_excerpt_hash_semantics(self) -> None:
        locator = LineRangeLocator(1, 1)
        first = self.register(locator, "?\r\n")
        second = self.register(locator, "?\r\n")
        self.assertEqual(first.evidence.evidence_id, second.evidence.evidence_id)
        self.assertFalse(second.wrote_registry)
        self.assertEqual(second.evidence_count, 1)
        self.assertEqual(
            first.evidence.excerpt_hash,
            hashlib.sha256("?\r\n".encode("utf-8")).hexdigest(),
        )
        self.assertNotEqual(excerpt_sha256("?\r\n"), excerpt_sha256("?\n"))
        self.assertNotEqual(excerpt_sha256("?"), excerpt_sha256("e\u0301"))
        self.assertEqual(excerpt_sha256(b"\xff"), hashlib.sha256(b"\xff").hexdigest())

    def test_artifact_summary_and_layout_contract(self) -> None:
        result = self.register()
        self.assertEqual(
            result.evidence_file,
            self.registration.layout.machine_root / "evidence.jsonl",
        )
        rows = self.rows()
        self.assertEqual(
            rows[0],
            {
                "schema_version": EVIDENCE_SCHEMA_VERSION,
                "kind": EVIDENCE_REGISTRY_KIND,
                "registry_version": EVIDENCE_REGISTRY_VERSION,
                "record_type": "summary",
                "project_id": self.registration.project_id,
                "evidence_version": EVIDENCE_VERSION,
                "identity_version": EVIDENCE_IDENTITY_VERSION,
                "hash_algorithm": EVIDENCE_HASH_ALGORITHM,
                "excerpt_text_encoding": EXCERPT_TEXT_ENCODING,
                "locator_schema_version": EXTRACTION_SCHEMA_VERSION,
                "evidence_count": 1,
            },
        )
        self.assertEqual(rows[1]["kind"], EVIDENCE_KIND)
        self.assertEqual(rows[1]["evidence_id"], result.evidence.evidence_id)

    def test_source_and_recorded_version_binding_is_strict(self) -> None:
        with self.assertRaises(EvidenceConflictError):
            register_evidence(
                self.workspace,
                self.registration.project_id,
                source_id="src-00000000000000000000000000000000",
                content_hash=self.content_hash,
                locator=LineRangeLocator(1, 1),
                excerpt="VALUE = 1\n",
            )
        with self.assertRaises(EvidenceConflictError):
            register_evidence(
                self.workspace,
                self.registration.project_id,
                source_id=self.source.source_id,
                content_hash="0" * 64,
                locator=LineRangeLocator(1, 1),
                excerpt="VALUE = 1\n",
            )
        with self.assertRaises(EvidenceConflictError):
            register_evidence(
                self.workspace,
                self.registration.project_id,
                source_id=self.source.source_id,
                source_version=2,
                content_hash=self.content_hash,
                locator=LineRangeLocator(1, 1),
                excerpt="VALUE = 1\n",
            )
        with self.assertRaises(EvidenceMismatchError):
            register_evidence(
                self.workspace,
                self.registration.project_id,
                source_id=self.source.source_id,
                source_version=1,
                content_hash="0" * 64,
                locator=LineRangeLocator(1, 1),
                excerpt="VALUE = 1\n",
            )

    def test_excerpt_mismatch_is_rejected_and_validation_is_deterministic(self) -> None:
        digest = excerpt_sha256("VALUE = 1\n")
        with self.assertRaises(EvidenceMismatchError):
            register_evidence(
                self.workspace,
                self.registration.project_id,
                source_id=self.source.source_id,
                content_hash=self.content_hash,
                locator=LineRangeLocator(1, 1),
                excerpt="VALUE = 2\n",
                expected_excerpt_hash=digest,
            )
        evidence = self.register(LineRangeLocator(1, 1), "VALUE = 1\n").evidence
        valid = validate_evidence(
            evidence,
            self.source_registry,
            observed_content_hash=self.content_hash,
            excerpt="VALUE = 1\n",
        )
        self.assertTrue(valid.valid)
        mismatch = validate_evidence(
            evidence,
            self.source_registry,
            excerpt="VALUE = 2\n",
        )
        self.assertFalse(mismatch.valid)
        self.assertEqual(mismatch.reason_code, "excerpt-hash-mismatch")
        content_mismatch = validate_evidence(
            evidence,
            self.source_registry,
            observed_content_hash="0" * 64,
        )
        self.assertFalse(content_mismatch.valid)
        self.assertEqual(
            content_mismatch.reason_code,
            "observed-content-hash-mismatch",
        )

    def test_content_transition_invalidates_old_evidence_without_deleting_it(self) -> None:
        evidence = self.register(LineRangeLocator(1, 1), "VALUE = 1\n").evidence
        (self.project / "src" / "model.py").write_text(
            "VALUE = 2\nprint(VALUE)\n",
            encoding="utf-8",
            newline="\n",
        )
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)
        current_sources = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        validation = validate_evidence(evidence, current_sources)
        self.assertFalse(validation.valid)
        self.assertEqual(
            validation.reason_code,
            "current-content-hash-mismatch",
        )
        loaded = load_evidence_registry(
            self.workspace,
            self.registration.project_id,
        )
        self.assertEqual(loaded.records, (evidence,))

    def test_loader_rejects_bad_versions_hashes_fields_ids_and_bindings(self) -> None:
        self.register()
        baseline = self.rows()
        mutations: list[tuple[str, list[dict[str, object]]]] = []

        missing_schema = copy.deepcopy(baseline)
        del missing_schema[1]["schema_version"]
        mutations.append(("missing schema", missing_schema))

        boolean_schema = copy.deepcopy(baseline)
        boolean_schema[1]["schema_version"] = True
        mutations.append(("boolean schema", boolean_schema))

        future_schema = copy.deepcopy(baseline)
        future_schema[1]["schema_version"] = EVIDENCE_SCHEMA_VERSION + 1
        mutations.append(("future schema", future_schema))

        nested_legacy = copy.deepcopy(baseline)
        assert isinstance(nested_legacy[1]["locator"], dict)
        nested_legacy[1]["locator"]["schema_version"] = 0
        mutations.append(("nested legacy", nested_legacy))

        nested_boolean = copy.deepcopy(baseline)
        assert isinstance(nested_boolean[1]["locator"], dict)
        nested_boolean[1]["locator"]["schema_version"] = True
        mutations.append(("nested boolean", nested_boolean))

        nested_future = copy.deepcopy(baseline)
        assert isinstance(nested_future[1]["locator"], dict)
        nested_future[1]["locator"]["schema_version"] = (
            EXTRACTION_SCHEMA_VERSION + 1
        )
        mutations.append(("nested future", nested_future))

        unknown_field = copy.deepcopy(baseline)
        unknown_field[1]["surprise"] = True
        mutations.append(("unknown field", unknown_field))

        bad_hash = copy.deepcopy(baseline)
        bad_hash[1]["excerpt_hash"] = "ABC"
        mutations.append(("bad hash", bad_hash))

        bad_id = copy.deepcopy(baseline)
        bad_id[1]["evidence_id"] = "evd-" + "0" * 64
        mutations.append(("bad id", bad_id))

        bad_source_version = copy.deepcopy(baseline)
        bad_source_version[1]["source_version"] = True
        mutations.append(("boolean source version", bad_source_version))

        unknown_source = copy.deepcopy(baseline)
        unknown_source[1]["source_id"] = "src-00000000000000000000000000000000"
        unknown_source[1]["evidence_id"] = evidence_id_for(
            project_id=self.registration.project_id,
            source_id=unknown_source[1]["source_id"],  # type: ignore[arg-type]
            content_hash=unknown_source[1]["content_hash"],  # type: ignore[arg-type]
            locator=unknown_source[1]["locator"],  # type: ignore[arg-type]
            excerpt_hash=unknown_source[1]["excerpt_hash"],  # type: ignore[arg-type]
        )
        mutations.append(("unknown source", unknown_source))

        for label, rows in mutations:
            with self.subTest(label=label):
                self.registration.layout.evidence_file.write_bytes(
                    self.encode_rows(rows)
                )
                with self.assertRaises(EvidenceError):
                    load_evidence_registry(
                        self.workspace,
                        self.registration.project_id,
                    )
        self.registration.layout.evidence_file.write_bytes(self.encode_rows(baseline))

    def test_loader_rejects_count_order_duplicate_keys_and_nonfinite_json(self) -> None:
        self.register(LineRangeLocator(1, 1), "first")
        self.register(LineRangeLocator(2, 2), "second")
        baseline = self.rows()

        bad_count = copy.deepcopy(baseline)
        bad_count[0]["evidence_count"] = 99
        self.registration.layout.evidence_file.write_bytes(self.encode_rows(bad_count))
        with self.assertRaises(EvidenceConflictError):
            load_evidence_registry(self.workspace, self.registration.project_id)

        out_of_order = [baseline[0], baseline[2], baseline[1]]
        self.registration.layout.evidence_file.write_bytes(
            self.encode_rows(out_of_order)
        )
        with self.assertRaises(EvidenceConflictError):
            load_evidence_registry(self.workspace, self.registration.project_id)

        duplicate = [copy.deepcopy(baseline[0]), baseline[1], baseline[1]]
        duplicate[0]["evidence_count"] = 2
        self.registration.layout.evidence_file.write_bytes(self.encode_rows(duplicate))
        with self.assertRaises(EvidenceConflictError):
            load_evidence_registry(self.workspace, self.registration.project_id)

        duplicate_key = (
            '{"schema_version":1,"schema_version":1,"kind":"x"}\n'.encode()
        )
        self.registration.layout.evidence_file.write_bytes(duplicate_key)
        with self.assertRaises(EvidenceError):
            load_evidence_registry(self.workspace, self.registration.project_id)

        self.registration.layout.evidence_file.write_text(
            '{"schema_version":1,"kind":NaN}\n',
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaises(EvidenceError):
            load_evidence_registry(self.workspace, self.registration.project_id)

    def test_concurrent_identical_writers_create_one_record(self) -> None:
        before = self.source_snapshot()

        def write_once(_: int):
            return self.register(LineRangeLocator(1, 1), "VALUE = 1\n")

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(write_once, range(16)))
        registry = load_evidence_registry(
            self.workspace,
            self.registration.project_id,
        )
        self.assertEqual(len(registry.records), 1)
        self.assertEqual({item.evidence.evidence_id for item in results}, {
            registry.records[0].evidence_id
        })
        self.assertEqual(sum(item.wrote_registry for item in results), 1)
        self.assertEqual(before, self.source_snapshot())

    def test_missing_registry_and_invalid_excerpt_types_fail_closed(self) -> None:
        with self.assertRaises(EvidenceError):
            load_evidence_registry(self.workspace, self.registration.project_id)
        with self.assertRaises(EvidenceError):
            self.register(excerpt=object())  # type: ignore[arg-type]
        with self.assertRaises(EvidenceError):
            self.register(excerpt="\ud800")


if __name__ == "__main__":
    unittest.main()
