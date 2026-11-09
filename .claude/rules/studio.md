---
paths:
  - "studio*.py"
  - "studio_ui/**"
  - "tests/test_studio*.py"
---
# History studio

- One way out: every export, share page and agent tool reads `studio_export.build_view()`. Never serialize from the database directly; `tests/test_studio_export.py` iterates `EXPORTERS`, so a new format is covered only if registered there.
- Redactions apply to every profile including `owner`. Do not add an owner bypass.
- Deletion must reach free text: `delete_item` / `purge_history` scrub names from story, captions, notes and trip titles, and delete the item's edit log.
- Studio tables never get rewritten by ingest (ingest drops `places`/`visits` only). Item ids are stable hashes of source identity so `recapture` keeps edits.
- Grant tokens: only the sha256 is stored; the token is read from `TIMELINE_GRANT` in the server's env, never passed by the agent.
- Leak tests match coordinates as whole numbers (38.714 must not match inside 38.7148).
- UI: no inline scripts or styles in the studio page (CSP `'self'`); share pages are exported HTML with their own no-script CSP.
- Browser check: `node tools/studio_ui_check.js <url> <outdir>` (uses the environment's Playwright).
