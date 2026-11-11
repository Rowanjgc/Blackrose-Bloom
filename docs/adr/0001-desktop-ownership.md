# One desktop instance per chat database

Status: accepted, 2026-10-15.

The desktop owns crash recovery and the adopted local router. Multiple desktop
instances previously shared pending chat rows and could recover another live
reply or stop its router. The application is a single-window desktop client.

Acquire a nonblocking advisory lock in the chat database directory before opening
storage or starting/adopting a router. Hold it through session, runtime and router
shutdown. A second launch exits with a readable message. Never unlink the lock
file; the operating system releases ownership when its descriptor closes or the
process exits. Scratch runtimes lock their own database directories.

Retriever CLI/MCP processes remain independently usable: their own cooperating
writer lock and SQLite snapshots protect the shared knowledge index. The desktop
lock does not govern externally managed inference processes or different chat
database directories.

Revisit this decision only when multiple live desktop windows are an explicit
product requirement. That change needs session-owned turns, abandoned-session
recovery and shared router lifetime ownership together.
