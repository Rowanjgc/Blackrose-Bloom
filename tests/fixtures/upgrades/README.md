# Historical chat compatibility fixtures

These synthetic fixtures freeze chat schemas 1–4. Schemas 1–3 were reviewed at
`7040328` on 2026-10-17; schema 4 preserves the artifact-cleanup journal before
the citation-audit migration. Apply `chat-1.sql` through the desired version in order. They are
independent of the production migration list so changing that list cannot silently
change the old database under test. `test_saved_compatibility.py` populates the
historical columns directly, then opens the database with the actual current
`Database` and `Repository` interfaces. No real user data or external paths are read.

`saved-form.json` freezes protocol 1, `blackrose-semantic/1` and the schema hash
from the generated contract at this checkpoint. It includes immutable source,
its hash and readable fallback. The fixture's saved state and completed action
are inserted separately, as they were stored in schema 3. Keep these historical
identities when adding a new format; explicitly decide which existing formats
remain supported and extend the matrix instead of regenerating all fixtures.

Run `.venv/bin/python -m unittest discover -s tests -p test_saved_compatibility.py -v`.
The matrix covers populated versions 1–4 to the current schema, repeated opens and
failure/retry at each pending migration, including failure after earlier steps
have committed. Raw historical column values, IDs, timestamps, attachment paths,
evidence, workspace grants, reference pointers, saved UI state and completed event
identity survive. Integrity and foreign keys are checked. Version 1 has no
workspace/reference/output fields; their migrated defaults are checked explicitly.

Saved-answer tests check supported state replay, refused state/actions for unknown
protocol/library/schema identities, missing/corrupt installed contracts and bundles,
readable fallback/source access, and legacy JSON animation replay after upgrading.
The browser probe in `scripts/check_interactive_ui.cjs` checks actual rendering,
including unfamiliar document shapes and failed bundle loads. Existing
`tests/animation_player.cjs` exercises the legacy animation player and controls.

These are offline compatibility checks. They do not establish native WebKit
rendering, live provider behavior, production recovery time or release contents.
Keep the separate archive/installed-wheel checks and synthetic restore drill.
