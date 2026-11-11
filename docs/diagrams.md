# Mermaid diagrams in chat

Blackrose renders explicitly closed `mermaid` code fences in assistant answers,
including saved conversations. Source stays visible while the model writes a
diagram. Completed diagrams render when visible; malformed or oversized diagrams
retain readable, copyable source. Stop does not auto-close unfinished diagrams.

Each diagram offers Expand, Source and Copy source. The larger viewer uses the
normal modal keyboard controls: Escape closes and restores focus. Wide diagrams
scroll inside the answer. Export, editing, and pan/zoom are not included.

The short model hint encourages compact diagrams for relationships, flows,
sequences and states, with prose for simple answers. It adds 297 UTF-8 bytes
(token counts depend on the provider) and no tools. Successful diagrams add no
model requests; syntax failures can trigger one small source-only repair.
Original Markdown alone enters model history; rendered images do not. Citation
rigor audits surrounding prose, not claims inside code fences or diagram images.

## Local renderer and limits

Build with `npm ci --prefix diagram-runtime` and `npm run build --prefix diagram-runtime`.
The full Mermaid 12.3.0 runtime includes mindmap and architecture diagrams, with
all code bundled locally. Custom icon packs and external resources are disabled.
The roughly 5.4 MB host is loaded only on the first visible, completed diagram.
An editable checkout without that build continues to show source.

One opaque-origin sandbox frame serializes rendering, with strict Mermaid
security, HTML labels disabled, no native bridge and no network access. Diagram
configuration directives and YAML frontmatter are rejected. Results become passive
SVG images; generated SVG never enters the main page DOM. Links, external image
elements and active SVG elements are removed. Colors follow Gothic Bloom.

Limits: 20,000 source characters, 200 edges, 2 MiB serialized SVG, and 32 cached
results or 8 MiB, whichever comes first. Cache keys use source within the current
renderer version and fixed theme. Timeouts latch a failed renderer until reload;
they are recovery deadlines, not hard CPU isolation guarantees. Removed views
discard results and queued work. No persistent data migration is needed.

## Automatic syntax repair

When Mermaid reports a syntax error in a completed assistant answer, Blackrose
waits for the current generation to finish, then asks the active model to fix
only that diagram. The request contains the failed source and bounded parser
diagnostic, without tools, conversation history, memory lookup or an extra skill
contract. It reuses the tool-free source-repair stream used by interactive answers
and the deferred repair scheduling used by animations. Successful source is
displayed in place. Your draft, attachments and surrounding prose are preserved.

The original response remains in the saved message. Corrected Markdown lives in
a revision of its existing message-block storage and is used for display and
future model history. Claims outside the diagram retain their original audit
positions. Attempts commit before inference, so reload or duplicate failure
reports cannot spend again. No chat schema upgrade is needed.

`diagram_repairs = 1` is the default maximum number of diagrams repaired per
answer (0 disables, maximum 2). Each diagram gets one attempt, even if the
replacement is still invalid. The source remains available after failure or
Stop. Stopped/interrupted attempts are not automatically restarted. Resource
limits, missing runtime assets, renderer timeouts, image failures and prohibited
configuration do not call the model. Stop and shutdown cancel queued or active
repairs; navigation cannot patch a different chat. A repair request has a
60-second deadline, an 80,000-byte output cap and a 128,000-byte reasoning cap,
each also bounded by the configured application limits.

## Verification

`tests/test_diagrams.py` checks fences, escaping, prompt admission and durable
source replay with one provider request. `tests/test_diagram_repairs.py` checks
source preservation, budgets, Stop, duplicates, reopening and conversation scope.
`node scripts/check_diagrams.cjs` uses
real Python Markdown and Chromium to check seven diagram types, lifecycle,
controls, isolation, failures and offline behavior. It needs the project Python
environment and the usual browser prerequisites. `scripts/check_gui_diagrams.py`
checks the same diagram types in native GTK/WebKit with scratch data and scripted
providers; it opens a visible window because WebKit suspends visibility callbacks
in hidden windows. CI runs it under Xvfb through the GUI health suite.

For local native checks, CI's `GDK_BACKEND=x11` and
`WEBKIT_DISABLE_DMABUF_RENDERER=1` settings also produced a clean final run.
Without them, direct reruns passed assertions and exited zero but emitted a
harmless GPU allocator diagnostic.

Local spot check on 2026-11-03 with `gemma-4-26b-a4b-qat`: all three flow,
sequence and state requests produced Mermaid; arithmetic, greeting and short
Python-code requests did not. This six-request sample used the compact host
guidance without tools and is not a cross-model reliability measurement.

A source-only repair spot check with local `qwen36-35b-a3b` on the same date
corrected a synthetic reproduction of the reported literal `\n` between graph
statements with one request. Scripted native checks also verify the real failure
bridge and in-place replacement. This is not a guarantee that every invalid
diagram can be repaired.
