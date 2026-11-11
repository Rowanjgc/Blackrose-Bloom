# SM-13: Outbound authorization, site grants and the conversation privacy latch

## Scope and ownership

- `src/blackrose/outbound.py`: `src/blackrose/outbound.py::OutboundPolicy`, the
  `OutboundBatch`/`OutboundRequest`/`Approval` values and
  `src/blackrose/outbound.py::site_of`/`request_key`/`refusal_key`.
- `src/blackrose/storage/repository.py`:
  `src/blackrose/storage/repository.py::Repository.mark_local_data_exposed`,
  `src/blackrose/storage/repository.py::Repository.local_data_exposed`.
- `src/blackrose/storage/migrations.py`: Migration 8 in
  `src/blackrose/storage/migrations.py::MIGRATIONS`, the
  `conversations.local_data_exposed` column and its backfill.
- `src/blackrose/chat.py`: `src/blackrose/chat.py::ChatController.expose_local_data`,
  `src/blackrose/chat.py::ChatController._outbound`, the knowledge/file entry points
  that expose local data (`src/blackrose/chat.py::ChatController._call_tool`,
  `src/blackrose/chat.py::ChatController.file_tool`,
  `src/blackrose/chat.py::ChatController.run_skill_script`), the paid-image entry point
  (`src/blackrose/chat.py::ChatController._generate_image` →
  `src/blackrose/chat.py::ChatController._authorize_remote_image`).
- Coupled, not owned here: `turns.py` installs one policy per reply
  (`src/blackrose/turns.py::TurnRunner._install_outbound_policy`), reads the durable
  mark (`src/blackrose/turns.py::TurnRunner._private_context`) and clears the policy
  in `src/blackrose/turns.py::TurnRunner._run`'s `finally`;
  `src/blackrose/tool_execution.py::ToolExecutor.preauthorize` and
  `src/blackrose/tool_host.py::ToolHost.preauthorize` forward a round's web calls;
  `src/blackrose/web/fetcher.py::PageFetcher._fetch_once` bounds redirects;
  `web/sites.py` decides which hosts may be granted
  (`src/blackrose/web/sites.py::site_key`, `src/blackrose/web/sites.py::trusted_by`).

Ownership, per [architecture.md](../architecture.md#modules-and-invariants) and its
module table (the `tool_host.py` and `outbound.py` rows):

- **One policy per reply, on the controller.** `ChatController.outbound` is
  `None` outside a reply (`src/blackrose/chat.py::ChatController.__init__`) and is
  cleared in the turn's `finally` (`src/blackrose/turns.py::TurnRunner._run`). Every
  field except `_sites`, `_trusted` and the borrowed `sites` dict starts empty each
  reply. Nothing else in the reply lifecycle reaches `WebAccess` or a paid image
  provider: `src/blackrose/chat.py::ChatController._outbound` and
  `src/blackrose/chat.py::ChatController._authorize_remote_image` are the only
  callers of `authorize`/`authorize_image`, and the architecture map states
  `tool_host.py` never exposes the policy object, only `outbound_active` and
  `preauthorize`.
- **One conversation-level map, by reference.** The policy receives
  `c.site_grants.setdefault(conversation.id, {})`
  (`src/blackrose/turns.py::TurnRunner._install_outbound_policy`); the map outlives
  the reply and is dropped when the conversation is deleted
  (`src/blackrose/chat.py::ChatController._delete_current_conversation`) or the app
  closes. It holds `host -> granted_while_private`.
- **One durable row, monotone.** `conversations.local_data_exposed` is written
  only as `1` (`src/blackrose/storage/repository.py::Repository.mark_local_data_exposed`),
  by `src/blackrose/turns.py::TurnRunner._install_outbound_policy` when a reply
  starts private and by `src/blackrose/chat.py::ChatController.expose_local_data`
  before `mark_private()`. It is never cleared.
- **One asyncio loop, one tool call at a time.** `_run_tools` awaits
  `preauthorize` once and then each call sequentially
  (`src/blackrose/turns.py::TurnRunner._run_tools`), so `mark_private()` can only
  move *between* calls of a round, never concurrently with `_check`/`_ask`.
  `preauthorize` and its round's calls are adjacent.
- **The model and the page own nothing.** The model chooses arguments; the
  policy decides. Page text never grants authority (the `outbound.py` module
  docstring, and `note_links` records only links of a page the policy read); tool
  output is not scanned for URLs; prompt text is not an approver.

## State variables

Policy fields are set in `src/blackrose/outbound.py::OutboundPolicy.__init__`;
writers below are `OutboundPolicy` methods unless a path is given.

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `conversations.local_data_exposed` | Migration 8 in `src/blackrose/storage/migrations.py::MIGRATIONS`; `src/blackrose/storage/repository.py::Repository.mark_local_data_exposed` | `INTEGER NOT NULL` 0/1 | Local data (names, listings, contents, library, references, attachments) reached the model in this conversation. Never cleared. | `src/blackrose/turns.py::TurnRunner._install_outbound_policy`; `src/blackrose/chat.py::ChatController.expose_local_data`. Migration 8 backfills 1 for any conversation with an assistant message. |
| `OutboundPolicy.private` | `__init__` | `bool` | This reply's privacy latch. | `__init__` (from `_install_outbound_policy`), `mark_private` only. |
| `_sites` | `__init__` | `dict[str, bool]`, borrowed | Conversation site grants: host → granted after local data was in use. | `authorize` (its `SITE` branch) only inside the policy; `_install_outbound_policy` creates and `ChatController._delete_current_conversation` drops the map. |
| `_allowed` | `__init__` | `dict[request_key, str]` | Exact GETs (scheme, host, port, path, query) this reply may read, and the basis: `your message`, `search result`, `link on a page…` (`PAGE_LINK`), `approved by you`. | `__init__`; `note_results`; `note_links`; `_record`. |
| `_hops` | `__init__` | `dict[request_key, int]` | Link depth of a `PAGE_LINK` entry; absence means 0 (typed/search/approved root). | `note_links`; `_record` pops. |
| `_queries` | `__init__` | `set[str]` | Exact search strings approved this reply. | `_record`. |
| `_images` | `__init__` | `set[(prompt, aspect_ratio, attachment ids)]` | Exact remote image identities approved this reply. | `authorize_image`. |
| `_prompts` | `__init__` | `int`, 0…`MAX_PROMPTS` (8) | Approval questions shown this reply; a batch costs one. | `preauthorize`; `_ask`. |
| `_declined` | `__init__` | `bool` | A request was refused in this reply. | `preauthorize`; `_ask`. Never reset. |
| `_refused` | `__init__` | `set[(kind, identity)]` | Batch rows the user left unchecked. Identity is `src/blackrose/outbound.py::refusal_key`. | `preauthorize`. |
| `_approve` | `__init__` | `Approver \| None` | The native sheet, or nothing. | `__init__` only. |
| `_trusted` | `__init__` | `tuple[str]` | Normalized `trusted_hosts` snapshot for this reply. | `__init__` only. |
| `ChatController.site_grants` | `src/blackrose/chat.py::ChatController.__init__` | `dict[int, dict[str, bool]]` | Per-conversation grants for the app's lifetime. | `_install_outbound_policy` (`setdefault`), `OutboundPolicy.authorize` through the borrowed map, `ChatController._delete_current_conversation` (pop). |

## States

Five orthogonal machines. Each is per conversation or per reply as stated.

**S1 — privacy latch** (per conversation; `P` = `policy.private`, `D` = the
durable row). Mutually exclusive and monotone:

| State | Predicate |
| --- | --- |
| `CLEAR` | `P == False` and `D == 0` |
| `PRIVATE` | `P == True`; in production `D == 1` because both writers write `D` first (`src/blackrose/chat.py::ChatController.expose_local_data`, `src/blackrose/turns.py::TurnRunner._install_outbound_policy`). A direct `mark_private()` call leaves `D` unchanged (H8), which is why that is not a production entry point. |
| `DURABLE` | `D == 1`; every later reply is installed with `P == True` (`src/blackrose/turns.py::TurnRunner._private_context`, `src/blackrose/turns.py::TurnRunner._install_outbound_policy`) |

`DURABLE` is absorbing for the conversation; `CLEAR` is reachable only for a
conversation that has never exposed local data (a new conversation id is a new
machine instance).

**S2 — site grant** (per `(conversation, host)`):

| State | Predicate | Effect while `CLEAR` | Effect while `PRIVATE` |
| --- | --- | --- | --- |
| `ABSENT` | `host not in _sites` | asks | asks |
| `PRE_LOCAL` | `_sites[host] is False` | allows (`site_allows`) | asks |
| `POST_LOCAL` | `_sites[host] is True` | allows | allows |

`PRE_LOCAL → POST_LOCAL` is the only write (`authorize`, its `SITE` branch); nothing writes backwards
(see I8).

**S3 — approval gate** (per reply). Mutually exclusive by precedence
`CLOSED > SATURATED > OPEN`, matching the check order in `_ask`:

| State | Predicate | New request that needs approval |
| --- | --- | --- |
| `CLOSED` | `_declined == True` | denied `approval_declined` without a sheet (`_ask`) |
| `SATURATED` | `not _declined and _prompts >= MAX_PROMPTS` | denied `approval_limit` without a sheet (`_ask`) |
| `OPEN` | neither | asks, then `OPEN` or `CLOSED` |

**S4 — batch question** (per model round, derived):

| State | Predicate |
| --- | --- |
| `NO_BATCH` | fewer than 2 pending, no approver, `CLOSED`, or `SATURATED` (`preauthorize`'s early return) — each call is asked alone |
| `ASKED_ONCE` | at least 2 pending, an approver, `OPEN`; up to `MAX_BATCH` rows, site not offered (rows rebuilt with `site=''`), one prompt (all in `preauthorize`) |
| `ANSWERED` | each row recorded as approved (`_record`) or refused + `CLOSED` (`preauthorize`'s answer loop) |

**S5 — remote image identity** (per `(prompt, aspect_ratio, references)`):

| State | Predicate |
| --- | --- |
| `AUTOMATIC` | `not private and not declined` (`authorize_image`) |
| `PENDING` | otherwise, identity not in `_images` (`authorize_image`); T4 decides |
| `APPROVED` | identity in `_images` (`authorize_image`) — reachable only while `private` (see I4 note) |

## Events

1. Reply install: `_install_outbound_policy` computes `private` from remembered
   notes or `_private_context` (durable row, attachments, file/directory refs,
   local evidence) and writes the row when private
   (`src/blackrose/turns.py::TurnRunner._private_context`,
   `src/blackrose/turns.py::TurnRunner._install_outbound_policy`).
2. `expose_local_data()`: a `knowledge_*` call
   (`src/blackrose/chat.py::ChatController._call_tool`) or a `file_*` call other
   than `directory_create`/`document_create`
   (`src/blackrose/chat.py::ChatController.file_tool`) or a skill script run,
   before the script starts (`src/blackrose/chat.py::ChatController.run_skill_script`) → durable write, then
   `mark_private` (`src/blackrose/chat.py::ChatController.expose_local_data`).
3. `authorize(name, arguments)` for `fetch_content` or `web_search`
   (`src/blackrose/chat.py::ChatController._outbound`).
4. `preauthorize(calls)` once per round, before any call runs
   (`src/blackrose/turns.py::TurnRunner._run_tools`,
   `src/blackrose/tool_execution.py::ToolExecutor.preauthorize`).
5. `note_results` after a search; `note_links` after a fetch (both in
   `src/blackrose/chat.py::ChatController._outbound`).
6. `authorize_image` for a paid remote image job
   (`src/blackrose/chat.py::ChatController._generate_image` →
   `src/blackrose/chat.py::ChatController._authorize_remote_image`).
7. The approver's answer (`True`, `SITE`, a tuple for a batch, anything else),
   an absent approver, or cancellation of the reply task while the sheet is
   open.
8. Reply end clears the policy (`src/blackrose/turns.py::TurnRunner._run`'s
   `finally`); conversation deletion and app exit drop the site-grant map
   (`src/blackrose/chat.py::ChatController._delete_current_conversation`).
9. Upgrade/startup: Migration 8's backfill; every reply re-reads the durable
   row (`src/blackrose/turns.py::TurnRunner._private_context`).

## Transition table

Policy methods are `src/blackrose/outbound.py::OutboundPolicy` methods; a
guard or effect names the statement inside the method it occurs in.

### T1 — privacy latch (S1)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `CLEAR` | reply install, local context present | `memory_notes` or `_private_context` true (`src/blackrose/turns.py::TurnRunner._install_outbound_policy`) | `UPDATE … = 1`, then policy with `private=True` | `PRIVATE`, `DURABLE` |
| `CLEAR`/`PRIVATE` | `expose_local_data` | policy exists and already private (`src/blackrose/chat.py::ChatController.expose_local_data`'s early return) | none (the row was written at install) | unchanged |
| `CLEAR` within a reply | `expose_local_data` | `private` false | row `= 1`, then `mark_private` (`src/blackrose/chat.py::ChatController.expose_local_data`) | `PRIVATE`, `DURABLE` |
| `CLEAR` | `mark_private()` called directly | — (`src/blackrose/outbound.py::OutboundPolicy.mark_private`) | `private = True` only; no durable write | `PRIVATE` (reply-only; see H8) |
| `PRIVATE` | new reply | row `= 1` (`src/blackrose/turns.py::TurnRunner._private_context`) | new policy `private=True` | `PRIVATE` |
| `DURABLE` | conversation deleted, same title recreated | new id | new row `= 0` | `CLEAR` (new instance) |
| any | anything else | — | no transition to `CLEAR` exists | unchanged |

### T2 — `fetch_content` basis selection (inside `_check`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| proposed GET | always | `validate_fetch_url` raises | `ValueError`; no sheet and no request | denied locally |
| proposed GET | `_allowed[key]` is `your message`/`search result`/`approved by you` | the `_allowed` lookup | return basis, no sheet | authorized |
| proposed GET | `_allowed[key]` is `PAGE_LINK` | `private` false | return `PAGE_LINK`, no sheet | authorized |
| proposed GET | `_allowed[key]` is `PAGE_LINK` | `private` true | fall through to the gates below | asks |
| proposed GET | site grant covers it | `site_allows` (`_sites[host]` true, or false while not private) | return `SITE_GRANT` | authorized |
| proposed GET | trusted site covers it | `trusted_allows` (https:443; no query when private) | return `TRUSTED_SITE` | authorized |
| proposed GET | none of the above | — | `OutboundRequest` with `site=site_of(url)` | S3 gate |

### T3 — `web_search` basis selection (inside `_check`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| proposed query | always | `not private` | `automatic` | authorized |
| proposed query | private | `query in _queries` | `approved by you` | authorized |
| proposed query | private | otherwise | request | S3 gate |

### T4 — ask gate (`_ask`) and recording (`authorize`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| needs approval | any | no approver | deny `approval_unavailable`; budget untouched | denied |
| needs approval | any | identity in `_refused` | deny `approval_declined` | denied |
| needs approval | any | `_declined` | deny `approval_declined` | denied |
| needs approval | any | `_prompts >= 8` | deny `approval_limit`; no sheet | denied |
| needs approval | any | otherwise | `_prompts += 1`, then await the sheet | `ASKED` |
| `ASKED` | answer `True` | the answer check in `_ask` | `_record` (called from `authorize`) | authorized |
| `ASKED` | answer `SITE` | request offers a site | `_record`; `_sites[site] = private or old` (`authorize`) | authorized + T7 |
| `ASKED` | answer `SITE`, no site offered | the answer check in `_ask` | `_declined = True`, deny `approval_declined` | `CLOSED` |
| `ASKED` | anything else (False, `None`, `'yes'`, wrong-length tuple) | the answer check in `_ask` | `_declined = True`, deny `approval_declined` | `CLOSED` |
| `ASKED` | cancellation while open | — | `CancelledError` propagates; no `_record`, nothing sent | reply ends |
| authorized | — | `_refused` not matched (`authorize`) | return the basis without asking | unchanged |

`_record` stores a fetch under its `request_key` and drops its hop count; a
search under its exact string; `authorize_image` stores an image identity.

### T5 — batch question (`preauthorize`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| calls collected | `_check` per web call | `KeyError/TypeError/ValueError` | skip; the call reports on its own | — |
| ≥2 pending | batch eligible | no approver, `CLOSED`, or `SATURATED` | no batch; each call hits T4 | `NO_BATCH` |
| ≥2 pending | batch eligible | up to `MAX_BATCH` | one sheet, `site=''` on every row, one prompt | `ASKED_ONCE` |
| `ASKED_ONCE` | answer `True` | the `answers` expression | every row approved | `ANSWERED` |
| `ASKED_ONCE` | tuple/list of the right length | the `answers` expression | each row `is True` → approved, else refused + `CLOSED` | `ANSWERED` |
| `ASKED_ONCE` | anything else | the `answers` expression | every row refused + `CLOSED` | `ANSWERED` |
| row approved | later `authorize` | identity not in `_refused` | runs on its recorded basis, even though `_declined` is true | authorized |
| row unchecked | later `authorize`, even if the round has since granted it | `authorize` or `_ask` (`_refused` membership) | denied `approval_declined` | refused |
| row beyond `MAX_BATCH` | later `authorize` | — | asked alone through T4 | S3 gate |

### T6 — remote image (`authorize_image`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| job | always | `not private and not declined` | `automatic` | authorized |
| job | otherwise | identity in `_images` | `approved by you` | authorized |
| job | otherwise | not in `_images` | `_ask` (T4), then add identity | `APPROVED` or denied |
| `APPROVED` | same identity again this reply | identity in `_images` | no sheet | authorized |

### T7 — site grant (`authorize`, `site_allows`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT`/`PRE_LOCAL` | single answer `SITE` while `private` | request.site set | `_sites[site] = True` | `POST_LOCAL` |
| `ABSENT`/`PRE_LOCAL` | single answer `SITE` while not private | request.site set | `_sites[site] = False` | `PRE_LOCAL` |
| `POST_LOCAL` | read | `site_allows` | allowed in both privacy states | `POST_LOCAL` |
| any | batch answer `SITE` | rows carry `site=''` | refused; map untouched | unchanged |
| any | conversation deleted | `src/blackrose/chat.py::ChatController._delete_current_conversation` | map dropped | `ABSENT` (all hosts) |

### T8 — link depth (`note_links`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| page read under any basis | its links | `hops = _hops.get(page,0)+1 > MAX_LINK_HOPS` | stop; no link recorded | unchanged |
| page read | links | `hops <= MAX_LINK_HOPS` and key not already in `_allowed` | `_allowed[key] = PAGE_LINK`, `_hops[key] = hops` | `PAGE_LINK` at depth `hops` |
| `PAGE_LINK` | approved by the user | `_record` | basis becomes `approved by you`; hop count dropped | depth 0 |
| `PAGE_LINK` | next reply | — | new policy; links are not carried over | `ABSENT` |

## Invariants

1. **The durable mark is monotone.** After `local_data_exposed = 1`, no code
   writes 0; the only writes are the Migration 8 default and backfill and the
   two `UPDATE … = 1` paths
   (`src/blackrose/storage/repository.py::Repository.mark_local_data_exposed`, called
   from `src/blackrose/turns.py::TurnRunner._install_outbound_policy` and
   `src/blackrose/chat.py::ChatController.expose_local_data`). Pinned by
   `tests/test_sm_13_outbound_policy.py::RepositoryLatchTests.test_the_mark_is_idempotent_and_durable` and
   `tests/test_storage.py::test_chats_with_replies_start_guarded_after_the_exposure_migration`.
2. **`private` is monotone within a policy.** The only writes are
   `src/blackrose/outbound.py::OutboundPolicy.__init__` and
   `src/blackrose/outbound.py::OutboundPolicy.mark_private`; the latter only ever
   sets `True`.
   `PrivacyLatchTests.test_a_link_granted_before_local_data_is_rechecked_after_it_is_used`
   calls `mark_private` twice and asserts the latch stays set.
3. **Every privacy-filtered grant map is filtered at each read.**
   `_sites` is read only in `OutboundPolicy.site_allows`, guarded in the same
   expression by `not self.private`; its callers are `_check` and
   `stays_allowed`, and both re-evaluate the flag on every call. `_queries` is
   read only in `_check`'s `web_search` branch, after its `not self.private`
   return. `_images` is read only in `authorize_image`, after its
   `not (private or declined)` return. `_allowed` is read for authorization only
   in `_check`'s `fetch_content` branch; a `PAGE_LINK` basis is rejected there
   while private. `_hops` is read only in `note_links` and decides whether a new
   link entry is admitted; it never authorizes. `note_links` reads `_allowed`
   only to avoid overwriting an entry. Pinned by
   `tests/test_sm_13_outbound_policy.py::PrivacyLatchTests.test_a_site_grant_recorded_before_local_data_stops_applying_when_it_is_used`,
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
   in `_refused` is denied on the automatic path (`authorize`) and on the
   ask path (`_ask`); the identity for a GET is its `request_key`
   (`src/blackrose/outbound.py::refusal_key`), so a fragment, host case or explicit
   default port cannot re-open it. `_declined` then blocks every other new
   question (`_ask`). Pinned by the four `BatchRefusalTests`.
6. **A batch never widens to a site.** Rows are rebuilt with `site=''`
   (`preauthorize`) and `_ask` accepts `SITE` only when the request offers one; a
   `SITE` answer to a batch therefore refuses every row (`preauthorize`'s
   `answers` expression). Pinned by
   `BatchRefusalTests.test_a_batch_answer_of_site_grants_nothing_and_declines_every_row`.
7. **A decline does not cancel an approval the user gave in the same sheet.**
   Approved rows are recorded before any call runs, so they run on their basis
   even with `_declined` true; only *new* questions are blocked. Pinned by
   `BatchRefusalTests.test_an_unchecked_request_stays_refused_when_the_round_grants_it`.
8. **At most `MAX_PROMPTS` sheets per reply, counted when shown.** Both paths
   increment before awaiting the approver (`preauthorize`, `_ask`) and both refuse
   at `>= MAX_PROMPTS` before incrementing. A batch costs one
   prompt for up to `MAX_BATCH` rows. Pinned by
   `PromptBudgetTests.test_a_full_budget_refuses_the_next_request_without_showing_it`
   and `tests/test_security_boundaries.py::test_a_batch_counts_once_toward_the_prompt_limit`.
9. **A site grant never downgrades.** `authorize`'s `SITE` branch writes `True` only when `private`
   was true at record time (`PRE_LOCAL`/`POST_LOCAL` distinction); a `True`
   entry always satisfies `site_allows`, so it is never asked about again and
   cannot be re-answered as `False`. Pinned by
   `PrivacyLatchTests.test_a_site_answer_before_local_data_is_recorded_as_pre_local`
   and `tests/test_security_boundaries.py::test_grant_recorded_after_local_data_is_covered_without_asking_when_not_private`.
10. **Redirect hops are bounded and re-checked where a grant authorized the
    read.** `PageFetcher` validates every target and stops after
    `MAX_REDIRECTS = 5` (`src/blackrose/web/fetcher.py::PageFetcher._fetch_once`,
    `src/blackrose/web/constants.py::MAX_REDIRECTS`); a read authorized by a site
    grant or trusted site passes `stays_allowed` on every hop
    (`src/blackrose/chat.py::ChatController._outbound`), which re-applies the
    current `private` state.
11. **Link depth is at most `MAX_LINK_HOPS = 2` from a non-link grant, and an
    approval restarts the chain.** `note_links` refuses deeper entries;
    `_record` drops the hop count. Pinned by `LinkHopTests`.
12. **Nothing is sent that SSRF validation would refuse, and no sheet is shown
    for it.** `_check`'s `validate_fetch_url` runs first in both `authorize` and
    `preauthorize`; `preauthorize` treats the `ValueError` as "fails on its own".
    Pinned by `tests/test_security_boundaries.py::test_approval_never_bypasses_public_destination_checks`.
13. **Decisions are logged without payloads.** `OutboundPolicy._deny` logs kind,
    code and host only; `_ask`'s approval log line does the same. Pinned by
    `tests/test_security_boundaries.py::test_refusals_are_logged_without_request_contents`.

## Hazards and open questions

1. **defect (found, fixed) — the three `_refused` sites in `src/blackrose/outbound.py::OutboundPolicy.authorize`, `preauthorize` and `_ask` (pre-fix).**
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

2. **fragile — `OutboundPolicy._sites` with
   `src/blackrose/turns.py::TurnRunner._install_outbound_policy` and
   `ChatController.site_grants`.** The policy borrows the conversation's mutable
   dict and mutates it (`authorize`). I9 depends on that being the only writer and
   on no policy outliving its reply (`src/blackrose/turns.py::TurnRunner._run`'s
   `finally`). I found no path that keeps a policy past the turn (`grep`
   for `.outbound` shows only `chat.py`, `turns.py` and `tool_host.py`, all
   access-by-reference at call time), but a future caller that stores one could
   write `True` into a later turn's grants. Would confirm: a retained policy
   reference across the reply's `finally`. No test; the window is unreachable today.

3. **documentation-gap — `src/blackrose/chat.py::ChatController.__init__`, `src/blackrose/turns.py::TurnRunner._install_outbound_policy`.** `site_grants` is a
   public controller attribute with no validation, and `OutboundPolicy` trusts
   whatever dict it is given. It is not reachable from the model, a page, the
   MCP surface or the GUI bridge (no reference outside `chat.py`, `turns.py` and
   tests), so forging a grant requires arbitrary code in the app process, which
   already owns the browser and the database. Severity: `none-found` for the
   stated threat model; recorded because the answer to "does `site_grants`
   expose a grant-forging surface?" is "only to in-process Python".

4. **documentation-gap — `src/blackrose/outbound.py::OutboundPolicy.trusted_allows`.** Once private, a trusted site
   covers only addresses without a query. A model-written *path* on a trusted
   host (`https://docs.example/<private text>`) still leaves without a sheet;
   only the query is treated as likely to be logged by the site's own search
   and analytics. This is narrower than a `POST_LOCAL` site grant and the
   limitation is stated in the module docstring, so it looks deliberate.
   Would confirm: an explicit statement that trusted hosts are trusted with
   path-encoded data but not query-encoded data.

5. **fragile — `src/blackrose/outbound.py::OutboundPolicy.note_links`.** `note_links` records `PAGE_LINK` entries
   even while `private`, where they can never authorize; `_allowed` is
   reply-scoped and its size is bounded by link depth (2) but not by page
   degree. Cost is memory only; no test pins a bound. Would confirm: a reply
   with a page declaring thousands of links (bounded in practice by the fetch
   response size limit).

6. **none-found — redirects.** Hops are bounded (I10) and grant-authorized reads
   re-check each hop. Exactly-authorized reads do not pass `redirect_allowed`
   (`src/blackrose/chat.py::ChatController._outbound`'s plain-fetch branch), which
   is intended: there is no grant to widen, and the redirect target is chosen by
   the server, not by the model. Note that `note_links` is called with the
   *requested* URL (`src/blackrose/chat.py::ChatController._outbound`) even
   after redirects, so a redirect chain does not consume link depth; depth per
   URL is still bounded.

7. **none-found — `_declined`.** No write sets it false (`__init__` initializes
   it; `preauthorize` and `_ask` only set it true), so it is terminal for the
   reply. A new message starts a new policy
   (`src/blackrose/turns.py::TurnRunner._install_outbound_policy`), which is the
   documented "until the next message" scope. Pinned by
   `tests/test_sm_13_outbound_policy.py::BatchRefusalTests.test_a_decline_blocks_later_questions_without_asking_again`.

8. **documentation-gap — `src/blackrose/outbound.py::OutboundPolicy.mark_private`.**
   `mark_private()` performs no durable write. Durability rests on both production
   callers ordering the row first
   (`src/blackrose/chat.py::ChatController.expose_local_data` writes the row before
   calling it; `src/blackrose/turns.py::TurnRunner._install_outbound_policy` writes
   it before construction). A
   future direct caller would get a reply-only latch that a restart clears.
   Would confirm: any call site of `mark_private` outside `chat.py`.

9. **none-found — prompt budget ordering.** The budget is consumed when the
   sheet is shown, not when it is answered, so a cancelled or failed reply can
   leave it spent; the policy is discarded with the reply
   (`src/blackrose/turns.py::TurnRunner._run`'s `finally`), so
   this has no observable effect. `tests/test_security_boundaries.py::test_stop_while_approval_is_pending_sends_nothing`
   shows nothing is sent when a sheet is abandoned.

## Formalization decisions

**One source change** in `outbound.py` (the one file this investigation owned):

- Added `src/blackrose/outbound.py::refusal_key` and used it at the three
  `_refused` sites (`authorize`, `preauthorize`, `_ask`), with the field
  annotated `set[tuple[str, object]]` in `OutboundPolicy.__init__`. A fetch refusal is now
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
  (`src/blackrose/storage/repository.py::Repository.mark_local_data_exposed`); a
  `rowcount` check would only catch a deleted conversation, and deletion is
  refused while a reply is generating
  (`src/blackrose/chat.py::ChatController._delete_current_conversation`), so no
  obligation is stranded.
- `expose_local_data`'s early return
  (`src/blackrose/chat.py::ChatController.expose_local_data`) is sound because
  every policy with `private=True` was installed by a path that wrote the row
  (`src/blackrose/turns.py::TurnRunner._install_outbound_policy`). Changing it
  would add a redundant write.
- Migration 8's backfill (`src/blackrose/storage/migrations.py::MIGRATIONS`) is
  deliberately conservative and needs no change.
- Follow-up worth considering, not a patch: make `site_grants` private to
  `ChatController` (or hand the policy a copy plus a write callback) so H2/H3
  are structural rather than conventional. It is wider than this investigation
  and touches `chat.py`/`turns.py`, so it is recorded here only.

## Verification

All commands from the repository root, offline; the runner gives each its own
temporary XDG tree.

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_13_outbound_policy.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_security_boundaries.py
.venv/bin/python scripts/check_unit_tests.py -p test_capability_disclosure.py
.venv/bin/python scripts/check_unit_tests.py -p test_tool_handlers.py
.venv/bin/python scripts/check_unit_tests.py -p test_memory.py
.venv/bin/python scripts/check_unit_tests.py -p test_storage.py
.venv/bin/python scripts/check_unit_tests.py -p test_saved_compatibility.py
.venv/bin/python scripts/check_unit_tests.py -p test_skill_authority.py
.venv/bin/python scripts/check_unit_tests.py -p test_image_generation.py
.venv/bin/python scripts/check_unit_tests.py -p test_turns.py
```

The first suite pins this machine's guards and refusals;
`tests/test_security_boundaries.py` is the maintained outbound suite, and the
rest cover the callers that install, expose and consume the policy.

Regression evidence for H1: with `refusal_key` replaced by the pre-fix raw tuple
at the three `_refused` sites,
`tests/test_sm_13_outbound_policy.py::BatchRefusalTests.test_the_refusal_covers_every_spelling_of_the_same_request`
fails with `AssertionError: OutboundDenied not raised` for `variant='…#part'`
while the suite's other tests pass; with the fix, all pass.
