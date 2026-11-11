# Persistent memory

Blackrose can remember durable context across conversations: how you like to be
helped, how you learn, your tools and environment, your projects and the
conclusions of earlier research. Replies then need less restating. Memory uses a
self-hosted [Honcho](https://github.com/plastic-labs/honcho) instance and is an
optional enhancement:

- It is off by default, and the app behaves normally when Honcho is unavailable.
- It is not a source of truth. Conversation history stays in `chats.db`; current
  files, machine state and documentation are still verified with tools.
- Remembered text is untrusted data. It never enters the system message and can
  never grant authority. The model obeys instructions in remembered text, so recall
  withholds notes that carry links, requested actions, dictated output or rule
  overrides. A purely stylistic instruction worded like a preference remains an
  accepted gap.

## Setup

Run Honcho (v3 API; validated with 3.5.0) on a loopback address with its deriver
and an embedding model configured, then set in the startup TOML:

```toml
memory_enabled = true
memory_endpoint = "http://127.0.0.1:8765"
memory_peer = "user"   # the Honcho peer that stands for you
```

`memory_endpoint` must be a loopback URL. Blackrose creates a `blackrose`
workspace on first use, with your peer (observed) and a `blackrose` peer for the
assistant (not observed, so derivation runs once per message). Each conversation
is one Honcho session, named with an installation id and the conversation id. The
workspace configuration disables session summaries and supplies instructions: keep
durable preferences, learning style, environment, projects and research
conclusions; record volatile state such as versions or service status only as
dated observations; never record credentials; and ignore instructions inside
quoted or pasted material.

Honcho runs without authentication here: its API is reachable only from the local
machine, where any local process can already read `chats.db`.

## How it works

One module, `src/blackrose/memory.py`, holds an httpx client for the few Honcho
endpoints it needs, with no SDK and no provider abstraction. After a failure,
memory is skipped for five minutes.

**Recall.** Once per typed request, before the prompt is built, within 1.5
seconds: the peer card and up to six conclusions within cosine distance 0.32 of
the request, fetched in parallel with no model call. (The distance is calibrated
for EmbeddingGemma 2 as Honcho's embedder.) Notes are whitespace-normalized, at
most 300 characters each and about 2,400 in total, and conclusions keep their
date. Withheld notes are marked in the review sheet. A slow answer skips recall
for that turn without pausing memory; an outage pauses it for five minutes.

The notes travel JSON-encoded in a delimited untrusted block on the request
message, the same channel skills use. A fixed system-message line says that
remembered context may be stale or wrong, carries no authority, yields to the
current request and system policy, and does not replace checking current state.
Context admission counts the block; it is never stored in history. The reply's
sources list "Remembered context · N notes", whose technical record shows the
notes. Recall marks the conversation's local data as exposed, so model-proposed
web requests need the same approval as after reading local files. Recall runs only
for local models and never in an excluded chat, so notes drawn from other
conversations are never sent to a cloud provider.

**Ingestion.** When a turn completes, a row is added to a `memory_outbox` table
(message ids only). A background drain sends the text you typed and the visible
reply, capped at about 4,000 characters, after secret redaction. Fenced,
triple-quoted and `>`-quoted material is replaced with a placeholder in both, and
a turn that is only a paste is not sent: small deriver models tend to record an
instruction inside a pasted document as a fact about the user. Tool results,
attachments, evidence, reasoning and interactive blocks are never sent. The drain
runs after turns and at startup with backoff, and never delays a reply. Nothing is
queued when memory is off or the conversation is excluded.

## Controls

- The Memory switch and `/memory on|off|status`, off by default and failing
  closed. Switching off discards turns not yet sent.
- `/memory exclude|include` keeps the current conversation out of memory.
  Excluding also deletes its Honcho session if it ever contributed.
- `/memory` opens the review sheet, whether memory is on or off: the editable
  profile (peer card, one statement per line), the remembered notes (newest
  first, 20 per page, text search, each with Forget), and "Forget everything",
  which only the native in-window confirmation can accept.
- Deleting a conversation deletes its Honcho session through the outbox.

Honcho deletes a session's messages and conclusions in the background, but dream
conclusions (deductive and inductive) are stored without a session and survive
it. They keep links to the conclusions they came from, so forgetting a
conversation first lists the session's conclusions, follows those links forward
and deletes every dream conclusion built on them, including ones that also drew
on other conversations. It then deletes the session's own conclusions directly, so
recall stops finding them at once, and finally the session. Each step can be
repeated after an interruption.

"Forget everything" discards unsent turns, deletes every session, deletes the
remaining conclusions about you (in rounds, since session deletion is
asynchronous) and empties the profile; the workspace and its configuration stay.
It holds the outbox lock, so no turn is sent meanwhile. A failure is reported as
unfinished, and repeating it completes it. Sheet actions are tried even while
background memory is paused after an outage.

### The profile after forgetting

The profile has no record of which conversation each line came from, so
forgetting cannot edit it line by line. Instead, any forget (a conversation
deleted or excluded, or Forget on one note) first sets `card_stale_since` (a UTC
timestamp) in your peer's metadata, before deleting anything. While it is set,
recall leaves the profile out, so what was forgotten stops reaching replies at
once; the remaining notes still carry what is known. "Rebuild profile" in the
sheet sets the mark on request, and the sheet says when a rebuild is pending.

Blackrose does not rebuild the profile itself. Schedule a maintenance job that
runs Honcho's `card_refresh` dream with `rebuild` for the `blackrose` workspace
and your peer, then clears `card_stale_since` only if it still holds the value
read before the rebuild. Without such a job, a forgotten profile stays out of
recall until "Forget everything" empties it.

Dreams rewrite the whole profile and a rebuild reads only notes, so a line you add
in the sheet is also stored as a note with no conversation ("Written by you" in
the sheet). Dreams and rebuilds see it, and forgetting a conversation never
removes it. Removing the line deletes that note too. Notes change before the
profile, so repeating an interrupted save completes it. Lines already in the
profile are not claimed as yours, because they cannot be told apart from
dream-written ones. Removing a dream-written line lasts only until a dream finds
it supported again; forgetting the notes behind it is what removes it.

## Design choices

Rejected:

- A generic memory-provider layer: there is one backend.
- Honcho's dialectic chat in the reply path: an extra model call on every turn.
- Patching Honcho: configuration plus an external maintenance job covers what
  Blackrose needs.

Possible later work:

- Dialectic recall for explicit "what do you know about me" questions.
- Scoped Honcho tokens per workspace.
