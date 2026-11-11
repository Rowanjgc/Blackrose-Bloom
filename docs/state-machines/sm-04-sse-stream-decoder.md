# SM-04: SSE stream decoder, block accumulator and line framing

## Scope and ownership

| Surface | Files | Owner |
| --- | --- | --- |
| Line framing | `src/blackrose/backend/stream_limits.py:86-149` (`sse_lines`) | The provider client that opened the HTTP response |
| Wire validation and resource bounds | `src/blackrose/backend/stream_limits.py:13-61` (`StreamLimitExceeded`, `StreamBounds`, `StreamBudget`), `:64-83` (`bounded_choices`) | The reply (one budget per reply, passed to every provider request) |
| Protocol decoding | `src/blackrose/backend/opencode_protocols.py:145-300` (`StreamDecoder`) | The OpenCode Go request that created it (`src/blackrose/backend/opencode.py:125-143`) |
| Local wire decoding | `src/blackrose/backend/router.py:173-228` (`RouterClient.stream`) | The local router request |
| Downstream call/finish reconciliation | `src/blackrose/turns.py:115-183`, `:616-663`, `:691-711` | The turn (`TurnRunner`) |

Ownership notes:

- Every object here is **non-durable and task-local**. `sse_lines` framing
  state and `StreamDecoder` fields exist only for the lifetime of one HTTP
  response inside one asyncio task. No database column, JSON blob or file
  represents them, and none is shared between tasks.
- `StreamBudget` is the one object with a wider lifetime: it is constructed once
  per reply (`src/blackrose/turns.py:527`), stored on `ReplyContext.budget`
  (`turns.py:102-110`) and passed to every provider request in that reply
  (`turns.py:626-628`), including the repair substream
  (`src/blackrose/repairs.py:6-25`). Its counters are monotonic by design
  (`stream_limits.py:27`).
- The only *persisted* consequence is `_provider_content`: `StreamDecoder.end`
  returns `{'protocol', 'content'}` (`opencode_protocols.py:161-162`), the turn
  stores it in `RoundOutput.provider_content` (`turns.py:636-637`) and
  `Providers.continuation` copies it onto the assistant wire message
  (`backend/providers.py:74-80`), where `prepare` replays it as native provider
  content (`opencode_protocols.py:64-65`, `:89-90`, `:123-124`).
- All mutation happens on the event loop. The only ordering points that yield to
  other tasks are `asyncio.sleep(0)` every 32 lines/chunks
  (`stream_limits.py:131-133`, `:82-83`) and the `await` on the next chunk/choice.
  There is no lock and none is needed.
- Cancellation (Stop) propagates into the async generator; `async with
  self.client.stream(...)` (`opencode.py:129`) and `aclosing`
  (`stream_limits.py:68`) close the response. The decoder is then discarded with
  `completed=False`; nothing has to be released.
- `StreamDecoder` is also subclassed for acceptance evidence
  (`scripts/accept_opencode_go.py:457-489`), which reads only `feed`,
  `completed` and `native`.

## State variables

### `sse_lines` (frames one HTTP response)

| Variable | Location | Type | Meaning | Writers |
| --- | --- | --- | --- | --- |
| `pending` | `stream_limits.py:89` | `bytearray` | Bytes of the current line that have not seen `\r`/`\n` yet | `:119` append, `:123` clear |
| `first_line` | `stream_limits.py:90` | `bool` | The next yielded line is still the first one, so a leading BOM is removed | `:129`, `:148` |
| `skip_lf` | `stream_limits.py:91` | `bool` | The previous chunk ended exactly after `\r`; a `\n` opening the next non-empty chunk belongs to that CRLF | `:109`, `:140` |
| `line_count` | `stream_limits.py:92` | `int` | Cooperative-yield counter | `:131` |
| `chunk`, `start`, `newline` | `:97`, `:105`, `:112` | locals | Current chunk and scan position | loop locals |

### `StreamBudget` (one reply)

| Variable | Location | Type | Meaning | Writers |
| --- | --- | --- | --- | --- |
| `bounds` | `stream_limits.py:30` | `StreamBounds` | Frozen limits (`:17-23`) | constructor |
| `deadline` | `stream_limits.py:31` | `float` | Absolute loop time; `now + duration_seconds`, fixed at construction | constructor |
| `response_bytes` | `stream_limits.py:32` | `int` | Raw bytes read off the wire, before framing | `:44` |
| `reply_bytes` | `stream_limits.py:33` | `int` | UTF-8 bytes of visible answer text, tool names/ids/arguments | `:56` (from `turns.py:148-153`, `repairs.py:16-24`) |
| `reasoning_bytes` | `stream_limits.py:34` | `int` | UTF-8 bytes of reasoning text | `:53` |

### `StreamDecoder` (one provider response)

| Variable | Location | Type | Meaning | Writers |
| --- | --- | --- | --- | --- |
| `protocol` | `opencode_protocols.py:147` | `'chat' \| 'messages' \| 'responses' \| 'google'` | Selects the branch in `feed` | constructor (`opencode.py:119`) |
| `completed` | `:148` | `bool` | A finish reason has been recorded; the backend boundary requires it (`opencode.py:142-143`) | `feed` chat (`:176`), `end` (`:160`) |
| `blocks` | `:149` | `dict[int, dict]` | **Open** Anthropic content blocks by index (after this change) | `:226` add, `:251` pop |
| `arguments` | `:150` | `dict[int, str]` | Partial JSON of an open `tool_use` block | `:228` init, `:246` append, `:253` pop |
| `native` | `:151` | `list[dict]` | Provider-native history for the next request | `:254` (messages), `:285` (responses), `:191` (google) |
| `finish` | `:152` | `str \| None` | Normalized stop reason; **unused on the `chat` branch** | `:265` |
| `calls` | `:153` | `int` | Count of tool calls opened (`responses`) or emitted (`google`) | `:278`, `:198` |
| `usage` | `:154` | `dict` | Normalized `prompt_tokens`/`completion_tokens` | `:219-220`, `:257`, `:186-187`, `:289-290` |

### `RouterClient.stream` (one local response)

| Variable | Location | Type | Meaning | Writers |
| --- | --- | --- | --- | --- |
| `completed` | `router.py:184` | `bool` | A choice carried `finish_reason` | `:226` |
| `saw_calls` | `router.py:185` | `bool` | A tool-call delta was seen anywhere in the stream | `:219` |

## States

### F: line framing (`sse_lines`)

| State | Predicate |
| --- | --- |
| `START` | `first_line ∧ ¬skip_lf ∧ pending == b''` |
| `FIRST_PARTIAL` | `first_line ∧ pending != b''` (BOM and/or line bytes buffered) |
| `BODY` | `¬first_line ∧ ¬skip_lf` |
| `CR_BOUNDARY` | `¬first_line ∧ skip_lf` (a `\r` ended the previous chunk; `pending` is empty) |
| `EXHAUSTED` | terminal: upstream raised `StopAsyncIteration`; the generator returns after the final partial line |

`FIRST_PARTIAL` and `BODY` differ only in whether a BOM is stripped from the next
line, so they are the same framing state with different first-line policy.

### S: stream lifecycle (`StreamDecoder`, `RouterClient.stream`)

| State | Predicate |
| --- | --- |
| `OPEN` | `¬completed ∧ finish is None` |
| `STOP_REASON` | `finish is not None ∧ ¬completed` (messages only; responses/google record `calls` instead and chat has no `finish`) |
| `COMPLETED` | `completed` — terminal for the backend boundary, **not** terminal for `feed` |

### B: block lifecycle (messages only)

| State | Predicate |
| --- | --- |
| `ABSENT` | `index ∉ blocks` (never opened, or opened and already closed) |
| `OPEN_TEXT` / `OPEN_THINKING` / `OPEN_TOOL_USE` | `blocks[index]['type']`; `OPEN_TOOL_USE` additionally satisfies `index ∈ arguments` |
| `CLOSED` | `index ∉ blocks` and a block with that index is already in `native` — observationally identical to `ABSENT`, which is why index reuse after a stop is indistinguishable from a first use |

There is no separate `CLOSED` state variable; `CLOSED` is derived from `native`.
That is the reason the pre-existing duplicate-stop defect was silent (H1).

### C: call accumulation (`turns.py`, downstream of every decoder)

| State | Predicate |
| --- | --- |
| `NO_CALL` | `RoundOutput.calls == {}` |
| `CALL_OPEN` | an entry exists whose `function.arguments` is still growing |
| `CALL_READY` | the stream ended; `require_normal_finish` compares `bool(calls)` with the reason (`turns.py:179-181`) |

## Events

| Event | Source | Enters at |
| --- | --- | --- |
| Byte chunk from the wire | `response.aiter_bytes()` | `sse_lines` (`stream_limits.py:97`) |
| Upstream exhaustion | `StopAsyncIteration` | `sse_lines:98`, `bounded_choices:74` |
| Deadline exceeded | `asyncio.timeout_at` / `check_time` | `sse_lines:94-103`, `turns.py:629` |
| Budget exceeded | byte counters | `stream_limits.py:42-61` |
| SSE line | framing output | `opencode.py:132-141`, `router.py:204-207` |
| `data:` payload terminated by a blank line | blank SSE line | `opencode.py:135-138` (feeds the decoder) |
| `[DONE]` | literal payload | `StreamDecoder.feed:165`, `router.py:208` |
| JSON packet | `json.loads` | `feed:169`, branch dispatch `:173-183` |
| Provider error packet | `error` / `type == 'error'` | `feed:170-172`, `router.py:213-214` |
| `end(reason)` | messages `message_stop`, responses `completed`/`incomplete`, google `finishReason` | `:156`, `:207`, `:267`, `:295-297` |
| EOF of the HTTP body | iterator exhaustion | `opencode.py:139-143`, `router.py:227-228` |
| Cancellation / Stop | task cancel | generator close |

## Transition table

### F: `sse_lines`

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `START`/`BODY` | chunk | no `\r`/`\n` in chunk | append chunk to `pending` (`:119`) | `FIRST_PARTIAL`/`BODY` |
| `FIRST_PARTIAL`/`BODY` | chunk | terminator found | append head, yield assembled line (`:119-130`) | `BODY` |
| any | chunk | terminator is `\n` | `start = newline + 1` (`:135`) | `BODY` |
| any | chunk | terminator is `\r`, next byte in chunk is `\n` | consume both (`:137-138`) | `BODY` |
| any | chunk | terminator is `\r`, more bytes follow in chunk | `start = newline + 1` (`:135-136`) | `BODY` |
| any | chunk | terminator is `\r` and it is the last byte | `skip_lf = True` (`:139-140`) | `CR_BOUNDARY` |
| `CR_BOUNDARY` | non-empty chunk starting with `\n` | `:106-108` | `start = 1`; swallowed LF is **not** a line | `BODY` |
| `CR_BOUNDARY` | non-empty chunk not starting with `\n` | `:109` | `start = 0` | `BODY` |
| `CR_BOUNDARY` | empty chunk | `if skip_lf and chunk` is false (`:106`) | `skip_lf` preserved | `CR_BOUNDARY` |
| any | chunk | `len(pending) + segment > sse_line_bytes` | raise `StreamLimitExceeded` **before** appending (`:115-118`) | unchanged |
| any | chunk | `response_bytes + len(chunk) > response_bytes` | raise (`:104` → `:42-48`) | unchanged |
| any | top of loop | `now >= deadline` | raise (`:94` → `:36-40`) | unchanged |
| any | await next chunk | `asyncio.timeout_at` fires | raise `StreamLimitExceeded` (`:100-103`) | unchanged |
| `FIRST_PARTIAL`/`BODY` | EOF | `pending != b''` | yield `pending` before returning (`:145-149`) | `EXHAUSTED` |
| `BODY` | EOF | `pending == b''` | return (`:98-99`) | `EXHAUSTED` |
| `CR_BOUNDARY` | EOF | — | return; `skip_lf` is dropped (`:98-99`) | `EXHAUSTED` |

The first yielded line additionally loses one leading `\ufeff` (`:127-129`), and a
final partial line loses it too (`:147-148`). Lines are decoded per assembled
line, never per chunk, so a multi-byte character or BOM split across chunks is
assembled before decoding.

### S/B: `StreamDecoder.feed` — dispatch

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| any | `data == '[DONE]'` | `completed` | return `[]` (`:165-168`) | unchanged |
| any | `data == '[DONE]'` | `¬completed` | raise "ended without a finish reason" (`:166-167`) | unchanged |
| any | packet with `error`/`type == 'error'` | — | raise generic error (`:170-172`, deliberately no provider text) | unchanged |
| any | any packet | `protocol == 'chat'` | see below (`:173-179`) | — |
| any | any packet | `protocol == 'messages'` | `anthropic()` (`:180-181`) | — |
| any | any packet | `protocol == 'responses'` | `responses()` (`:182-183`) | — |
| any | any packet | otherwise | google branch (`:184-210`) | — |

### `chat` branch

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `OPEN`/`STOP_REASON` | packet with choices | — | yield every choice verbatim (`:174-175`, `:179`) | unchanged |
| `OPEN` | any choice has `finish_reason` | — | `completed = True` (`:175-176`) | `COMPLETED` |
| `COMPLETED` | another choice with a finish reason | no guard | `completed` stays true, choice yielded | `COMPLETED` |
| `COMPLETED` | content/reasoning/tool delta | no decoder guard | yielded; the turn rejects it (`turns.py:632-635`) | `COMPLETED` |
| any | packet carries `usage` | — | append a `{'delta': {}, '_usage': …}` pseudo-choice after the real choices (`:177-178`) | unchanged |
| `COMPLETED` | EOF | — | `opencode.py:142-143` sees `completed` and returns | terminal |

`finish` is never written on this branch: `end()` is unreachable for `chat`.

### `messages` branch (Anthropic)

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `OPEN` | `message_start` | — | `usage.prompt_tokens = input + cache_creation + cache_read` (`:217-221`) | `OPEN` |
| `ABSENT` | `content_block_start` | `index ∉ blocks` | store block; `tool_use` → `arguments[index] = ''` and emit id/name/`''` fragment (`:222-230`); `text` → emit its initial text (`:231-232`) | `OPEN_*` |
| `OPEN_*` | `content_block_start` | `index ∈ blocks` | **raise** "started a content block twice" (`:223-224`) | unchanged |
| `ABSENT` (closed) | `content_block_start` | `index ∉ blocks` | accepted; a fresh, empty argument buffer (`:228`) | `OPEN_*` |
| `OPEN_*` | `content_block_delta` | `finish is None ∧ index ∈ blocks` | append text/thinking/signature/partial JSON to the block and `arguments` (`:233-247`); emit the delta for text, thinking and arguments | `OPEN_*` |
| `OPEN_*` | `content_block_delta` | `finish is not None` | **raise** "sent output after its stop reason" (`:215-216`) | unchanged |
| `ABSENT` | `content_block_delta` | — | `KeyError` (`:235`) → mapped to "invalid stream response" by `opencode.py:146-147` | unchanged |
| `OPEN_*` | `content_block_stop` | `index ∈ blocks` | parse `arguments[index]` into `block['input']`, append to `native`, retire both maps (`:248-254`) | `ABSENT` (closed) |
| `ABSENT` | `content_block_stop` | `index ∉ blocks` | **raise** "stopped an unknown content block" (`:249-250`) | unchanged |
| `OPEN` | `message_delta` with `stop_reason` | `finish is None` | `finish = normalized` (`:259-265`) | `STOP_REASON` |
| `STOP_REASON` | `message_delta`, same normalized reason | equal | idempotent, no write (`:263-265`) | `STOP_REASON` |
| `STOP_REASON` | `message_delta`, different reason | unequal | **raise** "conflicting stop reasons" (`:263-264`) | unchanged |
| `STOP_REASON` | `content_block_stop` | `index ∈ blocks` | block still appended to `native`; no output emitted | `ABSENT` |
| `STOP_REASON` | `message_stop` | — | `end()` → `completed = True`, terminal packet (`:266-267` → `:156-162`) | `COMPLETED` |
| `OPEN` | `message_stop` | `finish is None` | **raise** "ended without a finish reason" (`:158-159`) | `OPEN` |
| `COMPLETED` | `message_stop` | — | **no guard**: `end()` runs again and returns a second terminal packet with the same reason | `COMPLETED` |

### `responses` branch (OpenAI)

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| any | `response.output_text.delta` | — | emit content (`:272-273`) | unchanged |
| any | `response.reasoning_summary_text.delta` | — | emit reasoning (`:274-275`) | unchanged |
| any | `response.output_item.added`, item type `function_call` | — | `calls += 1`; emit id/name with `arguments=''` (`:276-280`) | unchanged |
| any | `response.function_call_arguments.delta` | — | emit an arguments fragment for `output_index` (`:281-283`) | unchanged |
| any | `response.output_item.done` | — | append the item to `native` (`:284-285`) | unchanged |
| `OPEN`/`STOP_REASON` | `response.completed` / `response.incomplete`, body normal | no incomplete details, status `completed`, no `error` | `completed = True`; reason `tool_calls` if `calls` else `stop` (`:297`) | `COMPLETED` |
| `OPEN`/`STOP_REASON` | same, body abnormal | `incomplete_details`, status ≠ `completed`, or `error` | reason from `incomplete_details.reason`, else `incomplete` (`:292-296`) | `COMPLETED` |
| `COMPLETED` | another `response.completed` | **no guard** | second terminal packet with the same reason (`:297`) | `COMPLETED` |
| `COMPLETED` | any content/argument delta or item | **no guard** | emitted and appended to `native` (`:272-285`); the turn rejects it (`turns.py:632-635`) | `COMPLETED` |
| any | `response.failed` | — | raise "could not complete this response" (`:298-299`) | unchanged |
| `OPEN` | EOF | — | `opencode.py:142-143` raises "ended before completion" | unchanged |

Duplicate `output_item.added` for one `output_index` increments `calls` twice while
`turns.collect_tool_call` merges by index, so `bool(calls)` and the emitted call set
stay consistent; the count is only used as a boolean at `:297`.

### `google` branch

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| any | `usageMetadata` present | — | replace `usage` with prompt + (candidates + thoughts) (`:184-188`) | unchanged |
| any | candidate part with `text` | — | `native.append(part)`; emit `content`, or `reasoning_content` when `thought` (`:190-193`) | unchanged |
| any | candidate part with `functionCall` | — | `native.append(part)`; emit fragment `index = self.calls`, id from provider or `go_call_N`, arguments `json.dumps(args)`; `calls += 1` (`:194-198`) | unchanged |
| `OPEN`/`STOP_REASON` | candidate with `finishReason` | — | normalize (`:201-206`), `end(normalized)` → `completed` (`:207`) | `COMPLETED` |
| `COMPLETED` | later packet with parts | **no guard** | parts still appended to `native` and emitted (`:190-193`); the turn rejects it | `COMPLETED` |
| `COMPLETED` | later `finishReason` | **no guard** | a second terminal packet | `COMPLETED` |
| any | `promptFeedback.blockReason` | — | raise "could not process" (`:208-209`) — evaluated **after** the parts of the same packet are emitted | unchanged |

### `RouterClient.stream`

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| any | SSE line not starting with `data:` | — | skip (`router.py:205-206`) | unchanged |
| any | `data:` payload `[DONE]` | — | `break` out of the read loop (`:208-209`) | post-loop |
| any | empty payload | — | skip (`:210-211`) | unchanged |
| any | packet with `error` | — | raise the router's message (`:213-214`) | unchanged |
| any | packet with `usage`/`timings` | — | yield a usage-only choice (`:215-217`) | unchanged |
| any | choice | — | `saw_calls \|= bool(delta.tool_calls)` (`:219`) | unchanged |
| any | choice with `finish_reason == 'stop'` | `saw_calls` | rewrite the copy to `tool_calls` (`:222-223`) | unchanged |
| any | choice | — | yield (`:224`) | unchanged |
| any | choice with `finish_reason` | — | `completed = True` and **keep reading** (`:225-226`) | `COMPLETED` |
| `COMPLETED` | later content or tool delta | no guard | yielded; the turn rejects it (`turns.py:632-635`) | `COMPLETED` |
| post-loop | — | `¬completed` | raise "ended before completion" (`:227-228`) | terminal |
| post-loop | — | `completed` | generator returns | terminal |

## Invariants

1. **A finish reason is only emitted when one was recorded.** `end()` refuses to
   build a terminal packet without `reason or self.finish` (`opencode_protocols.py:156-159`);
   `feed` only sets `completed` from a provider finish signal (`:176`, `:207`,
   `:297`). Tests: `test_message_stop_without_a_stop_reason_is_rejected`,
   `test_end_uses_an_explicit_reason_over_the_recorded_one`.
2. **`[DONE]` before a finish reason is always rejected**, in both backends
   (`opencode_protocols.py:165-167`; `router.py:208-209` + `:227-228`). After a
   finish reason it is accepted and yields nothing. Tests:
   `test_done_before_a_finish_reason_is_rejected`,
   `test_done_after_a_finish_reason_is_accepted`,
   `test_done_without_a_finish_reason_is_rejected`.
3. **A stream that ends without a finish reason never reaches a consumer as a
   completed round.** EOF is checked at the backend boundary
   (`opencode.py:139-143`, `router.py:227-228`), independently of consumer
   discipline (`repairs.py:13-25` has no finish check of its own). Tests:
   `test_eof_without_done_or_finish_is_rejected`,
   `test_an_aborted_decode_never_reports_completed`.
4. **`native` contains each completed block exactly once, and only closed
   blocks.** `messages` appends only on `content_block_stop` after removing the
   record from `blocks` (`:249-254`); a second stop or a start while open is
   refused (`:223-224`, `:249-250`). Tests:
   `test_duplicate_block_stop_is_rejected`,
   `test_duplicate_block_start_at_an_open_index_is_rejected`,
   `test_tool_use_arguments_accumulate_into_the_native_input`.
5. **The recorded finish reason never changes.** A repeated identical reason is
   idempotent; a different one raises (`:263-264`). Tests:
   `test_conflicting_stop_reasons_are_rejected`,
   `test_repeated_stop_reason_and_message_stop_keep_the_first_reason`.
6. **The `messages` branch emits no content after the stop reason**
   (`:215-216`). The `chat`/`responses`/`google` branches do not enforce this
   themselves; `turns.py:632-635` rejects such output for every protocol. Test:
   `test_output_after_the_stop_reason_is_rejected`
   (downstream: `tests/test_stream_outcomes.py::test_output_after_finish_and_conflicting_reasons_are_rejected`).
7. **Framing preserves the terminator count.** One `\r`, `\n` or `\r\n` yields
   exactly one line, and the LF half of a CRLF split across chunks is never a
   blank line (`stream_limits.py:106-109`, `:134-140`). Tests:
   `test_crlf_split_across_chunks_yields_one_terminator`,
   `test_empty_chunk_between_cr_and_lf_preserves_the_frame`,
   `test_lone_cr_terminates_lines`, `test_crlf_blank_line_split_across_chunks`.
8. **One BOM, first line, complete bytes.** A leading U+FEFF is removed from the
   first yielded line only (`:127-129`, `:147-148`); a BOM in a later line is
   preserved. Tests:
   `test_bom_split_across_chunks_is_stripped_from_the_first_line_only`.
9. **An unterminated final line is delivered exactly once** (`:145-149`), which
   is what lets `opencode.py:139-141` feed the last event of a stream that ends
   without a blank line. Test: `test_unterminated_final_line_is_delivered_at_eof`.
10. **Line buffering never exceeds `sse_line_bytes`.** The check happens before
    the append (`:115-118`). Test:
    `test_line_at_the_limit_is_accepted_and_one_byte_more_is_rejected`.
11. **Every raw byte read is counted before framing**, including comments and
    framing overhead (`:104` → `:42-48`). Test:
    `test_response_budget_counts_framing_bytes_that_are_not_data_lines`.
12. **Output bytes are charged when the fragment arrives, not when a call
    completes** (`stream_limits.py:50-61`; callers `turns.py:148-153`). A call
    that never stops still consumes the reply budget. Test:
    `test_incomplete_tool_call_fragments_still_consume_the_reply_budget`.
13. **A budget spans every provider request in a reply and is never reset**
    (`stream_limits.py:27`; construction `turns.py:527`, per-request pass-through
    `turns.py:626-628`, `opencode.py:126`, `router.py:175`). Tests:
    `test_one_budget_accumulates_across_two_provider_requests`,
    `test_reasoning_and_reply_budgets_are_independent_and_count_utf8_bytes`.
14. **A fragment refused by the budget is not merged into the call.** The charge
    happens first (`stream_limits.py:56`), the raise second (`:58-61`), and the
    merge in `collect_tool_call` (`turns.py:151-154`) never runs. Test:
    `test_incomplete_tool_call_fragments_still_consume_the_reply_budget`.
15. **The normalisation `stop → tool_calls` belongs to the local router only**
    (`router.py:222-223`). The `chat` decoder passes the provider reason through
    untouched, so the OpenCode Go path cannot relabel a stop. Test:
    `test_decoder_does_not_normalise_stop_after_tool_deltas`,
    `test_stop_after_a_tool_delta_is_normalised_to_tool_calls`,
    `test_stop_without_a_tool_delta_stays_stop`.
16. **A call set that disagrees with the finish reason is refused before any tool
    runs** (`turns.py:179-181`, `:691-698`). This is the reconciliation point that
    makes `calls` a decoder-side counter rather than a decoder-side obligation.

## Hazards and open questions

### H1 — duplicate `content_block_*` events silently corrupted the next request — `defect` (fixed)

`opencode_protocols.py:222-254`, before this change. `blocks[index] = block`
overwrote an open block and `arguments[index] = ''` discarded its argument
prefix; `content_block_stop` appended the block to `native` every time it was
seen. Demonstrated, not theoretical:

- duplicate `content_block_start` at index 0 (a retried event): the decoder
  re-emitted the id/name fragment, so `turns.collect_tool_call`
  (`turns.py:153`) concatenated the name — the round produced
  `name == 'write_filewrite_file'` and `arguments == '{"a":1}{"a":2}'`,
  with no exception raised anywhere.
- duplicate `content_block_stop`: `native` held two identical `tool_use`
  blocks, and `prepare('messages', …)` replayed both in the next request
  (`opencode_protocols.py:64-65`), i.e. a duplicate `tool_use` id on the wire.
- reusing a stopped index for a text block: the retired `arguments[0]` was still
  in the map, so `content_block_stop` attached the old tool JSON as
  `block['input']` to the text block.

Severity `defect` because the decoder silently produced a request that the
provider would reject, and because the first variant also silently altered a
tool name without any guard firing. Demonstrated with tests:
`test_duplicate_block_start_at_an_open_index_is_rejected`,
`test_duplicate_block_stop_is_rejected`,
`test_reusing_a_stopped_index_starts_with_a_clean_argument_buffer` (all three
fail on the pre-change code, see Verification).

### H2 — `responses` never recovers arguments that arrive only in `output_item.done` — `fragile`, demonstrated

`opencode_protocols.py:276-285`. The decoder emits a function call only from
`response.output_item.added` (with `arguments=''`) and from
`response.function_call_arguments.delta`. `response.output_item.done` is only
appended to `native`. A provider or gateway that buffers arguments and sends them
complete in the done item therefore yields a call whose
`function.arguments == ''`; `collect_tool_call` merges nothing, and tool
execution rejects the call for invalid JSON *after* the round has been decoded
and the reason `tool_calls` accepted. The round fails without any effect, so the
failure is fail-closed, but the call never runs.

Note: bead `bra-vom.4` describes this fallback as existing ("falls back to full
arguments in `output_item.done` only if `not arguments.get(idx)`"), and names
`call_indices`/`next_index`, which do not exist in this tree.
`git log -S call_indices` and `git log -S next_index` find no revision of this
file with those names, so the inventory is aspirational rather than stale.
Test that records the gap:
`test_function_call_arguments_completed_only_in_the_done_item_are_not_recovered`
— invert it when the proposal below lands.

### H3 — `end()` hands out the live `native` list — `fragile`, demonstrated by probe

`opencode_protocols.py:161-162` passes `self.native` by reference into
`_provider_content`. On the `google` branch a part in a packet after
`finishReason` is still appended (`:191`) and therefore appears inside the
already-yielded terminal packet's content. `turns.py:632-635` rejects the round,
so no committed history can contain it, but `repairs.py:13-25` consumes choices
without a finish check and would keep appending the late text to a repair. Not
pinned by a test (it would cement the aliasing as expected); reproduced with a
five-line probe feeding
`{'candidates': [{'content': {'parts': [{'text': 'hi'}]}, 'finishReason': 'STOP'}]}`
then `{'candidates': [{'content': {'parts': [{'text': 'late'}]}}]}` and observing
the first terminal packet's `content` grow to two parts. A copy in `end()`
(`'content': list(self.native)`) would remove the aliasing; not changed because
the observable path is already guarded.

### H4 — terminal-state enforcement is layered and asymmetric — `fragile`, documented

`messages` refuses output after its stop reason (`:215-216`); `chat` (`:173-179`),
`responses` (`:272-285`) and `google` (`:190-193`) do not, and none of the four
refuses a duplicate terminal event. The universal guard is
`turns.py:632-635` plus the idempotent-from-the-turn's-perspective
`turns.py:655-660`. A duplicate terminal packet is harmless there
(`delta == {}`, same reason). A consumer that does not reuse `_stream_round`
would lose that guard, but the backend boundary still enforces completion
(invariant 3). Tests:
`test_repeated_stop_reason_and_message_stop_keep_the_first_reason` and
`tests/test_stream_outcomes.py::test_output_after_finish_and_conflicting_reasons_are_rejected`.

### H5 — the bead's state inventory does not match the tree — `documentation-gap`

`bra-vom.4` places `StreamDecoder` at lines 318-621 (actual `:145-300`), claims
`blocks: dict[int, str]`, `arguments: dict[int, bool]`, `calls: set[int]`,
`next_index` and `call_indices` (actual: `blocks: dict[int, dict]`,
`arguments: dict[int, str]`, `calls: int`, neither of the other two), and claims
`StreamBudget` owns `started`, `bytes_used`, `events_used`, `tool_calls` and
`tool_bytes` (actual: `deadline`, `response_bytes`, `reply_bytes`,
`reasoning_bytes`; tool-byte accounting lives in `turns.collect_tool_call`). The
table above records the tree. The acceptance criteria — transitions documented
and verified, framing and budget invariants tested — do not depend on the stale
names.

### H6 — line framing: no defect found — `none-found`

`\r` at a chunk boundary, `\r` alone, `\r\n` split across chunks, an empty chunk
between the `\r` and the `\n`, a BOM split over three chunks, a multi-byte
character split across chunks, and a trailing partial line all frame correctly;
the 8 tests in `SseFramingTests` pin them. The `skip_lf` flag is only ever
set while `pending` is empty (it is set after a line was yielded and cleared),
which is why the swallowed LF cannot lose buffered bytes.

### H7 — two defensive checks in `sse_lines` are unreachable — `none-found`

- `:124-125` (`if line.endswith(b'\r')`) cannot fire: `pending` is only extended
  with `chunk[start:end]` where `end` is the terminator index (`:113`, `:119`),
  so a CR never survives into `pending`.
- `:141-144` (post-loop `pending` size check) cannot fire: `:115-118` raises
  before `pending` can exceed the limit, and the final segment is checked by the
  same expression.

Both are cheap and harmless. Removing them is churn and would shrink the
defence if the scan loop is ever changed, so no change is proposed.

### H8 — `add_output` charges before it refuses — `none-found` (by design)

`stream_limits.py:50-61` increments the counter and then compares, so a fragment
that trips the limit has already been counted, while `collect_tool_call`
(`turns.py:148-154`) does not merge it. The reply aborts either way, so this is
fail-closed; the asymmetry is recorded because it is surprising in isolation.
`add_output` also does not call `check_time` (`:36-40`); a burst of already-read
choices is bounded by the byte caps and by `bounded_choices`' per-choice deadline
check (`:70`), not by `add_output`. Pinned by
`test_incomplete_tool_call_fragments_still_consume_the_reply_budget`.

### H9 — `StreamBudget` requires a running loop and an absolute deadline — `fragile`

`stream_limits.py:31` calls `asyncio.get_running_loop()` in `__init__`, so a
budget cannot be built or reset off-loop, and `deadline` cannot be extended. All
current construction sites are inside async code
(`turns.py:527` with `stream_bounds`, `opencode.py:126`, `router.py:175`), and
the single deadline is the intended "whole reply" bound. Anything that retries a
reply by reusing the budget keeps the original deadline; anything that retries by
building a new budget resets it. Not demonstrated as a problem.

### H10 — a finished stream is drained to EOF rather than closed — `fragile`

`router.py:225-226` sets `completed` and keeps reading; `opencode.py:132-149`
likewise consumes lines until the body closes. A provider that keeps the
connection open after its finish reason is bounded only by
`budget.duration_seconds` (`stream_limits.py:96-103`). Closing the response on
the first finish reason would save that wait but changes who owns the socket and
would drop the usage packet that OpenAI-compatible servers send *after* the
finish reason (`test_usage_packet_after_the_finish_reason_is_delivered_without_content`),
so no change is proposed.

## Formalization decisions

### Applied: refuse block-lifecycle violations in the `messages` branch (H1)

`src/blackrose/backend/opencode_protocols.py:222-254`:

```python
        if kind == 'content_block_start':
            if index in self.blocks:
                raise RuntimeError('OpenCode Go started a content block twice.')
            ...
        elif kind == 'content_block_stop':
            if index not in self.blocks:
                raise RuntimeError('OpenCode Go stopped an unknown content block.')
            block = self.blocks.pop(index)
            if index in self.arguments:
                block['input'] = json.loads(self.arguments.pop(index) or '{}')
            self.native.append(block)
```

Why this shape:

- The demonstrated defect is a lost update plus duplicated provider history, and
  `blocks`/`arguments` are already the state that decides it, so the guard needs
  no new variable and no new layer.
- `pop` on stop makes `blocks` mean **open blocks only**. That is the invariant
  H1 was missing, and it is what stops a retired argument buffer from being
  attached to a later block at the same index.
- Reuse of an index *after* it closed stays allowed: the guard rejects only a
  start while the same index is still open, which no conforming provider emits.
  A provider that numbers blocks per message keeps working
  (`test_reusing_a_stopped_index_starts_with_a_clean_argument_buffer`).
- The raises are `RuntimeError`, which `opencode.py:144-147` does not swallow
  into "invalid stream response", so the failure names the protocol violation.

No other source change is proposed. In particular the duplicated-terminal and
output-after-finish behaviour (H4) is left to `turns.py:632-635`, because moving
that guard into four decoders would duplicate a policy the turn already owns and
would change the emitted-choice contract for consumers that only read text.

### Proposed, not applied: recover buffered `responses` arguments (H2)

Smallest patch that restores the fallback the bead describes, in
`opencode_protocols.py` only:

```python
    def __init__(self, protocol):
        ...
        self.call_indices = {}      # responses: call_id -> output_index

    def responses(self, packet):
        ...
        if kind == 'response.output_item.added' and packet['item']['type'] == 'function_call':
            item = packet['item']
            self.calls += 1
            self.call_indices[item['call_id']] = packet['output_index']
            return [delta(tool_calls=[...])]
        if kind == 'response.function_call_arguments.delta':
            self.arguments[packet['output_index']] = True
            return [delta(tool_calls=[...])]
        if kind == 'response.output_item.done':
            item = packet['item']
            self.native.append(item)
            index = self.call_indices.get(item.get('call_id'))
            if (item.get('type') == 'function_call' and index is not None
                    and index not in self.arguments):
                return [delta(tool_calls=[{'index': index,
                               'function': {'arguments': item.get('arguments') or ''}}])]
```

Not applied because: (a) it adds a state variable and changes the fragment stream
of every `responses` call, which is a transport contract other beads in this epic
may be documenting; (b) the existing tests construct `output_item.done` packets
without `output_index` (`tests/test_opencode.py:344-347`), so the `call_id` map is
required and its interaction with a missing `item['call_id']` needs its own
denial test; (c) the current behaviour is fail-closed (the call is rejected for
invalid arguments before any effect), so it is a functionality gap rather than a
safety one. A second variant remains even with the patch: a `function_call` that
appears only in `output_item.done` (no `added` event) increments no counter, so
`finish` stays `stop` while `native` holds a function call — that variant needs a
separate decision about whether to treat the item as a call.

Follow-up: one bead, "recover buffered Responses arguments and reconcile
`calls` with items seen only in `output_item.done`", with the regression test
being the inversion of
`test_function_call_arguments_completed_only_in_the_done_item_are_not_recovered`.

## Verification

Run from the repository root.

```
$ .venv/bin/python scripts/check_unit_tests.py -p test_sm_04_sse_stream_decoder.py -v
Ran 43 tests in 0.020s

OK
```

Pre-existing focused suites for the touched modules:

```
$ .venv/bin/python scripts/check_unit_tests.py -p test_telemetry.py
Ran 5 tests in 0.004s
OK

$ .venv/bin/python scripts/check_unit_tests.py -p test_opencode.py
Ran 25 tests in 0.153s
OK

$ .venv/bin/python scripts/check_unit_tests.py -p test_stream_outcomes.py
Ran 12 tests in 1.148s
OK

$ .venv/bin/python scripts/check_unit_tests.py -p test_interactive_ui_evaluation.py
Ran 5 tests in 0.002s
OK

$ .venv/bin/python scripts/check_unit_tests.py -p test_stream_limits.py
Ran 13 tests in 1.270s

FAILED (errors=2)
```

The two `test_stream_limits.py` errors are environmental and pre-existing in this
worktree: `test_ui_repair_substream_uses_the_same_turn_byte_budget` and
`test_ui_repair_failure_logs_only_operation_and_class` fail inside
`blackrose/ui_documents.py:55` with
`FileNotFoundError: src/blackrose/gui/static/ui-contract.json`, a gitignored
generated artifact (`.gitignore:34`) that is absent here. The traceback never
reaches `stream_limits` or `opencode_protocols`; the other 11 tests in that file
pass. Generating the artifact (`npm test --prefix ui-runtime`, or the build step
that writes it) would be needed to run those two.

Regression evidence for H1 (the guard was reverted in a scratch copy, the file
restored byte-identically afterwards; no git command was used):

```
test_duplicate_block_start_at_an_open_index_is_rejected -> FAILED (failures=1)
test_duplicate_block_stop_is_rejected                  -> FAILED (failures=1)
test_block_stop_for_an_unopened_index_is_rejected      -> FAILED (errors=1)
test_reusing_a_stopped_index_starts_with_a_clean_argument_buffer -> FAILED (failures=1)
```

Static checks:

```
$ .venv/bin/python -m ruff check src/blackrose/backend/opencode_protocols.py tests/test_sm_04_sse_stream_decoder.py
All checks passed!

$ .venv/bin/python -m mypy
Success: no issues found in 19 source files
```

Whole offline suite in this worktree:

```
$ .venv/bin/python scripts/check_unit_tests.py
Ran 1261 tests in 41.331s

FAILED (failures=4, errors=53, skipped=23)
```

All 57 of those are the same environmental gap, not this change:
48 errors are `FileNotFoundError: src/blackrose/gui/static/ui-contract.json`,
1 is `motion-canvas-runtime.js`, 3 are `ValueError: Motion Canvas needs setup:
run blackrose setup-animations`, and the 3 saved-compatibility failures plus 1
`test_ui_documents` `IndexError` are downstream of the missing UI contract
(`'Interactive UI runtime unavailable; rebuild ui-runtime'`). The affected
modules are `test_ui_documents`, `test_interactive_ui_fidelity`,
`test_saved_compatibility`, `test_security_boundaries`, `test_motion_*`,
`test_session_lifecycle`, `test_transcript_history` and the two `test_stream_limits`
repair tests — none of the diff's call sites. Building `ui-runtime`
(`npm test --prefix ui-runtime`) and running `blackrose setup-animations` would
be required for a green full-suite run in a fresh worktree.

Not run: the native `--gui` probes and any live provider probe. Neither is
needed: the change is a pure guard inside one decoder and the protocol matrix is
covered offline by `tests/test_stream_outcomes.py` and `tests/test_opencode.py`.
