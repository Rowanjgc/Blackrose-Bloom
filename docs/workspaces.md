# Conversation workspaces and artifacts

Choose **Workspace → Choose directory** in the sidebar to authorize a local
working directory for the current conversation. Its name stays visible in the
sidebar and its full path appears in the workspace browser. Browse subdirectories,
preview text/documents, or insert a file/directory reference with **Reference**.
**Clear workspace** removes the grant. Workspace selection persists per chat in
SQLite and does not change the selected model or require another mode.

Launch with `blackrose .` to use the caller's current directory, or
`blackrose ~/Dev/` to use an explicit directory. `blackrose` (and the desktop
launcher) uses the [output folder](#where-new-files-go) `Downloads/Blackrose`,
creating it if necessary. The launch directory becomes the current chat's
workspace and the starting workspace for new chats in that desktop session.
Opening another existing chat retains that chat's saved workspace.

To access another directory, mention its literal path in a chat message, such as
`Please inspect ~/Documents` or `Compare "~/Project notes" with ~/Dev/`.
Blackrose asks for permission before sending the message. Approval grants reads
within those directories for the current conversation; new files and replacements
there still need their own review. Declining keeps the draft
and sends nothing. These extra grants remain in memory until app exit, conversation
deletion, or workspace selection/clearing. They never come from model output,
file contents, history replay, or web/tool results. Only existing directories
qualify; naming a file does not grant access to its parent. Use absolute paths
or `~/` paths; quote paths containing spaces. The prompt supplies approved paths
in canonical absolute form, including resolved targets for directory aliases.

Type `@` in the composer to search accessible files and directories, or `$` to
search enabled installed skills. Continue typing to filter by fuzzy name/path;
use Up/Down and Enter/Tab, or click a suggestion. Escape dismisses the menu.
Several references can appear in one message. Selected local references display
as `@{/absolute/path}`; skills display as `$skill-id`. Selection carries structured
metadata separately from that readable text. Removing the visible token removes
its invocation from the submitted request. Typing an arbitrary path or skill
name alone does not create a structured reference.

File references carry an absolute path, file/directory kind, and device/inode
identity. The backend rechecks identity and current authorization before accepting
the request. They do not attach or paste file contents. Uploaded attachments
continue to work as before and remain distinct from local references.

Explicit skills resolve through `SkillManager.read` before inference. Only their
`SKILL.md` instructions and existing resource index are supplied; other resource
bodies still require `read_skill`. Selected instructions are appended to the
request as a delimited user-selected block, prominent for that request but ranked
below application policy, while ordinary automatic discovery remains available.
Skill bodies stay transient; the chat stores the selected IDs. A missing, disabled
or unreviewed selected skill fails admission and leaves the draft recoverable.

## File tools and destinations

The existing tool execution loop exposes:

| Tool | Behavior |
| --- | --- |
| `file_list` | Paginated directory listing |
| `file_search` | Bounded recursive fuzzy name/path/extension search |
| `file_info` | File/directory metadata |
| `file_read` | Paginated text, including extracted PDF and DOCX |
| `directory_create` | Create directories in the output folder |
| `document_create` | Save a document or script; outside the output folder, propose it for review |
| `file_edit` | Propose replacement text for user review |

No indexing is required. RAG still searches the shared semantic knowledge index;
file tools inspect actual filesystem objects. Tool calls can be combined with
knowledge, web, image, animation, and skill tools through the same bounded
`TurnRunner` continuation loop. Tool errors return structured failures for the
model to recover from. The reply displays compact activity and consulted file
paths; generated outputs become attachment cards.

### Where new files go

Model output is untrusted, and a new file can be as dangerous as an edit: a
`conftest.py`, a CI workflow or an editor task runs the next time another tool
opens the project. Blackrose therefore creates files without asking in one place
only, the app-owned **output folder** `Downloads/Blackrose`. It is created on
first use with mode 0700. Nothing else loads or runs files from it.

Everywhere else (the workspace, directories approved in chat, and Downloads
itself), `document_create` returns a pending review card and writes nothing, not
even parent folders. The card shows the full destination and the exact text; a
PDF is reviewed as the Markdown it is rendered from. **Approve new file** writes
exactly those bytes once, creating missing folders on the path. Approval is
refused if a file has appeared at that path since the proposal, or if the path
now leads through a symlink. The token is single-use and expires on rejection,
workspace change, conversation deletion or restart, like edit approvals. At most
eight reviews can be pending. `directory_create` works only inside the output
folder; elsewhere a folder is created by approving a file inside it.

The backend enforces this in `WorkspaceFiles.write` and `mkdir`. A write that is
not marked reviewed is refused unless its destination is the output folder, so a
prompt or tool result cannot unlock it. Granting a directory in chat grants reads
only.

Relative output names use the active workspace, or the output folder when there is
no workspace. The model is told to save new artifacts in the output folder with an
absolute path unless you ask for another location. Launching plain `blackrose`
selects the output folder as the workspace, so relative names are created there
without review and the default chat reads only that folder, not the rest of
Downloads. Older chats keep their saved workspace, such as Downloads, until you
change it. Linux Downloads comes from `XDG_CONFIG_HOME/user-dirs.dirs`
(`XDG_DOWNLOAD_DIR`, including `$HOME`), or `~/Downloads`. That configuration is
parsed as data and never sourced as shell code. Other destinations require
selecting their directory as the workspace or approving a directory mentioned in
chat.

`document_create` supports Markdown, text, HTML, JSON, YAML, CSV, XML, TOML, SQL,
reStructuredText, CSS, Python, shell, JavaScript, and TypeScript. JSON output is
validated. For `.pdf`, the tool supplies Markdown and the application renders real
PDF bytes with headings, lists, paragraphs, and tables using
[ReportLab Platypus](https://docs.reportlab.com/reportlab/userguide/ch5_platypus/).
Rendering runs in a worker thread and uses no shell, HTML execution, remote assets,
or application-controlled conversion subprocess. Scripts are saved without
executable permissions and are never run.

New-file collisions become `name (1).ext`, `name (2).ext`, etc. Files are actually
saved at the returned destination. The existing content-addressed attachment store
also retains a snapshot for conversation replay, so later external modifications
or deletion do not erase the chat preview. Deleting a conversation removes its
managed snapshots through normal attachment cleanup; it does not delete output
files in the workspace/Downloads or its [edit backups](#edit-backups).

Cards show filename, MIME type, size, destination, **Preview**, **Open externally…**
where appropriate, and **Show folder…**. Markdown renders through the existing safe
renderer; scripts and structured text use source formatting. HTML previews use an
iframe with an empty sandbox and restrictive content security policy, without
scripts, application bridge access, forms, or remote assets. HTML cards are marked
as active web pages. PDF cards provide a text preview; **Open externally…** shows
the actual PDF with full page layout.
Open/reveal checks current authorization again. Preview reads the saved snapshot.
Existing image/animation players remain intact and gain **Show folder…**; their
original attachment-store destinations and animation export behavior are retained.

Nothing leaves Blackrose without a native confirmation. It shows the absolute path
and file type, or the folder, and the desktop's default application for it, which
is the application that launches. Cancel is the default. Opening HTML outside the
preview runs its scripts and lets it reach the network, so it gets a warning dialog.
Scripts and other structured text are never handed to another application.

### Edit backups

An approved edit never leaves a copy in your project, where ignore rules written
for `.env` or `secrets.yml` would not cover it. The replacement is published by
atomically exchanging the file with a hidden, private
`.blackrose-stage-<id>-edit` entry in the same folder; the displaced original then
moves to `~/.local/share/blackrose-bloom/edit-backups/<conversation>/`
(`XDG_DATA_HOME`), whose folders are mode 0700. It keeps its permission bits. On
another filesystem it is copied, synced, and only then removed from the project.
File tools cannot read the store, even when a workspace contains it.

If the store cannot take it, the original stays in the project as a visible
`name.backup` (numbered if one exists) and a notice gives its exact path. If the
file changed while the edit was being approved, the approved text is still
published, the approval expires, and the notice names both the file and the
retained version.

Each backup is recorded in the chat database with its original path, digest,
time, conversation and approval. The record is written before publication, so
a backup interrupted by a crash is moved to the store at next start. Records and
files survive restart and conversation deletion.

**Workspace → Edit backups** lists the current conversation's backups.
**Preview** shows the saved text; **Restore** asks for confirmation, then puts
back the exact original bytes. Restore is refused unless the backup still
matches its recorded digest, the current workspace grants the file, and the file
still holds the approved replacement; the version it replaces becomes a backup in
turn. **Remove** deletes one backup (a copy kept in the project is only
forgotten). **Remove backups older than 30 days** applies across all chats,
including deleted ones, but never removes a version that changed during approval
or a copy kept in the project. Nothing is removed automatically.

## Permissions and limits

- Within the selected workspace: regular-file reads and metadata are allowed; new
  files and edits are reviewed, except new files when the workspace is the output
  folder itself. Traversal outside the grant is refused.
- Without a workspace: new outputs in the output folder are allowed. Reads/mentions
  cover only outputs already attached to this conversation, not unrelated Downloads
  files.
- Symlinks are refused during operations, including directory ancestors. Selecting
  a symlink with the native picker authorizes its resolved target explicitly.
  Implicit Downloads operations require a real directory path; configure its real
  location or select that target as a workspace if your Downloads is a symlink.
  If the output folder is a symlink or is reached through one, plain `blackrose`
  starts without a workspace and logs why instead of following it.
- The persisted workspace grant records its device/inode identity. Replacing the
  root requires selecting it again. Switching or clearing a workspace removes its
  access; earlier references cannot grant access back. Authorized Downloads outputs
  remain accessible independently.
- Existing files are never overwritten by `document_create`; proposing a file that
  already exists is refused with a pointer to `file_edit`. `file_edit` returns
  a pending review card. Its review shows a bounded line comparison with context:
  removed/added lines carry a visible sign, spoken label and color, and a summary
  states line counts and line-ending, final-newline or byte-order-mark changes.
  Long lines are excerpted around their first difference; display limits state how
  many changed lines are omitted. The complete existing and proposed text remain
  available as disclosures. Approving after
  the reply finishes checks the original digest and atomically replaces the file;
  the previous version moves to the [edit backup store](#edit-backups), outside
  the project. Changed files fail approval. Reject leaves the
  file untouched. Pending proposals survive page reloads within the desktop session
  but expire on application restart or workspace change.
- No delete or shell-execution tool is exposed. Unrelated paths require a native
  directory selection or an explicit directory-access confirmation. Extra grants
  retain the same root-identity, traversal, and symlink checks as workspaces.

Listings return 50 entries/page; search scans at most 2,000 entries and reports
truncation. Composer queries debounce for 180 ms and reuse a five-second bounded
workspace index. Workspace results precede otherwise accessible recent references
and Downloads outputs. Search inside a narrower directory if the scan is truncated.
At most 20 selected references are accepted per message.

Reads use existing document admission/extraction limits (10 MiB input, 200 PDF
pages, 40,000 extracted characters) and return at most 12,000 characters per call,
with continuation/truncation metadata. File contents are untrusted evidence.
Source/proposal text is limited to 256 KB UTF-8; rendered outputs are bounded too.
The existing prompt admission and tool-round limits still apply to every round.
Once an output write begins, cancellation waits for disk and attachment persistence
and retains the completed output in the interrupted reply.

PDF typography is intentionally basic. Bundled Vera provides Latin-focused
coverage; additional scripts/CJK, embedded images, sophisticated pagination, and
HTML-to-PDF conversion are not supported. HTML preview blocks supporting remote or
local file assets. There is one workspace grant per conversation; separate read-only
location grants, deletion, shell execution, and automatic image/animation copies to
workspace destinations are deferred. Native open/reveal launches the desktop's
GIO default application for the file type or folder after confirmation;
`$BROWSER` is not consulted.

## Verification

`tests/test_workspace.py` exercises real temporary files, boundary violations,
symlinks, root identity, Downloads permissions, collisions, extraction/PDF validity,
reference persistence, explicit/automatic skill disclosure, recoverable tool failures,
approval/backup, and cancellation settlement. `tests/test_edit_backups.py` covers
the store move (including a scratch Git repository's `.env`), cross-filesystem
copy, store failure, crash recovery, restore guards, persistence and retention. Existing chat, upload, RAG, web,
image, animation, storage, and provider regression suites remain enabled.
`tests/test_directory_access.py` checks CLI defaults and paths, confirmation and
denial, follow-up access, conversation isolation, revocation, identity changes,
and symlink/traversal confinement for additional grants.

Run `.venv/bin/python scripts/check_health.py --gui` to include six GTK/WebKit
checks. `check_gui_workspace.py` uses scratch XDG directories and a scripted provider
to exercise directory selection, keyboard/mouse autocomplete, multiple references,
a real page reload, saved files, safe previews, and edit approval without live model
or paid calls. Live model quality and the desktop's external viewer/file manager
are separate operational integrations.
