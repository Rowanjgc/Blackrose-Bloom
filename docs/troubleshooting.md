# Troubleshooting and local diagnostics

### Concurrent workspace edits

Approved replacements require filesystem support for atomic exchange. The
displaced file is retained beside the replacement under a `.backup` name. If
its contents changed during publication, Bloom reports the published path and
the retained file so both can be reviewed. If exchange is unavailable, the
original is not overwritten. Other programs can still write to an open
displaced inode or overwrite the new file after publication; close or
coordinate other editors when a stable snapshot is required. An interruption
during publication can leave a staged or displaced `.backup` file; inspect it
before removing it.

Start with the visible request outcome and the relevant recent log. A model's
Ready status describes availability; a failed/stopped reply is a separate outcome.
The reasoning panel can contain saved reasoning after generation has ended.

Open **Reply details → Copy diagnostics** on a failed, stopped or interrupted
reply. The keyboard-accessible button copies the app/Python/platform versions,
reply state, and local conversation/message IDs when available. Failed admission
before a reply is saved offers the same action in **Request details**, including
after a page reload in that session. Successful replies have no diagnostics
button. A toast confirms copying or reports clipboard failure.

This shareable report deliberately omits prompts, answers, reasoning, error text,
model/endpoint settings, paths, credentials and logs. Error details remain visible
in the disclosure for local troubleshooting. The report identifies the local
record and runtime; it does not diagnose the cause or upload anything. Completion,
failure and stop announcements use a short outcome in the live region; detailed
errors can be read separately.

## Find recent errors

Application diagnostics are in `$XDG_STATE_HOME/blackrose-bloom/blackrose.log`;
router diagnostics are alongside them in `llama-router.log`. The default state
root is `~/.local/state`. Each stream retains the current file and `.1`–`.3`,
newest first, with **2 MiB per file (8 MiB per stream, 16 MiB together)**.
Records over 32 KiB keep their tail with an omission marker. Startup clips any
oversized legacy files to their recent tail before opening the writer. Layout
and lock files in that directory are separate from diagnostics.

A launched router writes stdout/stderr to a pipe, drained in fixed-size chunks
by a dedicated logging thread. The writer closes/reopens its own file on rotation;
the router never holds a renamed log descriptor. Output without newlines is
bounded too. Shutdown drains remaining output after the router exits. Chunk
boundaries can split long lines. An independently launched router retains its
launcher's output destination: adoption lets Bloom stop that process, but does
not redirect its existing stdout/stderr. Configure retention at that launcher.
After an application crash, restart Bloom to re-establish managed output; a child
whose output pipe lost its reader may exit. Do not use external rename-only
rotation on a live router from an older Bloom version; close the desktop/router
and start the updated application first.

`blackrose doctor` checks configured service/catalog/knowledge readiness without
inference. It contacts configured endpoints and prints paths/status. It is not a
live-model quality check. Verbose launch (`blackrose -v`) also writes diagnostics
to the terminal. Logs and doctor output can contain private paths, prompts/tool
errors or service details. To share diagnostics, explicitly copy only relevant
excerpts into a new file, inspect/redact credentials and private content, then
review that file before sending it. There is no automatic upload/export.

## Unavailable services

- **Local model/router:** inspect the configured endpoint, executable and preset
  paths, then `llama-router.log`. Fix a missing binary/preset or occupied endpoint,
  and retry. Keep the presets' inference settings under user control. Close an
  existing desktop normally if ownership is refused; do not delete lock files to
  bypass the active owner. "Requires an API key that Blackrose does not have"
  means a router at the endpoint rejected the key: stop a router whose
  `llama-router-*.key` was deleted, or set `router_api_key_file` for one you
  manage. Do not copy keys into logs or bug reports.
- **Cloud provider:** check the selected provider and its catalog error. Re-provision
  its desktop keyring entry using the [credential commands](user-guide.md#image-generation),
  unlock the keyring, and retry. Provider selection is explicit.
- **Knowledge:** inspect doctor output and the configured Retriever path. Missing
  embedding weights affect search; stored document reads remain available.
  Follow [Retriever setup](user-guide.md#knowledge-and-web-access), keeping its database
  and immutable captures together. Re-download required weights rather than
  deleting the index.
- **Web search/local image service:** verify the configured endpoint and service
  diagnostics. A local image service failure does not send a paid fallback request.
- **Local image model still loaded:** after an image, the model stays loaded for
  up to two minutes so the next image starts faster; the Models dialog shows the
  countdown and **Release now**. If a window crashed while the model was warm,
  the next image job, a Models dialog read, or Release now in any window releases
  it. Without a running window, `systemctl --user stop comfyui.service` (if
  Blackrose started it) or ComfyUI's own unload frees the memory.

## Missing runtime builds and cache cleanup

For a source checkout, follow [contributor setup](../CONTRIBUTING.md#set-up-a-checkout):
build both `animation-runtime` and `ui-runtime` with their locked dependencies.
Missing/corrupt semantic resources preserve ordinary chat/fallback but refuse
interactive effects. Rebuild UI resources with:

```sh
npm ci --prefix ui-runtime
npm run build --prefix ui-runtime
```

For an installed wheel, reinstall the matching release to restore packaged UI
resources. Run `blackrose setup-animations` to prepare its optional compiler.
The installed compiler cache is under
`$XDG_CACHE_HOME/blackrose-bloom/animation-runtime/<resource-hash>/` (default
`~/.cache`). If setup reports an incomplete cache, close Bloom and remove only
the exact compiler-cache directory named in that error, then rerun setup. In a
checkout, regenerate only the runtime's `node_modules` with `npm ci` and rebuild.
These operations need registry access. Model-weight caches need explicit
re-download and can be large; they are not a generic troubleshooting reset.

Keep databases, attachments, illustration drafts, recovery ledgers, preferences,
managed skills and external workspace outputs. They are durable data, inventoried
in [backup and recovery](recovery.md). Use [artifact inspection](artifact-cleanup.md)
for suspected orphans before considering its explicit repair command.

## Interrupted replies and recovery failures

Stop waits for pending database/storage work and GPU cleanup. A completed artifact
can be saved even when the reply ends stopped or fails during memory restoration.
Reloading retains the terminal outcome; a crash leaves pending replies/actions
marked interrupted/cancelled on startup. Use Retry or send a new prompt after
addressing the visible error; review attached files before repeating a request.
Edits still inside the interactive state's 250 ms debounce can be lost on an
abrupt process exit. A stale-view/state-sequence error calls for reopening the
conversation, not clearing its stored history.

A **latched local GPU memory recovery error** deliberately blocks later inference
and model loading. Read the recovery error and local-image/router diagnostics;
stop the failed local generation process normally, resolve remaining GPU users
or the service/memory problem, then close and restart Bloom. Confirm service/model
readiness before retrying. Repeated Stop requests cannot skip cleanup, and
resetting settings or deleting databases does not release GPU allocations.

Animation playback recovery has a separate persisted two-attempt limit. Inspect
the readable fallback/source and error; fix compiler/playback availability before
requesting a new animation. Keep prior artifacts and the recovery ledger. See
[Motion Canvas](motion-canvas.md) and [animation compatibility](animations.md).

### Output saved but conversation artifact pending

A document or approved edit can reach disk while its artifact snapshot or database
attachment fails. The tool then reports `artifact_pending`, the exact `path`, any
replacement `backup`, and a `recovery_id` when its recovery record was saved.
Keep these files; creating the output again can produce a second copy.

Pending records live in the attachment store's `output-recovery/` directory and
survive restart. Repeating the same document request in the original conversation
retries metadata only. Controller integrations can call
`await controller.recover_output(recovery_id)` for a single record, including
approved edits whose approval token has already been consumed. Open the original
conversation and restore its workspace first. Recovery checks current directory
authorization and the output digest; changed files require manual review. It never
rewrites or deletes the output or its backup. If the recovery record itself could
not be saved (`recovery_id` is null), locate the reported disk path directly;
automatic retry cannot guarantee avoiding another copy. This record covers metadata
failures after publication, not a process crash between publication and recording.
