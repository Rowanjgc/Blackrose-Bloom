# SM-13: Outbound authorization, site grants and the conversation privacy latch

## Scope and ownership

- `src/blackrose/outbound.py`: `OutboundPolicy` (234–425), the
  `OutboundBatch`/`OutboundRequest`/`Approval` values (76–229) and
  `site_of`/`request_key`/`refusal_key` (124–152).
- `src/blackrose/storage/repository.py`: `mark_local_data_exposed` (432–437),
  `local_data_exposed` (439–445).
- `src/blackrose/storage/migrations.py`: Migration 8, the
  `conversations.local_data_exposed` column and its backfill (111–119).
- `src/blackrose/chat.py`: `expose_local_data` (724–735), `_outbound`
  (737–757), the knowledge/file entry points that expose local data (715–717,
  982–986), the paid-image entry point (1397–1399, 1438–1445).
- Coupled, not owned here: `turns.py` installs one policy per reply
  (`_install_outbound_policy` 430–441), reads the durable mark (317–331) and
  clears the policy in `finally` (367); `tool_execution.ToolExecutor.preauthorize`
  (195–210) and `tool_host.ToolHost.preauthorize` (50–54) forward a round's web
  calls; `web/fetcher.py` bounds redirects (61–78); `web/sites.py` decides which
  hosts may be granted (`site_key` 96–109, `trusted_by` 145–152).

Ownership, per [architecture.md](../architecture.md#modules-and-invariants) and its
module table (32, 36):

- **One policy per reply, on the controller.** `ChatController.outbound` is
  `None` outside a reply (`chat.py:139`) and is cleared in the turn's `finally`
  (`turns.py:367`). Every field except `_sites`, `_trusted` and the borrowed
  `sites` dict starts empty each reply. Nothing else in the reply lifecycle
  reaches `WebAccess` or a paid image provider: `chat.py:720–721` and
  `chat.py:1397–1399` are the only callers of `authorize`/`authorize_image`,
  and the architecture map states `tool_host.py` never exposes the policy
  object, only `outbound_active` and `preauthorize`.
- **One conversation-level map, by reference.** The policy receives
  `c.site_grants.setdefault(conversation.id, {})` (`turns.py:440`); the map
  outlives the reply and is dropped when the conversation is deleted
  (`chat.py:530`) or the app closes. It holds `host -> granted_while_private`.
- **One durable row, monotone.** `conversations.local_data_exposed` is written
  only as `1` (`repository.py:435`), by `turns.py:437` when a reply starts
  private and by `chat.py:733` before `mark_private()`. It is never cleared.
- **One asyncio loop, one tool call at a time.** `_run_tools` awaits
  `preauthorize` once and then each call sequentially (`turns.py:705–711`), so
  `mark_private()` can only move *between* calls of a round, never concurrently
  with `_check`/`_ask`. `preauthorize` and its round's calls are adjacent.
- **The model and the page own nothing.** The model chooses arguments; the
  policy decides. Page text never grants authority (the module docstring 17,
  and `note_links` records only links of a page the policy read); tool output is
  not scanned for URLs; prompt text is not an approver.

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `conversations.local_data_exposed` | `migrations.py:117`; `repository.py:432–445` | `INTEGER NOT NULL` 0/1 | Local data (names, listings, contents, library, references, attachments) reached the model in this conversation. Never cleared. | `turn._install_outbound_policy` `turns.py:437`; `ChatController.expose_local_data` `chat.py:733`. Migration 8 backfills 1 for any conversation with an assistant message (`migrations.py:118–119`). |
| `OutboundPolicy.private` | `outbound.py:245` | `bool` | This reply's privacy latch. | `__init__` `:245` (from `turns.py:436`), `mark_private` `:251` only. |
| `_sites` | `:247` | `dict[str, bool]`, borrowed | Conversation site grants: host → granted after local data was in use. | `authorize` `:304` only inside the policy; `turns.py:440` creates and `chat.py:530` drops the map. |
| `_allowed` | `:237` | `dict[request_key, str]` | Exact GETs (scheme, host, port, path, query) this reply may read, and the basis: `your message`, `search result`, `link on a page…` (`PAGE_LINK`), `approved by you`. | `__init__` `:237`; `note_results` `:273`; `note_links` `:284`; `_record` `:338`. |
| `_hops` | `:238` | `dict[request_key, int]` | Link depth of a `PAGE_LINK` entry; absence means 0 (typed/search/approved root). | `note_links` `:285`; `_record` pops `:339`. |
| `_queries` | `:239` | `set[str]` | Exact search strings approved this reply. | `_record` `:341`. |
| `_images` | `:240` | `set[(prompt, aspect_ratio, attachment ids)]` | Exact remote image identities approved this reply. | `authorize_image` `:389`. |
| `_prompts` | `:242` | `int`, 0…`MAX_PROMPTS` (8) | Approval questions shown this reply; a batch costs one. | `preauthorize` `:324`; `_ask` `:406`. |
| `_declined` | `:243` | `bool` | A request was refused in this reply. | `preauthorize` `:333`; `_ask` `:409`. Never reset. |
| `_refused` | `:244` | `set[(kind, identity)]` | Batch rows the user left unchecked. Identity is `refusal_key` (`:144–151`). | `preauthorize` `:332`. |
| `_approve` | `:241` | `Approver \| None` | The native sheet, or nothing. | `__init__` only. |
| `_trusted` | `:248` | `tuple[str]` | Normalized `trusted_hosts` snapshot for this reply. | `__init__` only. |
| `ChatController.site_grants` | `chat.py:137` | `dict[int, dict[str, bool]]` | Per-conversation grants for the app's lifetime. | `turns.py:440` (`setdefault`), `outbound.py:304` through the borrowed map, `chat.py:530` (pop). |

## States

Five orthogonal machines. Each is per conversation or per reply as stated.

**S1 — privacy latch** (per conversation; `P` = `policy.private`, `D` = the
durable row). Mutually exclusive and monotone:

| State | Predicate |
| --- | --- |
| `CLEAR` | `P == False` and `D == 0` |
| `PRIVATE` | `P == True`; in production `D == 1` because both writers write `D` first (`chat.py:733`, `turns.py:437`). A direct `mark_private()` call leaves `D` unchanged (H8), which is why that is not a production entry point. |
| `DURABLE` | `D == 1`; every later reply is installed with `P == True` (`turns.py:319`, `:436`) |

`DURABLE` is absorbing for the conversation; `CLEAR` is reachable only for a
conversation that has never exposed local data (a new conversation id is a new
machine instance).

**S2 — site grant** (per `(conversation, host)`):

| State | Predicate | Effect while `CLEAR` | Effect while `PRIVATE` |
| --- | --- | --- | --- |
| `ABSENT` | `host not in _sites` | asks | asks |
| `PRE_LOCAL` | `_sites[host] is False` | allows (`site_allows:256`) | asks |
| `POST_LOCAL` | `_sites[host] is True` | allows | allows |

`PRE_LOCAL → POST_LOCAL` is the only write (`:304`); nothing writes backwards
(see I8).

**S3 — approval gate** (per reply). Mutually exclusive by precedence
`CLOSED > SATURATED > OPEN`, matching the check order in `_ask`:

| State | Predicate | New request that needs approval |
| --- | --- | --- |
| `CLOSED` | `_declined == True` | denied `approval_declined` without a sheet (`:399`) |
| `SATURATED` | `not _declined and _prompts >= MAX_PROMPTS` | denied `approval_limit` without a sheet (`:402`) |
| `OPEN` | neither | asks, then `OPEN` or `CLOSED` |

**S4 — batch question** (per model round, derived):

| State | Predicate |
| --- | --- |
| `NO_BATCH` | fewer than 2 pending, no approver, `CLOSED`, or `SATURATED` (`:321`) — each call is asked alone |
| `ASKED_ONCE` | at least 2 pending, an approver, `OPEN`; up to `MAX_BATCH` rows (`:319`), site not offered (`:323`), one prompt (`:324`) |
| `ANSWERED` | each row recorded as approved (`_record`) or refused + `CLOSED` (`:328–333`) |

**S5 — remote image identity** (per `(prompt, aspect_ratio, references)`):

| State | Predicate |
| --- | --- |
| `AUTOMATIC` | `not private and not declined` (`:380`) |
| `PENDING` | otherwise, identity not in `_images` (`:382`); T4 decides |
| `APPROVED` | identity in `_images` (`:389`) — reachable only while `private` (see I4 note) |

## Events

1. Reply install: `_install_outbound_policy` computes `private` from remembered
   notes or `_private_context` (durable row, attachments, file/directory refs,
   local evidence) and writes the row when private (`turns.py:317–331`,
   `:430–441`).
2. `expose_local_data()`: a `knowledge_*` call (`chat.py:715–717`) or a
   `file_*` call other than `directory_create`/`document_create`
   (`chat.py:982–986`) → durable write, then `mark_private` (`chat.py:724–735`).
3. `authorize(name, arguments)` for `fetch_content` or `web_search`
   (`chat.py:737–757`).
4. `preauthorize(calls)` once per round, before any call runs
   (`turns.py:705`, `tool_execution.py:195–210`).
5. `note_results` after a search (`chat.py:747–748`); `note_links` after a
   fetch (`chat.py:753`, `:756`).
6. `authorize_image` for a paid remote image job (`chat.py:1397–1399` →
   `1438–1445`).
7. The approver's answer (`True`, `SITE`, a tuple for a batch, anything else),
   an absent approver, or cancellation of the reply task while the sheet is
   open.
8. Reply end clears the policy (`turns.py:367`); conversation deletion and app
   exit drop the site-grant map (`chat.py:530`).
9. Upgrade/startup: Migration 8's backfill; every reply re-reads the durable
   row (`turns.py:319`).

## Transition table

### T1 — privacy latch (S1)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `CLEAR` | reply install, local context present | `memory_notes` or `_private_context` true `turns.py:436` | `UPDATE … = 1` `:437`, then policy with `private=True` `:438–441` | `PRIVATE`, `DURABLE` |
| `CLEAR`/`PRIVATE` | `expose_local_data` | policy exists and already private `chat.py:730–731` | none (the row was written at install) | unchanged |
| `CLEAR` within a reply | `expose_local_data` | `private` false `chat.py:730` | row `= 1` `:733`, then `mark_private` `:735` | `PRIVATE`, `DURABLE` |
| `CLEAR` | `mark_private()` called directly | — `outbound.py:250–251` | `private = True` only; no durable write | `PRIVATE` (reply-only; see H8) |
| `PRIVATE` | new reply | row `= 1` `turns.py:319` | new policy `private=True` | `PRIVATE` |
| `DURABLE` | conversation deleted, same title recreated | new id | new row `= 0` | `CLEAR` (new instance) |
| any | anything else | — | no transition to `CLEAR` exists | unchanged |

### T2 — `fetch_content` basis selection (inside `_check`, `:346–363`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| proposed GET | always | `validate_fetch_url` raises `:349` | `ValueError`; no sheet and no request | denied locally |
| proposed GET | `_allowed[key]` is `your message`/`search result`/`approved by you` | `:351`, `:354` | return basis, no sheet | authorized |
| proposed GET | `_allowed[key]` is `PAGE_LINK` | `private` false `:354` | return `PAGE_LINK`, no sheet | authorized |
| proposed GET | `_allowed[key]` is `PAGE_LINK` | `private` true `:354` | fall through to the gates below | asks |
| proposed GET | site grant covers it | `site_allows` `:356` (`_sites[host]` true, or false while not private) | return `SITE_GRANT` | authorized |
| proposed GET | trusted site covers it | `trusted_allows` `:358` (https:443; no query when private) | return `TRUSTED_SITE` | authorized |
| proposed GET | none of the above | `:360–363` | `OutboundRequest` with `site=site_of(url)` | S3 gate |

### T3 — `web_search` basis selection (`:364–371`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| proposed query | always | `not private` `:366` | `automatic` | authorized |
| proposed query | private | `query in _queries` `:368` | `approved by you` | authorized |
| proposed query | private | otherwise `:370` | request | S3 gate |

### T4 — ask gate (`_ask`, `:392–415`) and recording (`authorize:287–305`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| needs approval | any | no approver `:394` | deny `approval_unavailable`; budget untouched | denied |
| needs approval | any | identity in `_refused` `:397` | deny `approval_declined` | denied |
| needs approval | any | `_declined` `:399` | deny `approval_declined` | denied |
| needs approval | any | `_prompts >= 8` `:402` | deny `approval_limit`; no sheet | denied |
| needs approval | any | otherwise `:406` | `_prompts += 1`, then await the sheet `:407` | `ASKED` |
| `ASKED` | answer `True` | `:408` | `_record` `:298`, `:336–341` | authorized |
| `ASKED` | answer `SITE` | request offers a site `:408` | `_record`; `_sites[site] = private or old` `:304` | authorized + T7 |
| `ASKED` | answer `SITE`, no site offered | `:408` | `_declined = True`, deny `approval_declined` `:409–412` | `CLOSED` |
| `ASKED` | anything else (False, `None`, `'yes'`, wrong-length tuple) | `:408` | `_declined = True`, deny `approval_declined` | `CLOSED` |
| `ASKED` | cancellation while open | — | `CancelledError` propagates; no `_record`, nothing sent | reply ends |
| authorized | — | `_refused` not matched `authorize:292` | return the basis without asking | unchanged |

`_record` stores a fetch under its `request_key` and drops its hop count
(`:337–339`); a search under its exact string (`:341`); an image identity at
`:389`.

### T5 — batch question (`preauthorize`, `:307–334`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| calls collected | `_check` per web call | `KeyError/TypeError/ValueError` `:315–318` | skip; the call reports on its own | — |
| ≥2 pending | batch eligible | no approver, `CLOSED`, or `SATURATED` `:321` | no batch; each call hits T4 | `NO_BATCH` |
| ≥2 pending | batch eligible | up to `MAX_BATCH` `:319` | one sheet, `site=''` on every row `:323`, one prompt `:324` | `ASKED_ONCE` |
| `ASKED_ONCE` | answer `True` | `:326–327` | every row approved | `ANSWERED` |
| `ASKED_ONCE` | tuple/list of the right length | `:326–327` | each row `is True` → approved, else refused + `CLOSED` | `ANSWERED` |
| `ASKED_ONCE` | anything else | `:326–327` | every row refused + `CLOSED` | `ANSWERED` |
| row approved | later `authorize` | identity not in `_refused` | runs on its recorded basis, even though `_declined` is true | authorized |
| row unchecked | later `authorize`, even if the round has since granted it | `authorize:292` or `_ask:397` | denied `approval_declined` | refused |
| row beyond `MAX_BATCH` | later `authorize` | — | asked alone through T4 | S3 gate |

### T6 — remote image (`authorize_image`, `:376–390`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| job | always | `not private and not declined` `:380` | `automatic` | authorized |
| job | otherwise | identity in `_images` `:382` | `approved by you` | authorized |
| job | otherwise | not in `_images` `:387` | `_ask` (T4), then add identity `:389` | `APPROVED` or denied |
| `APPROVED` | same identity again this reply | `:382` | no sheet | authorized |

### T7 — site grant (`:304`, `site_allows:253–256`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT`/`PRE_LOCAL` | single answer `SITE` while `private` | request.site set `:408` | `_sites[site] = True` `:304` | `POST_LOCAL` |
| `ABSENT`/`PRE_LOCAL` | single answer `SITE` while not private | `:408` | `_sites[site] = False` `:304` | `PRE_LOCAL` |
| `POST_LOCAL` | read | `:256` | allowed in both privacy states | `POST_LOCAL` |
| any | batch answer `SITE` | rows carry `site=''` `:323` | refused; map untouched | unchanged |
| any | conversation deleted | `chat.py:530` | map dropped | `ABSENT` (all hosts) |

### T8 — link depth (`note_links:275–285`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| page read under any basis | its links | `hops = _hops.get(page,0)+1 > MAX_LINK_HOPS` `:278–280` | stop; no link recorded | unchanged |
| page read | links | `hops <= MAX_LINK_HOPS` and key not already in `_allowed` `:283` | `_allowed[key] = PAGE_LINK`, `_hops[key] = hops` `:284–285` | `PAGE_LINK` at depth `hops` |
| `PAGE_LINK` | approved by the user | `_record:337–339` | basis becomes `approved by you`; hop count dropped | depth 0 |
| `PAGE_LINK` | next reply | — | new policy; links are not carried over | `ABSENT` |

## Invariants

1. **The durable mark is monotone.** After `local_data_exposed = 1`, no code
   writes 0; the only writes are the Migration 8 default and backfill and the
   two `UPDATE … = 1` paths (`repository.py:435`, `turns.py:437`,
   `chat.py:733`). Pinned by
   `RepositoryLatchTests.test_the_mark_is_idempotent_and_durable` and
   `tests/test_storage.py::test_chats_with_replies_start_guarded_after_the_exposure_migration`.
2. **`private` is monotone within a policy.** The only writes are `:245`
   (constructor) and `:251` (`mark_private`); both only ever set `True`.
   `PrivacyLatchTests.test_a_link_granted_before_local_data_is_rechecked_after_it_is_used`
   calls `mark_private` twice and asserts the latch stays set.
3. **Every privacy-filtered grant map is filtered at each read.**
   `_sites` is read only in `site_allows:256`, guarded in the same expression by
   `not self.private`; its callers are `_check:356` and `stays_allowed:266`, and
   both re-evaluate the flag on every call. `_queries` is read only
   at `:368`, after the `not self.private` return at `:366`. `_images` is read
   only at `:382`, after the `not (private or declined)` return at `:380`.
   `_allowed` is read for authorization only at `:351`; a `PAGE_LINK` basis is
   rejected while private at `:354`. `_hops` is read only at `:278` and decides
   whether a new link entry is admitted; it never authorizes. `note_links` reads
   `_allowed` at `:283` only to avoid overwriting an entry. Pinned by
   `PrivacyLatchTests.test_a_site_grant_recorded_before_local_data_stops_applying_when_it_is_used`,
   `…test_a_link_granted_before_local_data_is_rechecked_after_it_is_used` and
   `…test_a_trusted_site_loses_query_addresses_but_not_plain_ones`.
4. **Only the exact request survives the latch.** A user-typed GET, a
   user-approved GET, a search string and an image identity remain authorized
   across `mark_private` because the bytes they authorize do not change; a site
   grant or `PAGE_LINK` does not, because those widen to addresses the model has
   not yet written. Pinned by `…test_only_exact_requests_survive_the_latch` and
   `tests/test_security_boundaries.py::test_grant_from_before_local_data_asks_again_once_it_is_used`.
   "Do not change" holds because `validate_fetch_url` returns the address as
   `urlsplit` reads it and the fetcher sends that, so characters the key ignores
   (outer Unicode spaces, tabs, newlines) cannot ride a grant as an encoded
   payload; pinned by `TransmittedRequestTests`.
   A consequence of the code, not a gap: `_queries` and `_images` can only be
   populated while private (T3, T6), so there is no pre-private entry to
   invalidate.
5. **A refused request is never sent in the reply that refused it.** An identity
   in `_refused` is denied on the automatic path (`authorize:292`) and on the
   ask path (`_ask:397`); the identity for a GET is its `request_key`
   (`refusal_key:144–151`), so a fragment, host case or explicit default port
   cannot re-open it. `_declined` then blocks every other new question
   (`:399`). Pinned by the four `BatchRefusalTests`.
6. **A batch never widens to a site.** Rows are rebuilt with `site=''`
   (`:323`) and `_ask` accepts `SITE` only when the request offers one
   (`:408`); a `SITE` answer to a batch therefore refuses every row
   (`:326–327`). Pinned by
   `BatchRefusalTests.test_a_batch_answer_of_site_grants_nothing_and_declines_every_row`.
7. **A decline does not cancel an approval the user gave in the same sheet.**
   Approved rows are recorded before any call runs, so they run on their basis
   even with `_declined` true; only *new* questions are blocked. Pinned by
   `BatchRefusalTests.test_an_unchecked_request_stays_refused_when_the_round_grants_it`.
8. **At most `MAX_PROMPTS` sheets per reply, counted when shown.** Both paths
   increment before awaiting the approver (`:324`, `:406`) and both refuse at
   `>= MAX_PROMPTS` before incrementing (`:321`, `:402`). A batch costs one
   prompt for up to `MAX_BATCH` rows. Pinned by
   `PromptBudgetTests.test_a_full_budget_refuses_the_next_request_without_showing_it`
   and `tests/test_security_boundaries.py::test_a_batch_counts_once_toward_the_prompt_limit`.
9. **A site grant never downgrades.** `:304` writes `True` only when `private`
   was true at record time (`PRE_LOCAL`/`POST_LOCAL` distinction); a `True`
   entry always satisfies `site_allows`, so it is never asked about again and
   cannot be re-answered as `False`. Pinned by
   `PrivacyLatchTests.test_a_site_answer_before_local_data_is_recorded_as_pre_local`
   and `tests/test_security_boundaries.py::test_grant_recorded_after_local_data_is_covered_without_asking_when_not_private`.
10. **Redirect hops are bounded and re-checked where a grant authorized the
    read.** `PageFetcher` validates every target and stops after
    `MAX_REDIRECTS = 5` (`fetcher.py:61–78`, `constants.py:7`); a read authorized
    by a site grant or trusted site passes `stays_allowed` on every hop
    (`chat.py:749–752`), which re-applies the current `private` state.
11. **Link depth is at most `MAX_LINK_HOPS = 2` from a non-link grant, and an
    approval restarts the chain.** `note_links:278–280` refuses deeper entries;
    `_record:339` drops the hop count. Pinned by `LinkHopTests`.
12. **Nothing is sent that SSRF validation would refuse, and no sheet is shown
    for it.** `_check:349` runs first in both `authorize` and `preauthorize`;
    `preauthorize` treats the `ValueError` as "fails on its own" (`:315–318`).
    Pinned by `tests/test_security_boundaries.py::test_approval_never_bypasses_public_destination_checks`.
13. **Decisions are logged without payloads.** `_deny:422–425` logs kind, code
    and host only; `:413–414` the same. Pinned by
    `tests/test_security_boundaries.py::test_refusals_are_logged_without_request_contents`.

## Hazards and open questions

1. **defect (found, fixed) — `outbound.py:292`, `:332`, `:397` (pre-fix).**
   A batch refusal was recorded under the raw argument text while every grant is
   recorded under `request_key`. An unchecked URL could therefore be fetched in
   the same reply by spelling it `…/x#fragment`, `https://HOST/x` or
   `https://host:443/x`: `_allowed` already held the request key (e.g. as a
   page link granted after the sheet), the automatic path returned that basis,
   and the `_refused` membership test missed. The bytes sent are the same GET
   the user unchecked, so the refusal was defeated. Severity: medium (needs an
   adversarial model and a page/search grant for the same key, both plausible
   under prompt injection). Demonstrated before the fix by a standalone repro
   (`linked#part: link on a page read in this reply`) and by
   `BatchRefusalTests.test_the_refusal_covers_every_spelling_of_the_same_request`,
   which failed with "OutboundDenied not raised" against the pre-fix code.
   Confirmed by the same test after the fix. Fix: `refusal_key` (I5).

2. **fragile — `outbound.py:247` with `turns.py:440` and `chat.py:137`.** The
   policy borrows the conversation's mutable dict and mutates it (`:304`). I9
   depends on that being the only writer and on no policy outliving its reply
   (`turns.py:367`). I found no path that keeps a policy past the turn (`grep`
   for `.outbound` shows only `chat.py`, `turns.py` and `tool_host.py`, all
   access-by-reference at call time), but a future caller that stores one could
   write `True` into a later turn's grants. Would confirm: a retained policy
   reference across `turns.py:367`. No test; the window is unreachable today.

3. **documentation-gap — `chat.py:137`, `turns.py:440`.** `site_grants` is a
   public controller attribute with no validation, and `OutboundPolicy` trusts
   whatever dict it is given. It is not reachable from the model, a page, the
   MCP surface or the GUI bridge (no reference outside `chat.py`, `turns.py` and
   tests), so forging a grant requires arbitrary code in the app process, which
   already owns the browser and the database. Severity: `none-found` for the
   stated threat model; recorded because the answer to "does `site_grants`
   expose a grant-forging surface?" is "only to in-process Python".

4. **documentation-gap — `outbound.py:261`.** Once private, a trusted site
   covers only addresses without a query. A model-written *path* on a trusted
   host (`https://docs.example/<private text>`) still leaves without a sheet;
   only the query is treated as likely to be logged by the site's own search
   and analytics. This is narrower than a `POST_LOCAL` site grant and the
   limitation is stated in the module docstring (29–34), so it looks deliberate.
   Would confirm: an explicit statement that trusted hosts are trusted with
   path-encoded data but not query-encoded data.

5. **fragile — `outbound.py:284`.** `note_links` records `PAGE_LINK` entries
   even while `private`, where they can never authorize; `_allowed` is
   reply-scoped and its size is bounded by link depth (2) but not by page
   degree. Cost is memory only; no test pins a bound. Would confirm: a reply
   with a page declaring thousands of links (bounded in practice by the fetch
   response size limit).

6. **none-found — redirects.** Hops are bounded (I10) and grant-authorized reads
   re-check each hop. Exactly-authorized reads do not pass `redirect_allowed`
   (`chat.py:754–756`), which is intended: there is no grant to widen, and the
   redirect target is chosen by the server, not by the model. Note that
   `note_links` is called with the *requested* URL (`chat.py:753`, `:756`) even
   after redirects, so a redirect chain does not consume link depth; depth per
   URL is still bounded.

7. **none-found — `_declined`.** No write sets it false (`:243`, `:333`,
   `:409`), so it is terminal for the reply. A new message starts a new policy
   (`turns.py:438`), which is the documented "until the next message" scope.
   Pinned by `BatchRefusalTests.test_a_decline_blocks_later_questions_without_asking_again`.

8. **documentation-gap — `outbound.py:250–251`.** `mark_private()` performs no
   durable write. Durability rests on both production callers ordering the row
   first (`chat.py:733` before `:735`; `turns.py:437` before construction). A
   future direct caller would get a reply-only latch that a restart clears.
   Would confirm: any call site of `mark_private` outside `chat.py`.

9. **none-found — prompt budget ordering.** The budget is consumed when the
   sheet is shown, not when it is answered, so a cancelled or failed reply can
   leave it spent; the policy is discarded with the reply (`turns.py:367`), so
   this has no observable effect. `tests/test_security_boundaries.py::test_stop_while_approval_is_pending_sends_nothing`
   shows nothing is sent when a sheet is abandoned.

## Formalization decisions

**One source change** in `outbound.py` (the one file this bead owns):

- Added `refusal_key(kind, target)` (`:144–151`) and used it at the three
  `_refused` sites (`authorize:292`, `preauthorize:332`, `_ask:397`), with the
  field annotated `set[tuple[str, object]]` (`:244`). A fetch refusal is now
  keyed by `request_key`, the same identity every grant uses; a search refusal
  keeps its exact string. This closes H1. It never widens an approval: the only
  behavior change is that a request the user unchecked is refused under more
  spellings.
- Deliberate consequence: if one batch contains two spellings of the same GET
  and the user checks one and unchecks the other, the refusal wins and neither
  runs. That is the safe reading of an unchecked row, and no existing test or
  documented behavior expects the checked duplicate to run. `preauthorize`
  still lists both rows; deduplicating rows by `request_key` would change the
  batch contents and `MAX_BATCH` accounting, so it was not done.
- Top-of-file documentation and the approval sheet text are unchanged: the user
  still sees the exact argument text, and `OutboundDenied.request` still carries
  the raw target the caller wrote.

**No change proposed to `chat.py`, `repository.py` or `migrations.py`:**

- `mark_local_data_exposed` is already the minimal monotone write
  (`repository.py:432–445`); a `rowcount` check would only catch a deleted
  conversation, and deletion is refused while a reply is generating
  (`chat.py:521–523`), so no obligation is stranded.
- `expose_local_data`'s early return (`chat.py:730–731`) is sound because every
  policy with `private=True` was installed by a path that wrote the row
  (`turns.py:437`). Changing it would add a redundant write.
- Migration 8's backfill is deliberately conservative (`migrations.py:111–119`)
  and needs no change.
- Follow-up worth considering, not a patch: make `site_grants` private to
  `ChatController` (or hand the policy a copy plus a write callback) so H2/H3
  are structural rather than conventional. It is wider than this bead and would
  touch `chat.py`/`turns.py` concurrently with other work, so it is recorded
  here only.

## Verification

All commands from the repository root, offline, each test runner in its own
temporary XDG tree.

| Command | Result |
| --- | --- |
| `.venv/bin/python scripts/check_unit_tests.py -p test_sm_13_outbound_policy.py -v` | `Ran 16 tests in 0.045s` — `OK` |
| `.venv/bin/python scripts/check_unit_tests.py -p test_security_boundaries.py -q` | `Ran 73 tests in 2.058s` — `OK` (the outbound suite; there is no `test_outbound.py` in this repository) |
| `.venv/bin/python scripts/check_unit_tests.py -p test_capability_disclosure.py -q` | `Ran 21 tests` — `OK` |
| `.venv/bin/python scripts/check_unit_tests.py -p test_tool_handlers.py -q` | `Ran 10 tests` — `OK` |
| `.venv/bin/python scripts/check_unit_tests.py -p test_memory.py -q` | `Ran 47 tests` — `OK` |
| `.venv/bin/python scripts/check_unit_tests.py -p test_storage.py -q` | `Ran 17 tests` — `OK` |
| `.venv/bin/python scripts/check_unit_tests.py -p test_saved_compatibility.py -q` | `Ran 6 tests` — `OK` |
| `.venv/bin/python scripts/check_unit_tests.py -p test_skill_authority.py -q` | `Ran 1 test` — `OK` |
| `.venv/bin/python scripts/check_unit_tests.py -p test_image_generation.py -q` | `Ran 14 tests` — `OK` |
| `.venv/bin/python scripts/check_unit_tests.py -p test_turns.py -q` | `Ran 28 tests` — `OK` |
| `.venv/bin/python -m ruff check src/blackrose/outbound.py tests/test_sm_13_outbound_policy.py` | `All checks passed!` |
| `git diff --check` | no output (exit 0) |

Regression evidence: with `refusal_key` replaced by the pre-fix raw tuple at the
three sites, `test_the_refusal_covers_every_spelling_of_the_same_request` fails
with `AssertionError: OutboundDenied not raised` for `variant='…#part'` and the
other 15 tests pass; with the fix, all 16 pass. A standalone repro against the
pre-fix code printed `linked#part: link on a page read in this reply`.

Not run: `scripts/check_health.py` (the worktree carries other agents' in-flight
edits to unrelated modules, so its whole-repo gate would not describe this
change) and the native/browser probes, which this policy-only change does not
touch. `tests/test_outbound.py` named in the brief does not exist here;
`tests/test_security_boundaries.py` is the maintained outbound suite.
