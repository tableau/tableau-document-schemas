#!/usr/bin/env python3
"""
validate_workbook.py — Standalone structural validator for Tableau workbooks.

Validates a Tableau workbook's XML against the public Tableau workbook (TWB)
XSD schemas bundled in this repository under `schemas/<YYYY_R>/`.

Pipeline, per workbook:

    1. Read the workbook XML (unwrapping a .twbx archive if needed).
    2. Sniff the workbook's version from the raw bytes, before choosing a
       schema. The root `<workbook version='...'>` attribute is frozen at a
       legacy value ("18.1") by real-world Tableau Server/Online exports and
       does not track the workbook's actual format, so it's used only as a
       last resort. Preferred, in order:
         a. the `source-build` attribute on the root `<workbook>` tag (e.g.
            `source-build='2025.1.0 (...)'` -> version "25.1"), when its
            leading dotted version has a plausible (>= 2000) year;
         b. the `<!-- build YYYYR.YY.MMDD.HHMM -->` comment near the top of
            the file, decoded the same way;
         c. the root `<workbook version='...'>` attribute, taken verbatim.
    3. Select the matching XSD from `schemas/`: an exact version match if one
       is bundled; if the workbook's version is newer than anything bundled,
       fall back to the newest schema and warn; if it's older than the oldest
       bundled schema, reject it (no schema exists to validate against).
    4. Validate structure with libxml2 (via lxml), collecting line/element-
       tagged errors.

IMPORTANT — scope. XSD validation is *structural* only. It does NOT reproduce
Tableau's *semantic* validation (field/datasource resolution, calculated
field parsing, cross-references between sheets, connection attributes). The
schemas explicitly mark those regions `processContents="skip"`. A green
result here means "structurally well-formed against the schema", not
"guaranteed to open in Tableau". See README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

try:
    from lxml import etree
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "error: this tool requires lxml. Install it with:\n"
        "    pip install lxml\n"
    )
    sys.exit(2)


# Bundled schemas live at <repo_root>/schemas/<YYYY_R>/twb_<YYYY.R.0>.xsd.
SCHEMAS_DIRNAME = "schemas"

# The public TWB schemas declare `<xs:import namespace=".../user"/>` with NO
# schemaLocation: the `user` namespace is a deliberate extension point that
# the schemas never fully define (its only concrete reference is the
# `user:UserAttributes-AG` attribute group and the `user:localizable`
# annotation element). Tableau's internal XML parser tolerates the unresolved
# import; libxml2 (lxml) is stricter and refuses to build the whole schema.
# To stay faithful to the intended "open extension" semantics — rather than
# deleting the reference — we supply a permissive stub schema for that
# namespace.
USER_NS = "http://www.tableausoftware.com/xml/user"
_USER_NS_STUB_LOCATION = "urn:tableau:user-namespace-stub"
_USER_NS_STUB_XSD = f"""<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="{USER_NS}"
           elementFormDefault="qualified">
  <!-- Open extension point: accept any attributes Tableau attaches here. -->
  <xs:attributeGroup name="UserAttributes-AG">
    <xs:anyAttribute namespace="##any" processContents="lax"/>
  </xs:attributeGroup>
  <!-- `localizable` only appears inside xs:annotation/appinfo metadata, which
       libxml2 ignores, but declare it so the namespace is self-consistent. -->
  <xs:element name="localizable">
    <xs:complexType>
      <xs:anyAttribute namespace="##any" processContents="lax"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

# The schemas also `<xs:import>` the standard XML namespace (again with no
# schemaLocation) to reference `xml:base` / `xml:lang` / etc. libxml2 does not
# auto-supply these, so we serve the canonical W3C declarations for them. This
# is the standard xml.xsd content, trimmed to the attributes TWB references.
XML_NS = "http://www.w3.org/XML/1998/namespace"
_XML_NS_STUB_LOCATION = "urn:tableau:xml-namespace-stub"
_XML_NS_STUB_XSD = f"""<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="{XML_NS}"
           xmlns:xml="{XML_NS}">
  <xs:attribute name="base" type="xs:anyURI"/>
  <xs:attribute name="lang">
    <xs:simpleType>
      <xs:union memberTypes="xs:language">
        <xs:simpleType>
          <xs:restriction base="xs:string">
            <xs:enumeration value=""/>
          </xs:restriction>
        </xs:simpleType>
      </xs:union>
    </xs:simpleType>
  </xs:attribute>
  <xs:attribute name="space">
    <xs:simpleType>
      <xs:restriction base="xs:NCName">
        <xs:enumeration value="default"/>
        <xs:enumeration value="preserve"/>
      </xs:restriction>
    </xs:simpleType>
  </xs:attribute>
  <xs:attribute name="id" type="xs:ID"/>
</xs:schema>
"""

# Matches the `version` attribute inside the root <workbook ...> tag, allowing
# either single or double quotes. We scan the raw tag text (not a full parse)
# so we can pick a schema before validating. Quoted attribute values are
# matched as opaque units so a literal '>' inside one (legal, unescaped XML)
# doesn't end the match before the real tag close.
_WORKBOOK_TAG_RE = re.compile(
    rb"""<workbook\b(?:"[^"]*"|'[^']*'|[^>])*""", re.IGNORECASE | re.DOTALL
)
# Match the `version` attribute exactly — not `original-version`, `source-build`,
# etc. `\b` treats the hyphen in `original-version` as a word boundary, so guard
# with a lookbehind rejecting a preceding hyphen or word char.
_VERSION_ATTR_RE = re.compile(rb"""(?<![-\w])version\s*=\s*(['"])(.*?)\1""", re.IGNORECASE)
# The `source-build` attribute, e.g. source-build='2025.1.0 (20251.25.0313.2002)'.
# Its leading dotted version (before any parenthesized internal build number)
# is the product release the workbook was actually authored/saved with — unlike
# `version`, which real-world exports freeze at a legacy value.
_SOURCE_BUILD_ATTR_RE = re.compile(
    rb"""source-build\s*=\s*(['"])\s*([\d.]+)""", re.IGNORECASE
)
# The `<!-- build YYYYR.YY.MMDD.HHMM -->` comment near the top of the file.
# YYYYR is the product year+release concatenated (e.g. "20262" -> 2026.2); the
# following YY is redundant (last 2 digits of the year). This is a fallback,
# distinct from `source-build`, so it only fires when `source-build` is absent
# or clearly a placeholder (e.g. "0.0.0").
_BUILD_COMMENT_RE = re.compile(rb"<!--\s*build\s+(\d{5})\.(\d{2})\.")

# Known schema-vs-real-world gaps to suppress by default, confirmed against
# workbooks pulled from a live Tableau site:
#
#   - `_.fcp.*` attributes/elements: Tableau's internal Feature Capability
#     Property markers (e.g. `_.fcp.WorkbookFingerprinting.true...author-id`).
#     Absent from every bundled schema version (2018.1-2026.2); not something
#     a workbook author can control.
#   - missing `explain-data`: the bundled schemas require `explain-data`
#     unconditionally inside `Workbook-ExplainData-G` (no `minOccurs="0"`
#     anywhere in the chain), but real workbooks that never touched Explain
#     Data omit the element entirely. Confirmed as a schema bug, not a
#     workbook defect (see schemas/2025_2/twb_2025.2.0.xsd:7511).
#   - `accelerator-details` "not expected" errors: a cascade of the same
#     `explain-data` defect above, not an independent bug.
#     `Workbook-AcceleratorDetails-G` IS already `minOccurs="0"` at its
#     reference point (schemas/2025_2/twb_2025.2.0.xsd:7513), so this isn't a
#     missing-optionality problem. But libxml2's sequence walker is a single
#     forward cursor: it stalls on the mandatory-but-absent `explain-data`
#     slot and never advances past it, so the next real element it sees
#     (`accelerator-details`) gets reported as "not expected" against
#     whatever slots remain ahead of the stalled `explain-data` position —
#     confirmed by every observed message listing `explain-data` as one of
#     the "expected" options.
_IGNORED_ISSUE_PATTERNS = (
    re.compile(r"'_\.fcp\."),
    re.compile(r"^Element 'workbook': Missing child element\(s\)\. Expected is .*\bexplain-data\b.*\.$"),
    re.compile(r"^Element 'data-orientation': This element is not expected\. Expected is \( explain-data \)\.$"),
    re.compile(r"^Element 'accelerator-details': This element is not expected\. Expected is .*\bexplain-data\b.*\.$"),
)


def _is_ignored_issue(message: str) -> bool:
    return any(p.search(message) for p in _IGNORED_ISSUE_PATTERNS)


# Schema-vs-real-world gaps that are downgraded to warnings rather than
# dropped: every bundled schema (2018.1-2026.2) declares `source-build` as a
# required attribute on the root <workbook> element, but real-world exports
# don't always carry it. Version sniffing (`sniff_version()`) already falls
# back past a missing `source-build` to the build comment or `version`
# attribute, so this doesn't block validation from running — it's surfaced
# as a warning rather than silently ignored, since a missing `source-build`
# is unusual enough to be worth flagging.
#
#   - missing `simple-id`: `worksheet` and `dashboard` elements end their
#     sequence with a required `simple-id` (a UUID Tableau's own authoring
#     tools always stamp on save, used for cross-references elsewhere in the
#     schema). Observed only in hand-built/tooling-generated files (e.g.
#     `PublishToolTest`), not organically authored workbooks — treated as
#     worth flagging but not blocking, since it doesn't reflect a schema bug.
_WARNING_ISSUE_PATTERNS = (
    re.compile(r"^Element 'workbook': The attribute 'source-build' is required but missing\.$"),
    re.compile(r"^Element '(worksheet|dashboard|window)': Missing child element\(s\)\. Expected is .*\bsimple-id\b.*\.$"),
)


def _is_warning_issue(message: str) -> bool:
    return any(p.search(message) for p in _WARNING_ISSUE_PATTERNS)


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #
@dataclass
class Issue:
    """A single validation problem, tagged with location where available."""

    message: str
    line: Optional[int] = None
    level: str = "error"  # "error" | "fatal" | "warning"

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "line": self.line,
            "message": self.message,
        }

    def format(self) -> str:
        loc = f"[line {self.line}] " if self.line else ""
        return f"{self.level.upper()}: {loc}{self.message}"


@dataclass
class Result:
    """Outcome of validating one workbook."""

    source: str
    is_valid: bool = False
    version: Optional[str] = None
    schema: Optional[str] = None
    twb_entry: Optional[str] = None  # name of the .twb inside a .twbx, if any
    warnings: list[Issue] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {
            "name": os.path.splitext(os.path.basename(self.source))[0],
            "validation_timestamp": _utc_now_iso(),
            "isValid": self.is_valid,
        }
        all_issues = self.issues + self.warnings
        if all_issues:
            out["issues"] = [i.to_dict() for i in all_issues]
        return out


class ValidationError(Exception):
    """A hard failure that prevents validation from running at all."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Reading the workbook XML (.twb directly, or .twb inside a .twbx)
# --------------------------------------------------------------------------- #
def read_workbook_xml(path: str) -> tuple[bytes, Optional[str]]:
    """
    Return (xml_bytes, twb_entry_name).

    For a .twbx (a ZIP package) we read ONLY the top-level .twb entry out of the
    archive. Bundled data extracts (.hyper), images, and other resources are
    never decompressed or loaded into memory — zipfile seeks straight to the
    .twb entry — so peak memory stays proportional to the .twb XML regardless of
    how large the package is.
    """
    if not os.path.exists(path):
        raise ValidationError(f"file not found: {path}")

    if zipfile.is_zipfile(path):
        return _read_twb_from_archive(path)

    # Treat anything else as a raw .twb XML file.
    with open(path, "rb") as fh:
        return fh.read(), None


def _read_twb_from_archive(path: str) -> tuple[bytes, Optional[str]]:
    with zipfile.ZipFile(path) as zf:
        # Tableau writes exactly one .twb at the archive root. Pick root-level
        # entries (no path separator) ending in .twb; ignore anything nested.
        candidates = [
            name
            for name in zf.namelist()
            if name.lower().endswith(".twb") and "/" not in name.strip("/")
        ]
        if not candidates:
            # Fall back to any .twb anywhere, just in case of an unusual layout.
            candidates = [n for n in zf.namelist() if n.lower().endswith(".twb")]
        if not candidates:
            raise ValidationError(
                f"no .twb entry found inside archive: {os.path.basename(path)}"
            )

        entry = candidates[0]
        # Read ONLY this entry; the .hyper extract and other files are skipped.
        return zf.read(entry), entry


# --------------------------------------------------------------------------- #
# Version sniffing & parsing
# --------------------------------------------------------------------------- #
def _year_release_to_version_str(year: int, release: int) -> str:
    """Convert a product (year, release) pair to the "YY.R" form used by both
    the workbook's `version` attribute and this repo's `schemas/YYYY_R/`
    naming (e.g. year=2025, release=1 -> "25.1")."""
    return f"{year % 100}.{release}"


def _source_build_version(tag: bytes) -> Optional[str]:
    """Derive a "YY.R" version from the root tag's `source-build` attribute,
    e.g. source-build='2025.1.0 (...)' -> "25.1". Returns None if the
    attribute is absent or its leading version doesn't look like a real
    product release (placeholder builds use "0.0.0")."""
    match = _SOURCE_BUILD_ATTR_RE.search(tag)
    if not match:
        return None
    parts = match.group(2).decode("ascii", "replace").split(".")
    if len(parts) < 2:
        return None
    try:
        year, release = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if year < 2000:
        return None
    return _year_release_to_version_str(year, release)


def _build_comment_version(xml_bytes: bytes) -> Optional[str]:
    """Derive a "YY.R" version from the `<!-- build YYYYR.YY.MMDD.HHMM -->`
    comment near the top of the file, e.g. "20262.26.0804.1806" -> "26.2"
    (year 2026, release 2). Returns None if no such comment is present or it
    doesn't decode to a plausible year."""
    match = _BUILD_COMMENT_RE.search(xml_bytes)
    if not match:
        return None
    yyyyr = match.group(1).decode("ascii")
    try:
        year, release = int(yyyyr[:-1]), int(yyyyr[-1])
    except ValueError:
        return None
    if year < 2000:
        return None
    return _year_release_to_version_str(year, release)


def sniff_version(xml_bytes: bytes) -> Optional[str]:
    """
    Determine the workbook's version without a full parse. The root
    `<workbook version='...'>` attribute is frozen at a legacy value by
    real-world Tableau Server/Online exports and does not track the
    workbook's actual format, so it's used only as a last resort. Preferred,
    in order: the `source-build` attribute, then the `<!-- build ... -->`
    comment, then the `version` attribute. Returns None if none are present.
    """
    tag_match = _WORKBOOK_TAG_RE.search(xml_bytes)
    tag = tag_match.group(0) if tag_match else b""

    version = _source_build_version(tag)
    if version is not None:
        return version

    version = _build_comment_version(xml_bytes)
    if version is not None:
        return version

    attr_match = _VERSION_ATTR_RE.search(tag)
    if not attr_match:
        return None
    return attr_match.group(2).decode("ascii", "replace").strip()


def parse_version(version: str) -> Optional[tuple[int, ...]]:
    """
    Parse a dotted version string ("26.1", "26.2") into a comparable tuple of
    ints. Returns None if it doesn't look numeric-dotted.
    """
    parts = version.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple, tuple]:
    """Right-pad the shorter tuple with zeros so comparisons align by position."""
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


# --------------------------------------------------------------------------- #
# Schema discovery & selection
# --------------------------------------------------------------------------- #
@dataclass
class Schema:
    version: tuple[int, ...]  # e.g. (26, 2) — the TWB version the XSD targets
    version_str: str          # e.g. "26.2"
    path: str


def discover_schemas(schemas_dir: str) -> list[Schema]:
    """
    Scan the bundled schemas directory for `YYYY_R/twb_YYYY.R.0.xsd` files and
    return them keyed by their TWB version string. The product version 2026.N
    corresponds to TWB version string "26.N" (per the repo README), so
    schemas/2026_2/... registers as version (26, 2).
    """
    schemas: list[Schema] = []
    if not os.path.isdir(schemas_dir):
        return schemas

    for entry in sorted(os.listdir(schemas_dir)):
        subdir = os.path.join(schemas_dir, entry)
        if not os.path.isdir(subdir):
            continue
        # Directory name like "2026_2" -> product (2026, 2) -> TWB (26, 2).
        m = re.match(r"^(\d{4})_(\d+)$", entry)
        if not m:
            continue
        year, release = int(m.group(1)), int(m.group(2))
        twb_version = (year % 100, release)  # 2026 -> 26
        version_str = f"{year % 100}.{release}"

        xsds = [f for f in os.listdir(subdir) if f.lower().endswith(".xsd")]
        if not xsds:
            continue
        schemas.append(
            Schema(
                version=twb_version,
                version_str=version_str,
                path=os.path.join(subdir, sorted(xsds)[0]),
            )
        )
    return schemas


def select_schema(
    version_str: Optional[str], schemas: list[Schema]
) -> tuple[Schema, list[str]]:
    """
    Choose the XSD to validate against, returning (schema, warnings).

    Precedence:
      1. Exact version match — no warning.
      2. Workbook version newer than the newest bundled schema — fall back to
         the newest bundled schema and warn about potential drift.
      3. Workbook version older than the oldest bundled schema — no schema
         exists to validate against; ValidationError.
      4. Missing/unparseable version — ValidationError.
    """
    if not schemas:
        raise ValidationError(
            f"no XSD schemas found; expected them under '{SCHEMAS_DIRNAME}/'"
        )

    if not version_str:
        raise ValidationError(
            "could not determine the workbook's version: no `version` attribute "
            "found on the root <workbook> element"
        )

    parsed = parse_version(version_str)
    if parsed is None:
        raise ValidationError(
            f"unrecognized workbook version string: {version_str!r}"
        )

    warnings: list[str] = []
    ordered = sorted(schemas, key=lambda s: s.version)

    # 1. Exact match.
    for s in ordered:
        pv, sv = _pad(parsed, s.version)
        if pv == sv:
            return s, warnings

    oldest, newest = ordered[0], ordered[-1]
    pv_oldest, sv_oldest = _pad(parsed, oldest.version)
    pv_newest, sv_newest = _pad(parsed, newest.version)

    # 2. Newer than the newest bundled schema — fall back and warn.
    if pv_newest > sv_newest:
        warnings.append(
            f"workbook version {version_str} is newer than the newest bundled "
            f"schema ({newest.version_str}, {os.path.basename(newest.path)}); "
            f"falling back to it. Structural drift between the workbook's true "
            f"format and this schema may produce spurious errors or miss real ones."
        )
        return newest, warnings

    # 3. Older than the oldest bundled schema — no schema to validate against.
    if pv_oldest < sv_oldest:
        available = ", ".join(s.version_str for s in ordered)
        raise ValidationError(
            f"workbook version {version_str} predates the oldest bundled schema "
            f"({oldest.version_str}); no XSD is available for it "
            f"(available versions: {available})"
        )

    # Unreachable: every version is either an exact match, above the newest, or
    # below the oldest, or between two bundled versions with no exact match —
    # which is also unsupported.
    available = ", ".join(s.version_str for s in ordered)
    raise ValidationError(
        f"workbook version {version_str} has no exact matching XSD "
        f"(available versions: {available})"
    )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
# Namespaces the public TWB schemas import without a schemaLocation, mapped to
# the in-memory stub we serve for each.
_STUB_SCHEMAS = {
    USER_NS: (_USER_NS_STUB_LOCATION, _USER_NS_STUB_XSD),
    XML_NS: (_XML_NS_STUB_LOCATION, _XML_NS_STUB_XSD),
}
_STUB_BY_LOCATION = {loc: xsd for (loc, xsd) in _STUB_SCHEMAS.values()}


class _StubResolver(etree.Resolver):
    """Serves the in-memory stub schemas to libxml2 when the TWB schema
    imports them via their urn: stub locations."""

    def resolve(self, url, id, context):
        stub = _STUB_BY_LOCATION.get(url)
        if stub is not None:
            return self.resolve_string(stub, context)
        return None  # defer to lxml's default resolution for everything else


def _schema_parser() -> "etree.XMLParser":
    parser = etree.XMLParser()
    parser.resolvers.add(_StubResolver())
    return parser


def load_schema(schema_path: str) -> "etree.XMLSchema":
    """Load and compile a TWB XSD from a file path, giving each
    schemaLocation-less `<xs:import>` (the `user` extension namespace and the
    standard XML namespace) a location pointing at our stubs so libxml2 can
    build the grammar."""
    schema_doc = etree.parse(schema_path, _schema_parser())
    xs = "http://www.w3.org/2001/XMLSchema"
    for imp in schema_doc.getroot().findall(f"{{{xs}}}import"):
        stub = _STUB_SCHEMAS.get(imp.get("namespace"))
        if stub is not None and not imp.get("schemaLocation"):
            imp.set("schemaLocation", stub[0])
    return etree.XMLSchema(schema_doc)


def _check_utf8(xml_bytes: bytes) -> Optional[Issue]:
    """
    Byte-exact UTF-8 check, run before parsing. Returns an Issue if the bytes
    aren't valid UTF-8.
    """
    try:
        xml_bytes.decode("utf-8")
        return None
    except UnicodeDecodeError as ex:
        # Approximate a line number by counting newlines up to the bad byte.
        line = xml_bytes.count(b"\n", 0, ex.start) + 1
        return Issue(
            message=(
                f"file is not valid UTF-8 at byte {ex.start}: {ex.reason}"
            ),
            line=line,
            level="fatal",
        )


def validate_xml(
    xml_bytes: bytes, schema_path: str, ignore_known_gaps: bool = True
) -> tuple[list[Issue], list[Issue]]:
    """
    Validate the workbook XML against the given XSD. Returns
    (issues, warnings) — an empty `issues` list means structurally valid.
    Raises ValidationError for problems that stop validation from happening
    (bad schema, unparseable XML).

    When `ignore_known_gaps` is true (the default):
      - issues matching `_IGNORED_ISSUE_PATTERNS` (confirmed schema-vs-real-
        world gaps that don't affect validity) are dropped entirely;
      - issues matching `_WARNING_ISSUE_PATTERNS` (confirmed gaps worth
        flagging, but that don't block validation from running) are moved
        into the returned `warnings` list instead of `issues`.
    """
    # 1. UTF-8 sanity.
    utf8_issue = _check_utf8(xml_bytes)
    if utf8_issue:
        return [utf8_issue], []

    # 2. Compile the schema.
    try:
        schema = load_schema(schema_path)
    except (etree.XMLSyntaxError, etree.XMLSchemaParseError) as ex:
        raise ValidationError(f"failed to load XSD schema {schema_path}: {ex}")

    # 3. Parse the workbook XML. A parse failure here is a well-formedness
    #    error — report it as a fatal issue rather than a hard crash.
    try:
        doc = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as ex:
        line = getattr(ex, "lineno", None) or None
        return [
            Issue(
                message=f"XML is not well-formed: {ex.msg if hasattr(ex, 'msg') else ex}",
                line=line,
                level="fatal",
            )
        ], []

    # 4. Structural validation.
    if schema.validate(doc):
        return [], []

    issues: list[Issue] = []
    warnings: list[Issue] = []
    for err in schema.error_log:
        if ignore_known_gaps and _is_ignored_issue(err.message):
            continue
        if ignore_known_gaps and _is_warning_issue(err.message):
            warnings.append(
                Issue(message=err.message, line=err.line or None, level="warning")
            )
            continue
        issues.append(
            Issue(
                message=err.message,
                line=err.line or None,
                level="fatal" if err.level_name == "FATAL" else "error",
            )
        )
    return issues, warnings


def validate_workbook(
    path: str, schemas_dir: str, ignore_known_gaps: bool = True
) -> Result:
    """Top-level: read, sniff, select schema, validate. Never raises for a
    merely-invalid workbook — only for setup problems (handled by caller)."""
    result = Result(source=path)

    xml_bytes, twb_entry = read_workbook_xml(path)
    result.twb_entry = twb_entry

    result.version = sniff_version(xml_bytes)

    schemas = discover_schemas(schemas_dir)
    schema, warnings = select_schema(result.version, schemas)
    result.schema = schema.path
    result.warnings.extend(Issue(message=w, level="warning") for w in warnings)

    issues, xml_warnings = validate_xml(xml_bytes, schema.path, ignore_known_gaps)
    result.issues = issues
    result.warnings.extend(xml_warnings)
    result.is_valid = not any(i.level in ("error", "fatal") for i in result.issues)
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _default_schemas_dir() -> str:
    # schemas/ lives at the repo root; this script lives in workbook_validation/.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, SCHEMAS_DIRNAME)


def _print_human(result: Result) -> None:
    header = f"{result.source}"
    if result.twb_entry:
        header += f"  (.twb entry: {result.twb_entry})"
    print(header)
    print(f"  version: {result.version or '<unknown>'}")
    if result.schema:
        print(f"  schema:  {os.path.basename(result.schema)}")
    for w in result.warnings:
        print(f"  {w.format()}")
    if result.is_valid:
        print("  RESULT:  VALID (structurally conforms to the schema)")
    else:
        n = len(result.issues)
        print(f"  RESULT:  INVALID ({n} issue{'s' if n != 1 else ''})")
        for issue in result.issues:
            print(f"    - {issue.format()}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a Tableau workbook (.twb or .twbx) against the "
            "Tableau workbook XSD schemas bundled in this repository. "
            "Structural validation only — see README.md for the "
            "structural-vs-semantic caveat."
        )
    )
    parser.add_argument(
        "workbooks", nargs="+", metavar="WORKBOOK",
        help="one or more .twb / .twbx files to validate",
    )
    parser.add_argument(
        "--schemas-dir", default=_default_schemas_dir(),
        help="directory containing the XSD schemas, organized as "
             "<schemas-dir>/<YYYY_R>/twb_<YYYY.R.0>.xsd "
             "(default: the repo's top-level schemas/ directory)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="emit machine-readable JSON instead of human-readable text",
    )
    parser.add_argument(
        "--no-ignore-known-gaps", dest="ignore_known_gaps", action="store_false",
        help="report all schema issues as blocking errors, including confirmed "
             "schema-vs-real-world gaps that are otherwise suppressed or "
             "downgraded by default: `_.fcp.*` feature-capability "
             "attributes/elements and the `explain-data`/`accelerator-details` "
             "gap (normally dropped entirely), and the missing `source-build` "
             "attribute and missing `simple-id` gap (normally downgraded to a "
             "non-blocking warning). See README.md for details.",
    )
    args = parser.parse_args(argv)

    results: list[dict] = []
    any_invalid = False
    any_error = False

    for path in args.workbooks:
        try:
            result = validate_workbook(path, args.schemas_dir, args.ignore_known_gaps)
        except ValidationError as ex:
            any_error = True
            error_result = Result(
                source=path, issues=[Issue(message=str(ex), level="fatal")]
            )
            if args.json:
                results.append(error_result.to_dict())
            else:
                print(f"{path}")
                print(f"  ERROR: {ex}")
            continue

        if not result.is_valid:
            any_invalid = True
        if args.json:
            results.append(result.to_dict())
        else:
            _print_human(result)

    if args.json:
        print(json.dumps(results, indent=2))

    # Exit codes: 0 = all valid, 1 = at least one structurally invalid,
    # 2 = a setup/IO error (missing file, unsupported version, unreadable
    # schema) prevented validation from running for at least one input. The
    # JSON body (if requested) is always printed regardless of exit code, so
    # callers can inspect `isValid`/`issues` for full detail.
    #
    # With multiple inputs, `2` takes priority over `1`: if one input hits a
    # setup error and a different input is merely invalid, the exit code is
    # `2`, not `1` — check each result's `isValid` individually rather than
    # relying on the exit code to distinguish "invalid" from "errored" across
    # a batch. This ambiguity doesn't arise for the single-workbook case.
    if any_error:
        return 2
    return 1 if any_invalid else 0


if __name__ == "__main__":
    sys.exit(main())
