# Visual and OCR extraction (C-05)

C-05 adds a bounded, source-read-only visual follow-up layer for standalone
research images and PDF pages that C-04 explicitly marked for OCR or visual
review. The implementation lives in:

- `tools/visual_selection.py` — current-grounded B-07 authorization;
- `tools/visual_extractor.py` — local decoding, OCR/vision backend contracts,
  PDF follow-up composition, and selected-only project execution;
- `tools/extraction_schema.py` — Schema v1 `ImageRegionLocator`;
- `tools/source_access.py` — exact standalone image-region reopening.

The pipeline does not persist a new artifact, modify the Manifest, register
Evidence, write curated Markdown, or call a network service implicitly.

## Authorization boundary

`build_visual_selection(workspace_root, project_id)` obtains authorization only
through `load_current_reading_priority(...)`. A visual candidate is executable
only when both its B-07 `deep_read_status` and its C-05 decision are
`selected`. `deferred` and `limited` records remain audit information; they are
never treated as backend instructions.

The candidate formats are exactly:

```text
png, jpeg, gif, tiff, bmp, pdf
```

The selection report is an in-memory strict Schema v1 projection containing the
exact current Manifest identity, a canonical B-07 payload hash, policy decisions,
selected bytes, and every candidate's path/hash/size/mtime/format/research role,
rank, reason codes, and references. Selection does not open source content.

`execute_selected_visuals(...)` reauthorizes the complete report immediately
before each source read. It resolves only normalized Manifest-relative paths,
rejects symlink/reparse ancestors and non-regular files, opens with no-follow
semantics where the platform supports them, and verifies Manifest size, mtime,
and SHA-256. It then:

1. processes only the verified byte snapshot;
2. re-reads and verifies the source after backend work;
3. rebuilds the current-grounded selection and requires exact equality; and
4. re-reads the source once more before returning.

Manifest, policy, priority, registration, or source mutation fails closed.
Research project files are never modified.

For every backend or PDF renderer declaring `external_send = True`, project
execution also performs the same exact selection, registration, and source
signature/hash checks at the **send boundary**, after backend identity and the
callable method have been resolved and immediately before invocation. A policy
change during identity resolution therefore prevents the backend method from
running. This is a point-in-time authorization check: no implementation can
retroactively revoke bytes after an external method has already been invoked,
so post-invocation checks remain defense in depth rather than a claim of
retroactive non-disclosure. Low-level callers retain the explicit
`allow_external_send` switch and may provide an `external_send_authorizer`
callback when they need an equivalent currentness check.

## Standalone raster contract

Pillow decodes each retained frame locally. EXIF orientation is normalized
before coordinates or hashes are defined. The first version emits one whole-frame
region per retained frame:

```python
ImageRegionLocator(
    frame_index=0,
    x=0,
    y=0,
    width=decoded_width,
    height=decoded_height,
)
```

`frame_index`, `x`, and `y` are zero-based; the coordinate origin is the
normalized frame's top-left corner; width and height are positive pixels.
Arbitrary in-bounds regions can later be reopened through `source_access`.

A derived OCR or visual block records the normalized dimensions, raw RGBA hash,
normalized PNG hash, backend identity/version, operation, output hash, and
backend metadata. OCR/vision text is derived output. The pixel-region hash
verifies the source pixels and must not be represented as verification of the
derived text.

Exact reopening of an `ImageRegionLocator` returns canonical compact JSON with:

```text
frame_index, x, y, width, height,
decoded_width, decoded_height, rgba_sha256
```

and `excerpt_format = image-region-rgba-sha256`. PDF crops deliberately remain
outside this locator contract; whole PDF follow-up pages retain
`PdfPageLocator`.

## PDF composition

C-05 first runs the existing deterministic C-04 PDF extractor. It preserves all
native C-04 page blocks and metadata. Only the union of:

```text
ocr_recommended_pages
visual_review_recommended_pages
```

is eligible for rendering. `pages_with_images` alone is not authorization.
A selected PDF with no C-04 follow-up page returns `processed` with
`visual-followup-not-required`, and no renderer or OCR/vision backend is called.

The default local renderer uses direct images exposed by `pypdf`; it does not
silently add PyMuPDF, download a model, or rasterize arbitrary PDF drawing
commands. A page with no recoverable direct image has an explicit partial state.
Tests can inject a deterministic `PdfPageRenderer` for architecture/result and
failure fixtures. Derived PDF blocks keep the same one-based `PdfPageLocator` as
the native page. The total pixel limit is document-wide across all rendered
follow-up pages, not a fresh allowance per page. Each renderer call receives the
remaining budget. Custom renderer outputs are checked for type, page locator,
source format, frame/index semantics, unique image indexes, per-frame bounds,
per-page payload count, and cumulative pixels; contract violations fail without
a document.

## Backends and external-send policy

`TesseractCliOcrBackend` is the default OCR backend. It uses only an already
installed local `tesseract` executable and a bounded temporary PNG. If the
binary is absent, the extraction is `partial` with
`ocr-backend-unavailable`; no text is fabricated. C-05 does not claim
Tesseract quality in an environment where that executable is unavailable.

Semantic visual analysis is an injectable backend in C-05. No network/model
backend is configured by default. Tesseract temporary-file deletion failures are
explicit extraction failures rather than silently leaving sensitive pixels on
disk. Every backend declares:

```text
name, version, external_send
```

Local-content permission and raw external-send permission are independent.
Even for a locally readable selected source, a backend whose
`external_send = True` is not invoked unless the current B-02 decision is
`raw_external_send == allowed`. Denial is recorded as
`raw-external-send-denied`.

## Resource bounds

The default v1 limits are:

| Bound | Default |
|---|---:|
| source bytes | 32 MiB |
| normalized width / height | 16,384 px each |
| pixels per frame | 64,000,000 |
| total pixels | 96,000,000 |
| raster frames | 4 |
| derived output characters | 200,000 |
| PDF follow-up pages | 64 |
| direct PDF images per page | 16 |
| one direct PDF image | 32 MiB |
| backend timeout | 30 s |

Malformed images, Pillow decompression-bomb rejection, first-frame dimension or
pixel violations, source/hash mismatches, custom PDF renderer contract
violations, and all configured text backends crashing without usable
native/derived text fail without a document. If at least one raster frame was
already decoded safely, a malformed or over-limit later frame retains those
earlier frames and returns `partial` with
`image-frame-processing-failed`. Frame-count and cumulative-pixel omissions,
backend unavailability, empty output, missing direct PDF images, and output
truncation likewise return a document with explicit `partial` reason codes.
Cheap standalone dimension and pixel checks run before copying/normalizing a
frame. Direct PDF image bytes are exposed by `pypdf` before C-05 can apply its
byte and pixel checks, so the implementation does not claim that every rejected
PDF image is refused before library-level materialization. Backend exceptions
and diagnostics never become invented source text.

## Dataset behavior

C-05 has no independent sampling algorithm. It executes only upstream B-07
`selected` records. References may promote a small dataset sample upstream, but
B-07 keeps a large dataset group limited at 32 files or 64 MiB. Consequently a
training-image collection cannot become a bulk visual-backend job merely
because C-05 sees deferred or limited queue entries.

## Dependency and validation

Pillow is a declared bounded runtime dependency. `pypdf` remains the required
PDF dependency. Tesseract is optional and discovered locally at runtime.

Focused tests cover deterministic architecture/result analysis, scanned-PDF OCR,
native PDF text preservation, follow-up-only rendering, unavailable/empty/
crashing backends, external-send denial, malformed and multi-frame images,
resource/output bounds, stable serialization, exact pixel reopening, source and
state mutation, selected-only execution, and no bulk processing of large
training-image groups.
