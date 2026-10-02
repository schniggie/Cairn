# Integrated research workbench

Served at /research by the original Cairn FastAPI app. Assets live under /static/research/; Alpine is the existing shared vendor file. No build tool, iframe, sample fixtures, model credentials, or target requests in this page.

API: /api/research/sessions; readiness: /api/research/runtime. The page polls saved state, preserves project URLs, and distinguishes queued from running. Creating a session persists authorization but **does not execute research** until a reviewed runtime is implemented.

The standalone interactive design fixtures remain under prototypes/research-workbench/. Do not copy fixture findings into production.

See docs/development-charter.md and docs/implementation-status.md for scope and verified limits.

Reports are created with POST /api/research/sessions/{id}/reports. The page freezes the returned projection and Markdown, and stores a stable report link in the URL fragment. Existing GET report.md remains a live compatibility export; the new UI downloads the immutable snapshot. No report snapshot or evidence fixture is seeded into production.

Identity management imports JSON from a fixed Kali private directory. The browser sends labels and basenames only; account data never enters the page. See docs/identity-materials.md for formats, origin binding, replacement/revocation and key recovery. This feature does not log into targets or integrate an executor.

Source material UI is composed through sources.js while retaining Alpine getter descriptors. It captures immutable local text snapshots, browses saved content and preserves source line references. See docs/source-materials.md for scope and limits. It does not execute or audit source code.

comparison.js adds read-only saved source comparisons and navigation from code evidence back to the referenced snapshot. Changes do not imply a completed audit or successful fix; no source is executed.

reports.js adds cursor-paginated report history within the report dialog. Opening a report is read-only; creation requires the explicit generate action. List and content requests use independent sequence guards within the dialog lifecycle. See docs/report-versions.md.
