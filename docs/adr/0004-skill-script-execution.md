# Managed skill script execution

Status: implemented for bra-6na. See [setup and execution contract](../skill-scripts.md).

## Context

Managed skills currently supply untrusted text. Their approval records bind only
SKILL.md, and resource discovery deliberately does not open resource bodies.
That approval cannot authorize executable resources. Reading, importing,
selecting, approving or enabling a skill must retain its existing passive behavior.

Scripts require a new host boundary. Argument validation, Python isolated mode,
environment filtering and process groups alone do not confine arbitrary Python.
Workspace and outbound grants cannot be inherited by a subprocess.

## Initial execution contract

Support Linux system CPython 3.12/3.13, standard library only, inside a fresh
Bubblewrap namespace per invocation. Use the system interpreter, not the app
virtual environment. No shell, pip, requirements installation, setup hooks,
native extensions supplied by the package, or alternate runtime selection.
The five starter skills remain instruction-only and require no runtime setup.

An optional `execution.json` declares a version and named entry points whose
paths are relative Python files inside the package. Reject unknown manifest
fields and unsupported versions. Absence of the manifest preserves existing
instruction-only behavior. The host exposes an explicit `run_skill_script`
tool accepting a skill ID, declared entry point and JSON input. Neither a file
path nor an executable name is a tool argument. Discover this tool only when an
enabled skill has a stored execution approval. Each invocation verifies its full
snapshot and rejects stale permission. No implicit invocation from
`read_skill` or skill selection.

Input is UTF-8 JSON on stdin, bounded to 64 KB. No host paths, open descriptors,
attachment stores, workspace directories or credentials are mounted. Callers
can supply text obtained through existing authorized tools. This first contract
offers no direct file input or network permission, even when the conversation
has workspace or web grants. Adding either requires another reviewed design.

The script writes UTF-8 JSON to stdout and diagnostics to stderr. Combined
output is capped at 64 KB while draining, not after `communicate()` has buffered
it. Invalid JSON, invalid UTF-8, excess output and nonzero exit are readable
failed outcomes. Script output remains untrusted tool-result content and never
becomes policy. Return skill/entry identity, outcome and bounded diagnostic text;
do not return host stage paths or environment details.

An optional result artifact is a bounded UTF-8 plain-text body with a suggested
basename, carried in the JSON result. Accept only one artifact, at most 32 KB,
with a host-selected plain-text media type. Ignore script-supplied media types
and reject path separators/control characters in the name. The controller
imports it through the existing document/artifact path under the originating
conversation; the script cannot select a destination or replace a file. Artifact
publication must settle before cancellation propagates, using existing durable
artifact cleanup rules. Binary and active HTML artifacts are outside this version.

## Review and immutable execution input

Keep instruction approval separate from execution approval. Existing SKILL.md
approvals continue to enable instruction reads and do not grant execution.
The execution review shows the manifest, every executable source file, the full
resource inventory, content hashes and the fixed host permissions/limits. Render
untrusted names and bodies as text. No model call can approve execution.

An execution digest covers a versioned canonical manifest and sorted relative
paths, byte lengths and SHA-256 digests for every regular package file, including
SKILL.md and resources. Thus dependency/resource edits, additions, removals and
renames invalidate execution approval. Reject symlinks, special files and trees
beyond the existing import bounds (1000 entries, 20 MB); do not truncate approval
inventories. Exclude no executable file by extension or hidden-name convention.

Use descriptor-relative no-follow traversal to read a bounded private snapshot.
Validate and hash the copied bytes, compare the snapshot digest to the saved
approval, and execute only that snapshot. Never validate live files and then
mount the live package: that leaves a review/execution substitution race. A
change during traversal either yields the exact approved snapshot or fails.
Repeat enabled-state and approval checks immediately before launch. Keep the
snapshot private and remove it on every terminal path. Approval is explicit in
the native skill-management flow and is never inferred from bundled origin.

## Confinement and supervision

Bubblewrap creates new user, mount, PID, IPC, UTS and network namespaces. Bind
only the reviewed snapshot read-only at `/skill` and a documented system Python
runtime/library allowlist read-only. Do not bind `/`, `/home`, `/run`, `/tmp`,
the app virtual environment or the host `/proc`. Mount no procfs; use minimal
private devices and a bounded private scratch mount. Clear
the environment, close inherited descriptors, drop capabilities, prevent
privilege gain and create a new session. Python runs with `-I -S -B`.

Apply a default-deny seccomp allowlist to prevent process creation/exec after interpreter
startup, socket creation, namespace changes, mount operations and tracing. Do
not rely on RLIMIT_NPROC, which is per user and is insufficient for process
confinement. Establish the filter before untrusted source runs. Keep the policy
specific to supported architectures and fail closed on unsupported platforms.
Bound address space to 256 MB, CPU to 5 seconds, wall time to 10 seconds, open
descriptors to 32, file size to 1 MB and core dumps to zero. Bound writable scratch
in memory independently of file-size limits. A small trusted launcher owns
limits and filter installation; avoid `preexec_fn` in the threaded desktop.

The controller owns supervision through the existing tool-host dispatch seam.
Hold the originating conversation identity across navigation. Stop, deadline,
output overflow, caller failure and shutdown terminate the namespace/process
group, await reaping and drain/close pipes before removing the snapshot. Use
Bubblewrap's die-with-parent behavior as crash protection, alongside explicit
supervision. Register active jobs with shutdown ownership. No detached jobs and
no successful tool result until all descendants are gone.

Missing system Python, Bubblewrap, supported seccomp support, namespace support
or resource-limit support returns a readable unavailable outcome. Probe actual
confinement, not just binary presence. Never retry with weaker flags or execute
on the host. Instruction-only skill features remain available.

## Verification before enabling the tool

Use temporary managed packages, scratch app storage and deterministic local
fixtures. Include a statistics script accepting a list of numbers and returning
count/min/max/mean plus a plain-text report. Verify normal execution and artifact
ownership; import/read/enable cannot run marker code. Test old approvals,
changed source/resources/manifest, traversal races and disabled skills.

Run real confined fixtures that attempt host-file reads/writes, secret environment
reads, outbound and loopback sockets, fork/exec, symlink substitution, scratch
exhaustion, infinite output and infinite computation. Assert denial and absence
of host effects. Verify timeout, Stop during startup/running/publication,
shutdown, descendant cleanup, bounded diagnostics and missing dependencies.
Mocks of the subprocess command do not establish confinement.

Extend native skill review probes for distinct execution approval, digest
invalidation and cancellation. Run tool-schema/handler contract tests, installed
wheel resource checks, the offline health gate, native probes and diff checks.
Document platform prerequisites and runtime availability in README/distribution
guides when the capability ships; keep current instructions accurate meanwhile.

## Evidence and reconsideration

On 2026-11-10, a Bubblewrap smoke test with fresh namespaces and read-only system
runtime mounts printed `1` outside the agent sandbox. The agent sandbox itself
denied a namespace networking operation. This establishes basic host namespace
availability only. The implementation adds real subprocess regression fixtures
for the syscall/filesystem boundaries and lifecycle, scripted-turn report and
navigation tests, and a native review/permission probe. Commands and scope are
documented in the execution guide; a namespace skip is never a security pass.

Reconsider the runtime if standard-library Python is insufficient or portable
execution becomes necessary. Evaluate a WASI runtime as a separate capability;
do not silently expand host mounts or install package dependencies to satisfy a
script. Execution support must remain optional.
