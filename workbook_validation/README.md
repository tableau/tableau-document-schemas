# Tableau Workbook Validator

A standalone, cross-platform (macOS / Windows / Linux) script that validates a
Tableau workbook's XML against the Tableau workbook (TWB) XSD schemas bundled
in this repository under [`/schemas`](../schemas).

It sniffs the workbook's version, picks the matching schema, and validates
structure with a mature C XSD engine (libxml2, via `lxml`) — collecting
line-tagged errors.

## What it does — and does not — check

**Structural (syntactic) validation only.** A `VALID` result means the XML
conforms to the schema's element/attribute structure. It does **not** mean the
workbook will open in Tableau.

This is the same boundary the schemas draw. They deliberately skip (marked
`processContents="skip"` in the XSD):

- connection element attributes,
- calculated-field contents,
- cross-references such as tab/sheet names and field references.

Tableau's REST API validation endpoints go further and perform semantic
validation (see the [root README](../README.md#validating-a-twb)). Treat a
green result here as "structurally well-formed against the schema," not
"guaranteed to load."

## How version → schema selection works

1. The `version` attribute is read from the raw root `<workbook>` tag (before
   any full parse).
2. Schema is chosen by that version, in this precedence order:
   - **Exact version match** → used directly (e.g. workbook version `26.2`
     matches `schemas/2026_2/twb_2026.2.0.xsd`).
   - **Workbook version newer than the newest bundled schema** → falls back to
     the newest bundled schema and prints a warning about potential drift.
   - **Workbook version older than the oldest bundled schema** → rejected;
     no schema exists in this repository to validate against.

## Supported inputs

- **`.twb`** — raw workbook XML, validated directly.
- **`.twbx`** — a ZIP package. The tool reads **only** the top-level `.twb`
  entry out of the archive; bundled `.hyper` data extracts, images, and other
  resources are never decompressed or loaded, so memory use stays proportional
  to the `.twb` XML regardless of package size. (Note: the repository's
  bundled XSDs themselves are only published/supported for validating `.twb`
  content — see the [root README](../README.md#no-twbx-support). This script
  extends that to `.twbx` purely as a convenience for unwrapping the archive
  before validating the inner `.twb`.)

## Install

Requires Python 3.9+ and `lxml` (a prebuilt wheel wrapping libxml2 — installs
cleanly on macOS and Windows with no compiler).

```bash
python3 -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows (PowerShell):
#   .venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

## Usage

```bash
# Validate one or more workbooks (human-readable output)
python validate_workbook.py "World Indicators.twbx" some_workbook.twb

# Machine-readable JSON
python validate_workbook.py --json "World Indicators.twbx"

# Point at a different schemas directory
python validate_workbook.py --schemas-dir /path/to/schemas my.twbx
```

### Exit codes

| Code | Meaning |
|-----:|---------|
| `0`  | All inputs structurally valid |
| `1`  | At least one input was structurally invalid |
| `2`  | A setup/IO error (missing file, unsupported version, unreadable schema) prevented validation from running for at least one input |

When validating a single workbook — the primary intended use, e.g. from an
agent or plugin — the exit code alone tells you the outcome; no output
parsing is required to know whether it passed. `--json` output is always
printed regardless of exit code, so callers that want the *why* (which
elements, which lines) can parse `issues` after checking the exit code.

With multiple inputs, `2` takes priority over `1`: if any input hits a setup
error, the exit code is `2` even if a *different* input was merely invalid
(not errored). A `1` or `2` only means "at least one problem occurred
somewhere in the batch" — inspect each result's `isValid`/`issues`
individually to see which input(s) failed and how.

### JSON output shape

`--json` emits an array with one object per workbook:

```json
{
  "name": "Superstore",
  "validation_timestamp": "2026-08-18T01:21:04.008060+00:00",
  "isValid": true,
  "issues": [
    { "level": "error", "line": 10, "message": "..." }
  ]
}
```

`issues` is omitted entirely when there are none. Each entry has a `level`:

| Level     | Meaning | Affects `isValid`? |
|-----------|---------|---------------------|
| `fatal`   | The file itself is broken — not valid UTF-8, or the XML doesn't parse (not well-formed). Also used for the rare libxml2 validation error it classifies as fatal-severity internally. | Yes |
| `error`   | The XML parses fine but doesn't conform to the schema's structure — wrong element order, missing required children, unexpected elements, etc. This is the vast majority of real issues. | Yes |
| `warning` | A confirmed schema-vs-real-world gap that doesn't indicate a genuine workbook defect (see below) — reported for visibility but does not block validity. | No |

`fatal` and `error` both mean "structurally invalid" — the split is informational
(document unreadable vs. document readable-but-noncompliant), not a difference
in severity for the purposes of `isValid`.

#### Suppressed and downgraded issues

Some schema/real-world mismatches are known gaps, not genuine workbook
defects. By default (`--no-ignore-known-gaps` to disable):

- **Dropped entirely** — the schema wrongly requires something that real
  workbooks correctly omit:
  - `_.fcp.*` attributes/elements (Tableau's internal Feature Capability
    Property markers — not user-controllable, absent from all bundled schemas).
  - Missing `explain-data` (and its `accelerator-details`/`data-orientation`
    cascade) — the schema never marks `explain-data` optional, but most real
    workbooks never populate it.
- **Downgraded to a `warning`** — the gap is real but worth surfacing rather
  than hiding:
  - Missing `source-build` attribute on the root `<workbook>` element —
    schemas mark it required, but not all real exports carry it. Version
    detection already tolerates its absence (see below), so this doesn't
    block validation.
  - Missing `simple-id` on `worksheet`/`dashboard`/`window` — Tableau's own
    authoring tools always stamp this UUID on save; its absence has only
    been observed in hand-built/tooling-generated test files, not organically
    authored workbooks.

## Implementation notes

- **UTF-8 check.** The file is byte-checked for valid UTF-8 before parsing.
- **Namespace stubs.** The schemas `<xs:import>` the Tableau `user` extension
  namespace and the standard `xml` namespace *without* a `schemaLocation`.
  Tableau's own parser tolerates this; libxml2 is stricter. To stay faithful
  to the intended "open extension" semantics (rather than deleting the
  references), the tool injects permissive in-memory stub schemas for those
  namespaces at load time.
