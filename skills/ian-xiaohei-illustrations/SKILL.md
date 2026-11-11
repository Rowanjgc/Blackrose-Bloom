---
name: ian-xiaohei-illustrations
description: Plan, generate, or edit Ian/Xiaohei illustrations for articles or standalone topics in Blackrose Bloom. Sparse hand-drawn scenes on white, a solid-black Xiaohei performing the core action, and exact English labels.
---

# Ian Xiaohei illustrations for Blackrose

Create separate 16:9 horizontal illustrations: pure white, thin wobbly black
pen lines, sparse red/orange/blue notes, generous empty space. Xiaohei is one
solid-black bean-shaped creature with two white dot eyes, thin limbs and a
blank expression. It performs the main action. Avoid childish cuteness,
commercial vector styling, slides, formal flowcharts, titles and clutter.
The app supplies the fixed character and style instructions to the image model.

Follow the user's count; default to three images when unspecified. For a
standalone topic no article is required. For a particular article, read that
article before planning. Preserve user-requested labels and edits.

## Draft, validate, generate

Use `illustration_draft` before `image_gen`. This is the available Blackrose
workflow even if an older reference shows an inline illustration_plan.
Do not submit a nested illustration_plan or a prompt to image_gen.

First call illustration_draft with `changes` containing:

- `core_idea`: one takeaway.
- `facts`: a nonempty list of factual relationships the scene must preserve.
- `scene`: the physical scene, metaphor and layout. Describe a simple picture precisely.
- `action`: what Xiaohei physically does and touches.
- `labels`: exact English text with target object IDs; use [] for no text.

Optional fields: `objects` (id, description, optional position), `connections`
(from, to, meaning, optional appearance), `placement`, and `review_notes`.
Labels use text, target, and optional position. Target a declared object ID or
reserved ID `xiaohei`. IDs are internal and must not appear on the drawing.
The host defaults placement to standalone and omitted objects/connections to [].
Do not invent facts or relationships merely to fill a field.

The tool saves a draft and returns draft_id, revision, valid, all blocking
issues, and review status. It does not start an image job. Repair all issues
in one call with the draft_id, current revision, and only changed fields in
changes. Fields replace whole values; arrays replace whole arrays. At most
two repairs are allowed per draft. Do not loop by creating replacement drafts.
Use the returned revision, not an assumed revision.

When valid=true, call image_gen with draft_id and revision only. For edits add
reference_attachment_ids, and describe the preservation/change in scene/action.
The host revalidates the exact saved revision, compiles the prompt, displays
the plan, then starts generation. aspect_ratio defaults to 16:9. Use the tool's
active provider; do not switch providers or retry execution failures unless asked.
Each image requires its own draft. Never combine independent images into a grid.

For plan-only requests, save the draft and present the plan without image_gen.
If the user requests approval before generation, stop after the plan.

## Reviews are separate from structural checks

valid=true means the draft's structure and references passed. It does not
prove the facts, metaphor, English language, or final image are correct.
Review the facts against the topic and available sources. Put uncertainties
and likely misreadings with safeguards in review_notes. A self-written check
is not independent verification. Never call a Latin-script check English QA.

After generation inspect the actual image when vision is available. Check
character/action, correct relationships, whitespace, clean white background,
exact readable labels, no title, and no old-case reuse. Returned dimensions
and saved paths verify file properties only. Report unavailable visual checks
as unreviewed; do not fabricate pass badges. Preserve saved images if a later
review or model continuation fails.

Report the actual returned saved path. Never claim a file exists before a
successful tool result. Blackrose saves to its attachment store automatically.
Use available file tools for a separately requested export.

## Load references selectively

The workflow and schema above are sufficient to author a draft. Do not load
all references before a simple standalone illustration. Use read_skill with
this skill ID and the relative path only when needed:

- references/planning.md: worked draft and repair example.
- references/composition-patterns.md: selecting a fresh visual metaphor and old-case exclusions.
- references/xiaohei-ip.md: character details if needed beyond the host's fixed design.
- references/style-dna.md: style clarification.
- references/prompt-template.md: translating scene decisions into concise direction.
- references/qa-checklist.md: post-generation inspection criteria.

Do not open assets/examples by default or copy their compositions. Invent the
scene from the topic. The action must explain the concept; removing Xiaohei
should remove an essential part of the action.
