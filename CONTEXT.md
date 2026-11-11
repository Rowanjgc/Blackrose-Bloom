# Blackrose Bloom domain glossary

- **Conversation:** a durable ordered chat, with its title and attachment references.
- **Turn:** one accepted user request and its associated assistant reply. The
  request and pending reply are created together; the reply becomes complete,
  cancelled or failed. A saved turn owns its identity through cancellation.
- **Draft:** one conversation's unsent composer text, references and validated
  attachments, owned by the desktop session and saved in the chat database. It
  remains recoverable until the request is durably stored.
- **Evidence:** untrusted document, knowledge or web content supplied to a model.
  Stored source attribution records what was consulted; it does not archive
  transient fetched pages or skill bodies.
- **Workspace:** a conversation's optional authorized local directory, persisted
  with its device/inode identity. Local references are pointers checked against
  current authorization; they do not attach or index file contents.
- **Artifact:** an image, validated animation, or generated document saved as an attachment.
  Document artifacts also exist at their workspace/Downloads destination. Completed
  artifacts survive later continuation, cancellation and recovery failures.
- **Prompt:** the assembled request, including usable history, accepted evidence,
  instructions, clock, tool definitions and continuation results. Admission checks
  its complete content before inference.
- **Effect:** an executed tool operation with side effects, such as an image job.
  Argument and reference validation happen before its attempt budget is consumed.
- **Provider:** an explicitly selected chat or image backend. Provider adapters
  retain protocol-specific identity and continuation data.
- **Model preset:** a local router model configuration. The desktop selects it
  without overriding its inference tuning, except a reasoning effort the user
  explicitly chose.
- **Reasoning effort:** a per-model, per-request level (Default, Off, Low,
  Medium, High, XHigh). Default sends nothing. A turn takes a snapshot of the
  level when it starts.
- **Knowledge index:** the shared Retriever SQLite library: documents, chunks,
  FTS, embeddings, vector projections, provenance, identity and extraction snapshots.
- **Skill:** a managed instruction/resource package of untrusted third-party text.
  It is usable only after the user approves its exact `SKILL.md`; its text reaches
  the model outside the system message, and reading it does not execute its
  scripts or expand the available action set.
- **Desktop session:** the owner of drafts, active reply state and page snapshots.
  One desktop instance owns a chat database and its adopted router lifecycle.

See [architecture](docs/architecture.md) for module ownership and
[ADR-0001](docs/adr/0001-desktop-ownership.md) for desktop instance ownership.
