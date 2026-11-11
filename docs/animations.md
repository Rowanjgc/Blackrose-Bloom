# Explanatory animations

Blackrose exposes `animation_create` and `animation_read` for standard Motion
Canvas TSX. Import the [explanatory animation skill](../skills/explanatory-animations/SKILL.md)
for the authoring workflow and a relevant example.

Cloud models receive the authoring capability automatically. Local models carry
no animation tools or instructions by default; they author only when the user
selects the explanatory animation skill for that reply. Merely mentioning
"animation" does not load the contract or tools. Nothing switches providers
automatically. Previously saved animations remain available for local playback,
seeking, captions, and offline HTML export with any active model. Earlier v1 JSON
scenes retain their original interpreter.

Automatic playback-failure repair remains cloud-only because it is started by the
app rather than by a user request.

New scenes are type-checked, compiled, and rendered in a supervised WebKit worker
before saving. These checks establish execution, not explanation quality. Source
revisions create separate attachments and preserve previous versions.

See [Motion Canvas setup and checks](motion-canvas.md).
