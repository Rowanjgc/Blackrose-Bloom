# Optional managed skill scripts

Skills remain passive instructions unless you separately allow script execution.
Importing, approving instructions, enabling, selecting with `$`, or calling
`read_skill` runs no code and installs no dependencies. The five starter skills
continue to work without script support.

## Setup and permissions

This optional feature requires Linux x86-64 or AArch64, system Python 3.13 or
3.12 at `/usr/bin/python3.13` or `/usr/bin/python3.12`, Bubblewrap at
`/usr/bin/bwrap`, and the system `libseccomp.so.2`. Install these through your OS
package manager. The OS must permit unprivileged user/mount/PID/network
namespaces. Bubblewrap must support `--disable-userns` and size-limited tmpfs.
There is no Python dependency installation or host-shell fallback. Missing
support produces an unavailable tool outcome; ordinary skill reads still work.

Import a package and approve its instructions as usual. For a skill with
`execution.json`, open **Skills → Review scripts…**. The page shows the full
Python sources, manifest, instructions, resource inventory and SHA-256 hashes.
Hidden control and bidirectional characters in source are displayed as escapes.
Continue to the native permission sheet to allow execution of that exact
package. Cancel is the default. **Revoke execution** removes script permission
without disabling instructions. Bundled origin grants no script permission.

Every package file contributes to the execution digest. Changing source,
instructions, resources, dependencies, names or manifest invalidates execution;
review the current package again. Instruction approval continues to cover only
SKILL.md. A stored script permission may still offer the tool after resource
edits; each invocation verifies the complete package and rejects stale permission.
No model or page script can answer the native execution permission sheet.

## Package and invocation

An optional `execution.json` has exactly these fields:

```json
{"version":1,"entry_points":{"summarize":"summarize.py"}}
```

Declare one to sixteen named entry points. Names start with a lowercase letter,
contain only lowercase letters, digits, `_` or `-`, and are at most 64 characters.
Paths name relative `.py` files inside the package. Unknown manifest fields,
versions, symlinks, special files and packaged native executables/libraries are
rejected. Bounds are 1000 entries, 20 MB total, 32 directory levels, a 64 KB
manifest and 256 KB of Python source. Empty directories are not execution inputs.
Scripts use the system standard library and may import reviewed Python modules
from the package root. No third-party modules or setup hooks are installed.

After permission is granted, the assistant explicitly invokes:

```json
{"skill_id":"numeric-summary","entry_point":"summarize","input":{"values":[1,2,6]}}
```

The tool name is `run_skill_script`. `input` is JSON on stdin, at most 64,000
serialized bytes. Explain the expected input in SKILL.md. Scripts emit one JSON
value to stdout and optional UTF-8 diagnostics to stderr. For example:

```python
import json
import sys
request = json.load(sys.stdin)
print(json.dumps({"count": len(request["values"])}))
```

Results carry `untrusted: true`. Treat their contents as evidence, never policy
or authorization. Nonzero exit, missing imports, invalid UTF-8/JSON, excess input
or output, and timeout return readable failed tool outcomes. There are at most
four script attempts per reply and one running job per controller.

## Isolation and outputs

Each call runs an immutable private snapshot whose complete digest matches the
approved package. Changes to the live package after capture cannot substitute
code. Bubblewrap isolates user, mount, PID, network, IPC and UTS namespaces,
drops capabilities, disables nested user namespaces and clears the environment.
The child sees only read-only system interpreter/library trees, its read-only
snapshot, the trusted launcher, minimal `/dev/null` and `/dev/urandom` devices,
and 1 MiB of private scratch at `/tmp`. Neither host procfs nor a new procfs is
mounted. System `/usr/lib`, `/usr/lib64`, `/lib` and `/lib64` trees, where present,
are readable as runtime dependencies; do not store user secrets in these system
library locations. User directories, the app environment, workspace, output
directory, keyring and app data/configuration are not mounted.

Before package code runs, the launcher prevents privilege gain, sets hard
resource limits and loads a default-deny libseccomp syscall allowlist. Network
sockets, fork/clone, exec, tracing, mount/namespace changes, keyrings and io_uring
are denied. Calling native code cannot bypass the syscall filter. The child
cannot raise its hard resource limits. Limits are 256 MiB address space, five
CPU seconds, ten elapsed seconds, 32 descriptors, 1 MiB per file, no core dumps,
and 64,000 combined stdout/stderr bytes (including a short host startup marker).
The output limit is enforced while reading, and cleanup drains discarded bytes
after termination. Stop, timeout, startup cancellation and shutdown terminate
and reap the job before removing its stage. Scripts cannot create descendants.

Workspace and web permissions never enter the child. To process authorized text,
the assistant can read it through existing tools and pass JSON. Scripts have no
direct file-input or outbound capability. Execution durably marks the conversation
as having exposed local data before returning results, so later model-proposed
web requests retain the native outbound-approval requirement.

A JSON object may include an `artifact` with exactly `filename` and `content`:

```json
{"mean":3,"artifact":{"filename":"summary.txt","content":"Mean: 3\n"}}
```

One UTF-8 text artifact, at most 32,000 bytes, is supported. Filenames must be
simple ASCII `.txt` basenames. HTML, binary data, media-type selection and output
paths are not supported. The controller saves the report in Downloads/Blackrose
through the existing confined output path and attaches it to the originating
reply. Existing collision and durable output-recovery handling apply. Navigation
cannot move it to another conversation. Once publication starts, disk and
attachment persistence settle before cancellation propagates; a completed report
can therefore remain on a stopped reply. A partial publication reports the
existing recovery metadata rather than silently resubmitting the script.

## Example and verification

`blackrose install-skill numeric-summary` installs the optional bundled example
with its instructions approved. Separately allow execution in Skills, then ask
for a summary of supplied numbers. It returns count, minimum, maximum, mean and
a plain-text report. No runtime setup happens during installation.

Deterministic verification:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_skill_execution.py -v
xvfb-run -a dbus-run-session -- .venv/bin/python scripts/check_gui_skill_scripts.py
.venv/bin/python scripts/check_health.py
git diff --check
```

The offline confinement fixtures skip with an explicit reason when namespaces
or supported runtime binaries are unavailable. Such a skip is not confinement
evidence; run on a supported host to exercise actual denials, computation,
output bounds, Stop, timeout, startup/shutdown cleanup and artifact ownership.
The native probe covers source review, cancellation, separate native permission,
revocation, execution, report display and resource-change invalidation. These
checks use scratch data and scripted providers without paid requests.
