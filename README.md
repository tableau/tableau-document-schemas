# Tableau Document Schemas

Official XML Schema Definition (XSD) files for Tableau document formats.

## Overview

This repository provides machine-readable schema definitions for Tableau file formats, enabling developers to:

- **Validate** Tableau workbooks (`.twb`) against the official schema
- **Build tools** that programmatically create or modify Tableau documents



### Directory Structure

```
schemas/
└── 2026_1/
    └── twb_2026.1.0.xsd    # Tableau Workbook schema for version 2026.1
```

Schemas are organized by Tableau version using the naming convention `YYYY_Q/` (year and quarter).

## Validation

The workbook schemas provide **syntactic validation** of Tableau workbook structure — they verify that elements, attributes, and their relationships conform to the expected XML format.

Use these schemas to catch structural issues early, but be aware that a syntactically valid workbook may still contain errors that only surface when opened in Tableau or published to Tableau Server/Cloud.

## Version Compatibility

Workbooks created with newer versions of Tableau may contain elements not present in older schemas. Always use the schema version that matches the Tableau version used to create or last save the workbook.

**Optional:** When creating or modifying a TWB and validating against version X of the XSD, you can set the `original-version` attribute to X and replace the manifest with the `ManifestByVersion` tag. This allows any version of Tableau >= X to load the workbook.

```xml
<workbook original-version='26.1' source-build='0.0.0 (0000.0.0.0)' source-platform='win' version='26.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <document-format-change-manifest>
    <ManifestByVersion />
  </document-format-change-manifest>
```

## Community

Have questions, feedback, or ideas? Join the conversation in [GitHub Discussions](https://github.com/tableau/tableau-document-schemas/discussions).

## Code of Conduct

This project adheres to the Salesforce [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

## License

This project is licensed under the terms specified in [LICENSE.txt](LICENSE.txt).

---

<p align="center">
  <sub>Maintained by <a href="https://www.tableau.com">Tableau</a>, a Salesforce company</sub>
</p>
