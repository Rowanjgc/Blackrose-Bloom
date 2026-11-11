# Scene direction and generation

Use illustration_draft for authoring, then image_gen with its validated draft_id
and exact revision. The host compiles the saved draft into the image prompt;
never duplicate the plan in image_gen arguments or prose during generation.

Describe one concrete scene in scene: the visible objects, their positions and
sizes, the factual relationships or metaphor, and the large areas left empty.
Describe Xiaohei's physical action in action: exact contact, lean, stance and
movement. Bind short exact labels to declared object IDs with optional positions.
Keep optional review_notes focused on possible misreadings and uncertainties.

The host adds white background, hand-drawn line style, sparse color, English
text constraints, and fixed single-bean character identity to every image.

For edits, author a draft describing the preserved scene and the specific change.
Generate from its ID/revision with reference_attachment_ids. Title removal should
preserve characters, labels, paths and line style; change only the title and its
local marks. Never add new lettering to repair removed text.

The returned saved path proves storage. It does not prove a successful visual
edit. Inspect the result and report any unreviewed criteria honestly.
