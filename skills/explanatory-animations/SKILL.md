---
name: explanatory-animations
description: Create or revise playable educational animations using standard Motion Canvas.
---

Authoring tools are available when the user selects this skill. With a local model
they are loaded only for that reply; never suggest switching providers. Existing
animations can be played and exported with any active model.

When `animation_plan` is available, use it first for new animations, then implement
the accepted plan with `animation_create_planned`. Use direct authoring for revisions,
unsupported motion, or if planning fails. Otherwise use `animation_create` with a
standard Motion Canvas TSX module in `scene.source`. The tool
schema and built-in animation instructions describe the API. Blackrose compiles
and saves the animation, with inline playback and offline HTML export.

Make the motion explain the subject: show how actual objects change, connect,
rotate, bend, or travel. Use curves, paths, groups, and signals when helpful.
Captions should explain what the viewer sees. Choose the visual approach for the
request; a written storyboard is optional unless the user asks for one.
Use the app's Gothic Bloom palette: ink, light labels, restrained rose and gold.

Choose the objects, their spatial relationships, and the visible change before
writing source. For physical explanations draw the mechanism itself; labels and
stage boxes cannot stand in for needles, thread, belts, openings, or tube walls.
Use shared signals and reactive callbacks for connected motion. Keep the timeline
finite; the player handles repetition. Coordinate cues with actual source timing.
Compilation and playback checks do not establish that the explanation is correct.
The compiler rejects tween and timeline calls that are not yielded, and a motion
check warns when only the bottom caption band changes. For plan-first scenes, the
host also warns when a planned element never becomes visible or never changes a
property the plan moves. If a tool reports an error or warning, repair the complete
source and retry. After a successful
save, give a brief response so the user can play it. Do not require a separate
storyboard or approval step unless requested.

For revisions, read the saved source with `animation_read`, then submit the
complete revised scene with its actual `revises_attachment_id`. Earlier versions
remain available.

For linked rotation or mechanical motion, optionally read
[references/gears.tsx](references/gears.tsx). It demonstrates procedural teeth,
grouped shapes, and two gears driven by one signal. Read it only when relevant.
The JSON references for RAG and TCP are legacy v1 scenes retained for existing
players; new animations use Motion Canvas source.
