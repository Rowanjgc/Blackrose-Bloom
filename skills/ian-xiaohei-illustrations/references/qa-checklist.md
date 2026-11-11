# QA and Handoff in Blackrose

## What the evidence can establish

The tool's `width` and `height` establish aspect ratio. For requested 16:9
output, allow normal pixel rounding; flag a material mismatch. An edit that
intentionally preserves another source ratio should report that ratio.
`saved_path` establishes where the image was stored. Neither the path, prompt,
nor a successful API response establishes visual quality.

Only mark visual criteria as pass or fail when you can actually inspect the
image in your vision context. If the selected chat model lacks image input or
no image was supplied for inspection, mark those criteria **unverified**.
Do not claim to have opened a filesystem path; Blackrose has no such tool.

## Inspect the image

- Pure white background: no beige, paper grain, gradient, shadow, or noise.
- Xiaohei: solid black, white dot eyes, thin limbs, blank serious expression.
- Match the fixed bean/capsule proportions and eye placement; no separate head,
  neck, shoulders, mouth, or human torso. Check the planned stance and contact
  points rather than merely checking that a black figure appears.
- Xiaohei is recognizable and readable in size, not a tiny bystander, cat,
  furry mascot, outlined human, or colored figure.
- Xiaohei performs the core action; removing the creature breaks the metaphor.
- One clear idea with a coherent, strange physical metaphor.
- Compare visible relationships with the draft's facts and review
  notes. Connection direction and label attachment must preserve the concept;
  correct words at the wrong objects are a label failure. Check that the visual
  safeguard actually prevents the planned misreading.
- Spacious frame, roughly 40–60% subject and at least 35% empty white space.
- Brief, readable English labels; compare the entire visible text with the
  planned label list. Every requested label must be present and spelled
  correctly, with no invented words or pseudo-lettering. Normally plan 3–5
  labels; preserve the user's requested text and count.
- Every visible word is English. Chinese characters or other non-English
  lettering fail the label check; correct to the exact planned English labels.
- Color is restrained: orange main movement, red key problem/result, optional
  blue secondary state or note.
- No title or structure-type heading, especially in the top-left corner.
- No PPT, formal flowchart, course slide, cute mascot, polished commercial
  illustration, real UI, or complex architecture.
- Fresh composition consistent with the exclusions in the composition
  reference. Do not claim comparison against inaccessible example assets.
- Separate requested illustrations remain separate images.
- A detailed plan still yields a sparse drawing: no invented machinery,
  decorative objects, or extra detail beyond the planned scene.

## Corrections

Base a correction on a visible failure or the user's specific feedback.
Save a corrected draft, then use `image_gen` with its ID/revision and actual `reference_attachment_ids` for a focused edit;
author a new draft when a redesign is needed. There is no local
retouching or shell tool. Preserve the source ratio for a focused edit unless
reframing was requested.

For unreadable or invented text, correct the intended labels and remove only
the unrequested lettering; keep all requested text. For crowding, remove objects and
arrows. For decorative Xiaohei, give the character the action that explains
the idea. Preserve pure white, deadpan character design, and line restraint.

Make at most one corrective call per requested image unless the user requests
more revisions. If the result still has a visible issue, report it and mark
the image optional. Do not regenerate merely because visual QA is unavailable.
If a provider call fails, report the error and any images already saved; do not
enter a retry loop or claim that an unsuccessful call produced a file.

## Compact QA manifest

Provide a row for each final generated or edited result. Use actual returned
filenames or attachment IDs so corrections are distinguishable.

| Image | Aspect ratio | White background | Xiaohei core action | Labels | No title | Original metaphor | Actual saved path |
| --- | --- | --- | --- | --- | --- | --- | --- |

Fill cells with concise evidence: `pass, 16:9`, `fail, gray texture`,
`pass, 2 readable labels`, or `unverified`. Assess whether the metaphor repeats
a banned composition from the written specification; visual originality stays
unverified if the output cannot be inspected. Report source-preserving edits
as such rather than claiming that their composition is newly invented.

Use only absolute `saved_path` values returned by successful calls. For an
attached input that was never generated or edited, an attachment ID can
identify it, but it has no newly returned saved path to report.

Briefly identify each image's article placement and purpose. If visual review
supports it, name the most reliable and optional images. Otherwise say visual
review is pending. Images live in the app attachment store, not a workspace
export folder. If the user requested sequential article filenames, propose
them as manual-copy destinations without claiming a copy or rename occurred.
