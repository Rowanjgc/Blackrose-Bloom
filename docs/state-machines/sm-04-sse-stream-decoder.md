# SM-04: SSE stream decoder, block accumulator and line framing

## Scope and ownership

| Surface | Files | Owner |
| --- | --- | --- |
| Line framing | `src/blackrose/backend/stream_limits.py::sse_lines` | The provider client that opened the HTTP response |
| Wire validation and resource bounds | `src/blackrose/backend/stream_limits.py::StreamLimitExceeded`, `src/blackrose/backend/stream_limits.py::StreamBounds`, `src/blackrose/backend/stream_limits.py::StreamBudget`, `src/blackrose/backend/stream_limits.py::bounded_choices` | The reply (one budget per reply, passed to every provider request) |
| Protocol decoding | `src/blackrose/backend/opencode_protocols.py::StreamDecoder` | The OpenCode Go request that created it (`src/blackrose/backend/opencode.py::OpenCodeGoClient.stream`) |
| Local wire decoding | `src/blackrose/backend/router.py::RouterClient.stream` | The local router request |
| Downstream call/finish reconciliation | `src/blackrose/turns.py::RoundOutput`, `src/blackrose/turns.py::collect_tool_call`, `src/blackrose/turns.py::require_normal_finish`, `src/blackrose/turns.py::TurnRunner._stream_round`, `src/blackrose/turns.py::TurnRunner._run_tools` | The turn (`TurnRunner`) |

Ownership notes:

- Every object here is **non-durable and task-local**. `sse_lines` framing
  state and `StreamDecoder` fields exist only for the lifetime of one HTTP
  response inside one asyncio task. No database column, JSON blob or file
  represents them, and none is shared between tasks.
- `StreamBudget` is the one object with a wider lifetime: it is constructed once
  per reply (`src/blackrose/turns.py::TurnRunner._start_reply`), stored on `ReplyContext.budget`
  (`src/blackrose/turns.py::ReplyContext`) and passed to every provider request in that reply
  (`src/blackrose/turns.py::TurnRunner._stream_round`), including the repair substream
  (`src/blackrose/repairs.py::repair_text`). Its counters are monotonic by design
  (the `src/blackrose/backend/stream_limits.py::StreamBudget` docstring).
- The only *persisted* consequence is `_provider_content`: `StreamDecoder.end`
  returns `{'protocol', 'content'}` (`src/blackrose/backend/opencode_protocols.py::StreamDecoder.end`), the turn
  stores it in `RoundOutput.provider_content` (`src/blackrose/turns.py::TurnRunner._stream_round`) and
  `Providers.continuation` copies it onto the assistant wire message
  (`src/blackrose/backend/providers.py::ChatProviders.continuation`), where `prepare` replays it as native provider
  content (`src/blackrose/backend/opencode_protocols.py::_prepare`, once per protocol).
- All mutation happens on the event loop. The only ordering points that yield to
  other tasks are `asyncio.sleep(0)` every 32 lines/chunks
  (`src/blackrose/backend/stream_limits.py::sse_lines`, `src/blackrose/backend/stream_limits.py::bounded_choices`) and the `await` on the next chunk/choice.
  There is no lock and none is needed.
- Cancellation (Stop) propagates into the async generator; `async with
  self.client.stream(...)` (`src/blackrose/backend/opencode.py::OpenCodeGoClient.stream`) and `aclosing`
  (`src/blackrose/backend/stream_limits.py::bounded_choices`) close the response. The decoder is then discarded with
  `completed=False`; nothing has to be released.
- `StreamDecoder` is also subclassed for acceptance evidence
  (`scripts/accept_opencode_go.py::observed_decoder.ObservedDecoder`), which reads only `feed`,
  `completed` and `native`.

## State variables

### `sse_lines` (frames one HTTP response)

All are locals of `src/blackrose/backend/stream_limits.py::sse_lines`.

| Variable | Type | Meaning | Writers |
| --- | --- | --- | --- |
| `pending` | `bytearray` | Bytes of the current line that have not seen `\r`/`\n` yet | appended with each line segment, cleared when a line is yielded |
| `first_line` | `bool` | The next yielded line is still the first one, so a leading BOM is removed | cleared when the first line (or the final partial line) is yielded |
| `skip_lf` | `bool` | The previous chunk ended exactly after `\r`; a `\n` opening the next non-empty chunk belongs to that CRLF | cleared at the next non-empty chunk; set when a chunk ends in `\r` |
| `line_count` | `int` | Cooperative-yield counter | incremented per yielded line |
| `chunk`, `start`, `newline` | locals | Current chunk and scan position | loop locals |

### `StreamBudget` (one reply)

All are attributes set in `src/blackrose/backend/stream_limits.py::StreamBudget.__init__`.

| Variable | Type | Meaning | Writers |
| --- | --- | --- | --- |
| `bounds` | `StreamBounds` | Frozen limits (`src/blackrose/backend/stream_limits.py::StreamBounds`) | constructor |
| `deadline` | `float` | Absolute loop time; `now + duration_seconds`, fixed at construction | constructor |
| `response_bytes` | `int` | Raw bytes read off the wire, before framing | `src/blackrose/backend/stream_limits.py::StreamBudget.add_response_bytes` |
| `reply_bytes` | `int` | UTF-8 bytes of visible answer text, tool names/ids/arguments | `src/blackrose/backend/stream_limits.py::StreamBudget.add_output` (from `src/blackrose/turns.py::collect_tool_call`, `src/blackrose/repairs.py::repair_text`) |
| `reasoning_bytes` | `int` | UTF-8 bytes of reasoning text | `src/blackrose/backend/stream_limits.py::StreamBudget.add_output` |

### `StreamDecoder` (one provider response)

All are attributes set in `src/blackrose/backend/opencode_protocols.py::StreamDecoder.__init__`.

| Variable | Type | Meaning | Writers |
| --- | --- | --- | --- |
| `protocol` | `'chat' \| 'messages' \| 'responses' \| 'google'` | Selects the branch in `feed` | constructor (`src/blackrose/backend/opencode.py::OpenCodeGoClient.stream`) |
| `completed` | `bool` | A finish reason has been recorded; the backend boundary requires it (`src/blackrose/backend/opencode.py::OpenCodeGoClient.stream`) | `src/blackrose/backend/opencode_protocols.py::StreamDecoder.feed` (chat branch), `src/blackrose/backend/opencode_protocols.py::StreamDecoder.end` |
| `blocks` | `dict[int, dict]` | **Open** Anthropic content blocks by index (after this change) | `src/blackrose/backend/opencode_protocols.py::StreamDecoder.anthropic`: add on `content_block_start`, pop on `content_block_stop` |
| `arguments` | `dict[int, str]` | Partial JSON of an open `tool_use` block | `src/blackrose/backend/opencode_protocols.py::StreamDecoder.anthropic`: init on start, append on delta, pop on stop |
| `native` | `list[dict]` | Provider-native history for the next request | `src/blackrose/backend/opencode_protocols.py::StreamDecoder.anthropic` (messages), `src/blackrose/backend/opencode_protocols.py::StreamDecoder.responses` (responses), `src/blackrose/backend/opencode_protocols.py::StreamDecoder.feed` (google) |
| `finish` | `str \| None` | Normalized stop reason; **unused on the `chat` branch** | `src/blackrose/backend/opencode_protocols.py::StreamDecoder.anthropic` (`message_delta`) |
| `calls` | `int` | Count of tool calls opened (`responses`) or emitted (`google`) | `src/blackrose/backend/opencode_protocols.py::StreamDecoder.responses`, `src/blackrose/backend/opencode_protocols.py::StreamDecoder.feed` (google) |
| `usage` | `dict` | Normalized `prompt_tokens`/`completion_tokens` | `anthropic` (`message_start`, `message_delta`), `feed` (google), `responses` (terminal event) |

### `RouterClient.stream` (one local response)

Both are locals of `src/blackrose/backend/router.py::RouterClient.stream`.

| Variable | Type | Meaning | Writers |
| --- | --- | --- | --- |
| `completed` | `bool` | A choice carried `finish_reason` | per choice |
| `saw_calls` | `bool` | A tool-call delta was seen anywhere in the stream | per choice |

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
| `CALL_READY` | the stream ended; `require_normal_finish` compares `bool(calls)` with the reason (`src/blackrose/turns.py::require_normal_finish`) |

## Events

| Event | Source | Enters at |
| --- | --- | --- |
| Byte chunk from the wire | `response.aiter_bytes()` | `src/blackrose/backend/stream_limits.py::sse_lines` |
| Upstream exhaustion | `StopAsyncIteration` | `sse_lines`, `bounded_choices` |
| Deadline exceeded | `asyncio.timeout_at` / `check_time` | `sse_lines`, `src/blackrose/turns.py::TurnRunner._stream_round` |
| Budget exceeded | byte counters | `src/blackrose/backend/stream_limits.py::StreamBudget.add_response_bytes`, `src/blackrose/backend/stream_limits.py::StreamBudget.add_output` |
| SSE line | framing output | `src/blackrose/backend/opencode.py::OpenCodeGoClient.stream`, `src/blackrose/backend/router.py::RouterClient.stream` |
| `data:` payload terminated by a blank line | blank SSE line | `src/blackrose/backend/opencode.py::OpenCodeGoClient.stream` (feeds the decoder) |
| `[DONE]` | literal payload | `src/blackrose/backend/opencode_protocols.py::StreamDecoder.feed`, `src/blackrose/backend/router.py::RouterClient.stream` |
| JSON packet | `json.loads` | `feed`, then its branch dispatch |
| Provider error packet | `error` / `type == 'error'` | `feed`, `src/blackrose/backend/router.py::RouterClient.stream` |
| `end(reason)` | messages `message_stop`, responses `completed`/`incomplete`, google `finishReason` | `src/blackrose/backend/opencode_protocols.py::StreamDecoder.end`, called from `anthropic`, `responses` and the google branch of `feed` |
| EOF of the HTTP body | iterator exhaustion | `src/blackrose/backend/opencode.py::OpenCodeGoClient.stream`, `src/blackrose/backend/router.py::RouterClient.stream` |
| Cancellation / Stop | task cancel | generator close |

## Transition table

### F: `sse_lines`

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `START`/`BODY` | chunk | no `\r`/`\n` in chunk | append chunk to `pending` | `FIRST_PARTIAL`/`BODY` |
| `FIRST_PARTIAL`/`BODY` | chunk | terminator found | append head, yield assembled line | `BODY` |
| any | chunk | terminator is `\n` | `start = newline + 1` | `BODY` |
| any | chunk | terminator is `\r`, next byte in chunk is `\n` | consume both | `BODY` |
| any | chunk | terminator is `\r`, more bytes follow in chunk | `start = newline + 1` | `BODY` |
| any | chunk | terminator is `\r` and it is the last byte | `skip_lf = True` | `CR_BOUNDARY` |
| `CR_BOUNDARY` | non-empty chunk starting with `\n` | `skip_lf and chunk` | `start = 1`; swallowed LF is **not** a line | `BODY` |
| `CR_BOUNDARY` | non-empty chunk not starting with `\n` | `skip_lf and chunk` | `start = 0` | `BODY` |
| `CR_BOUNDARY` | empty chunk | `if skip_lf and chunk` is false | `skip_lf` preserved | `CR_BOUNDARY` |
| any | chunk | `len(pending) + segment > sse_line_bytes` | raise `StreamLimitExceeded` **before** appending | unchanged |
| any | chunk | `response_bytes + len(chunk) > response_bytes` | raise (`src/blackrose/backend/stream_limits.py::StreamBudget.add_response_bytes`) | unchanged |
| any | top of loop | `now >= deadline` | raise (`src/blackrose/backend/stream_limits.py::StreamBudget.check_time`) | unchanged |
| any | await next chunk | `asyncio.timeout_at` fires | raise `StreamLimitExceeded` | unchanged |
| `FIRST_PARTIAL`/`BODY` | EOF | `pending != b''` | yield `pending` before returning | `EXHAUSTED` |
| `BODY` | EOF | `pending == b''` | return | `EXHAUSTED` |
| `CR_BOUNDARY` | EOF | — | return; `skip_lf` is dropped | `EXHAUSTED` |

The first yielded line additionally loses one leading `\ufeff`, and a
final partial line loses it too. Lines are decoded per assembled
line, never per chunk, so a multi-byte character or BOM split across chunks is
assembled before decoding.

### S/B: `StreamDecoder.feed` — dispatch

All in `src/blackrose/backend/opencode_protocols.py::StreamDecoder.feed` unless named.

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| any | `data == '[DONE]'` | `completed` | return `[]` | unchanged |
| any | `data == '[DONE]'` | `¬completed` | raise "ended without a finish reason" | unchanged |
| any | packet with `error`/`type == 'error'` | — | raise generic error (deliberately no provider text) | unchanged |
| any | any packet | `protocol == 'chat'` | see below | — |
| any | any packet | `protocol == 'messages'` | `anthropic()` (`src/blackrose/backend/opencode_protocols.py::StreamDecoder.anthropic`) | — |
| any | any packet | `protocol == 'responses'` | `responses()` (`src/blackrose/backend/opencode_protocols.py::StreamDecoder.responses`) | — |
| any | any packet | otherwise | google branch (inline in `feed`) | — |

### `chat` branch

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `OPEN`/`STOP_REASON` | packet with choices | — | yield every choice verbatim | unchanged |
| `OPEN` | any choice has `finish_reason` | — | `completed = True` | `COMPLETED` |
| `COMPLETED` | another choice with a finish reason | no guard | `completed` stays true, choice yielded | `COMPLETED` |
| `COMPLETED` | content/reasoning/tool delta | no decoder guard | yielded; the turn rejects it (`src/blackrose/turns.py::TurnRunner._stream_round`) | `COMPLETED` |
| any | packet carries `usage` | — | append a `{'delta': {}, '_usage': …}` pseudo-choice after the real choices | unchanged |
| `COMPLETED` | EOF | — | `src/blackrose/backend/opencode.py::OpenCodeGoClient.stream` sees `completed` and returns | terminal |

`finish` is never written on this branch: `end()` is unreachable for `chat`.

### `messages` branch (Anthropic)

All in `src/blackrose/backend/opencode_protocols.py::StreamDecoder.anthropic` unless named.

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `OPEN` | `message_start` | — | `usage.prompt_tokens = input + cache_creation + cache_read` | `OPEN` |
| `ABSENT` | `content_block_start` | `index ∉ blocks` | store block; `tool_use` → `arguments[index] = ''` and emit id/name/`''` fragment; `text` → emit its initial text | `OPEN_*` |
| `OPEN_*` | `content_block_start` | `index ∈ blocks` | **raise** "started a content block twice" | unchanged |
| `ABSENT` (closed) | `content_block_start` | `index ∉ blocks` | accepted; a fresh, empty argument buffer | `OPEN_*` |
| `OPEN_*` | `content_block_delta` | `finish is None ∧ index ∈ blocks` | append text/thinking/signature/partial JSON to the block and `arguments`; emit the delta for text, thinking and arguments | `OPEN_*` |
| `OPEN_*` | `content_block_delta` | `finish is not None` | **raise** "sent output after its stop reason" | unchanged |
| `ABSENT` | `content_block_delta` | — | `KeyError` → mapped to "invalid stream response" by `src/blackrose/backend/opencode.py::OpenCodeGoClient.stream` | unchanged |
| `OPEN_*` | `content_block_stop` | `index ∈ blocks` | parse `arguments[index]` into `block['input']`, append to `native`, retire both maps | `ABSENT` (closed) |
| `ABSENT` | `content_block_stop` | `index ∉ blocks` | **raise** "stopped an unknown content block" | unchanged |
| `OPEN` | `message_delta` with `stop_reason` | `finish is None` | `finish = normalized` | `STOP_REASON` |
| `STOP_REASON` | `message_delta`, same normalized reason | equal | idempotent, no write | `STOP_REASON` |
| `STOP_REASON` | `message_delta`, different reason | unequal | **raise** "conflicting stop reasons" | unchanged |
| `STOP_REASON` | `content_block_stop` | `index ∈ blocks` | block still appended to `native`; no output emitted | `ABSENT` |
| `STOP_REASON` | `message_stop` | — | `end()` → `completed = True`, terminal packet (`src/blackrose/backend/opencode_protocols.py::StreamDecoder.end`) | `COMPLETED` |
| `OPEN` | `message_stop` | `finish is None` | **raise** "ended without a finish reason" (`end`) | `OPEN` |
| `COMPLETED` | `message_stop` | — | **no guard**: `end()` runs again and returns a second terminal packet with the same reason | `COMPLETED` |

### `responses` branch (OpenAI)

All in `src/blackrose/backend/opencode_protocols.py::StreamDecoder.responses` unless named.

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| any | `response.output_text.delta` | — | emit content | unchanged |
| any | `response.reasoning_summary_text.delta` | — | emit reasoning | unchanged |
| any | `response.output_item.added`, item type `function_call` | — | `calls += 1`; emit id/name with `arguments=''` | unchanged |
| any | `response.function_call_arguments.delta` | — | emit an arguments fragment for `output_index` | unchanged |
| any | `response.output_item.done` | — | append the item to `native` | unchanged |
| `OPEN`/`STOP_REASON` | `response.completed` / `response.incomplete`, body normal | no incomplete details, status `completed`, no `error` | `completed = True`; reason `tool_calls` if `calls` else `stop` | `COMPLETED` |
| `OPEN`/`STOP_REASON` | same, body abnormal | `incomplete_details`, status ≠ `completed`, or `error` | reason from `incomplete_details.reason`, else `incomplete` | `COMPLETED` |
| `COMPLETED` | another `response.completed` | **no guard** | second terminal packet with the same reason | `COMPLETED` |
| `COMPLETED` | any content/argument delta or item | **no guard** | emitted and appended to `native`; the turn rejects it (`src/blackrose/turns.py::TurnRunner._stream_round`) | `COMPLETED` |
| any | `response.failed` | — | raise "could not complete this response" | unchanged |
| `OPEN` | EOF | — | `src/blackrose/backend/opencode.py::OpenCodeGoClient.stream` raises "ended before completion" | unchanged |

Duplicate `output_item.added` for one `output_index` increments `calls` twice while
`turns.collect_tool_call` merges by index, so `bool(calls)` and the emitted call set
stay consistent; the count is only used as a boolean when the terminal reason is chosen.

### `google` branch

All inline in `src/blackrose/backend/opencode_protocols.py::StreamDecoder.feed`.

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| any | `usageMetadata` present | — | replace `usage` with prompt + (candidates + thoughts) | unchanged |
| any | candidate part with `text` | — | `native.append(part)`; emit `content`, or `reasoning_content` when `thought` | unchanged |
| any | candidate part with `functionCall` | — | `native.append(part)`; emit fragment `index = self.calls`, id from provider or `go_call_N`, arguments `json.dumps(args)`; `calls += 1` | unchanged |
| `OPEN`/`STOP_REASON` | candidate with `finishReason` | — | normalize, `end(normalized)` → `completed` | `COMPLETED` |
| `COMPLETED` | later packet with parts | **no guard** | parts still appended to `native` and emitted; the turn rejects it | `COMPLETED` |
| `COMPLETED` | later `finishReason` | **no guard** | a second terminal packet | `COMPLETED` |
| any | `promptFeedback.blockReason` | — | raise "could not process" — evaluated **after** the parts of the same packet are emitted | unchanged |

### `RouterClient.stream`

All in `src/blackrose/backend/router.py::RouterClient.stream`.

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| any | SSE line not starting with `data:` | — | skip | unchanged |
| any | `data:` payload `[DONE]` | — | `break` out of the read loop | post-loop |
| any | empty payload | — | skip | unchanged |
| any | packet with `error` | — | raise the router's message | unchanged |
| any | packet with `usage`/`timings` | — | yield a usage-only choice | unchanged |
| any | choice | — | `saw_calls \|= bool(delta.tool_calls)` | unchanged |
| any | choice with `finish_reason == 'stop'` | `saw_calls` | rewrite the copy to `tool_calls` | unchanged |
| any | choice | — | yield | unchanged |
| any | choice with `finish_reason` | — | `completed = True` and **keep reading** | `COMPLETED` |
| `COMPLETED` | later content or tool delta | no guard | yielded; the turn rejects it (`src/blackrose/turns.py::TurnRunner._stream_round`) | `COMPLETED` |
| post-loop | — | `¬completed` | raise "ended before completion" | terminal |
| post-loop | — | `completed` | generator returns | terminal |

## Invariants

1. **A finish reason is only emitted when one was recorded.** `end()` refuses to
   build a terminal packet without `reason or self.finish` (`src/blackrose/backend/opencode_protocols.py::StreamDecoder.end`);
   `feed` only sets `completed` from a provider finish signal (the chat
   branch of `feed`, or through `end`). Tests: `test_message_stop_without_a_stop_reason_is_rejected`,
   `test_end_uses_an_explicit_reason_over_the_recorded_one`.
2. **`[DONE]` before a finish reason is always rejected**, in both backends
   (`src/blackrose/backend/opencode_protocols.py::StreamDecoder.feed`; `src/blackrose/backend/router.py::RouterClient.stream`). After a
   finish reason it is accepted and yields nothing. Tests:
   `test_done_before_a_finish_reason_is_rejected`,
   `test_done_after_a_finish_reason_is_accepted`,
   `test_done_without_a_finish_reason_is_rejected`.
3. **A stream that ends without a finish reason never reaches a consumer as a
   completed round.** EOF is checked at the backend boundary
   (`src/blackrose/backend/opencode.py::OpenCodeGoClient.stream`, `src/blackrose/backend/router.py::RouterClient.stream`), independently of consumer
   discipline (`src/blackrose/repairs.py::repair_text` has no finish check of its own). Tests:
   `test_eof_without_done_or_finish_is_rejected`,
   `test_an_aborted_decode_never_reports_completed`.
4. **`native` contains each completed block exactly once, and only closed
   blocks.** `messages` appends only on `content_block_stop` after removing the
   record from `blocks` (`src/blackrose/backend/opencode_protocols.py::StreamDecoder.anthropic`); a second stop or a start while open is
   refused. Tests:
   `test_duplicate_block_stop_is_rejected`,
   `test_duplicate_block_start_at_an_open_index_is_rejected`,
   `test_tool_use_arguments_accumulate_into_the_native_input`.
5. **The recorded finish reason never changes.** A repeated identical reason is
   idempotent; a different one raises (`src/blackrose/backend/opencode_protocols.py::StreamDecoder.anthropic`). Tests:
   `test_conflicting_stop_reasons_are_rejected`,
   `test_repeated_stop_reason_and_message_stop_keep_the_first_reason`.
6. **The `messages` branch emits no content after the stop reason**
   (`src/blackrose/backend/opencode_protocols.py::StreamDecoder.anthropic`). The `chat`/`responses`/`google` branches do not enforce this
   themselves; `src/blackrose/turns.py::TurnRunner._stream_round` rejects such output for every protocol. Test:
   `test_output_after_the_stop_reason_is_rejected`
   (downstream: `tests/test_stream_outcomes.py::StreamOutcomeTests.test_output_after_finish_and_conflicting_reasons_are_rejected`).
7. **Framing preserves the terminator count.** One `\r`, `\n` or `\r\n` yields
   exactly one line, and the LF half of a CRLF split across chunks is never a
   blank line (`src/blackrose/backend/stream_limits.py::sse_lines`). Tests:
   `test_crlf_split_across_chunks_yields_one_terminator`,
   `test_empty_chunk_between_cr_and_lf_preserves_the_frame`,
   `test_lone_cr_terminates_lines`, `test_crlf_blank_line_split_across_chunks`.
8. **One BOM, first line, complete bytes.** A leading U+FEFF is removed from the
   first yielded line only (`sse_lines`); a BOM in a later line is
   preserved. Tests:
   `test_bom_split_across_chunks_is_stripped_from_the_first_line_only`.
9. **An unterminated final line is delivered exactly once** (`sse_lines`), which
   is what lets `src/blackrose/backend/opencode.py::OpenCodeGoClient.stream` feed the last event of a stream that ends
   without a blank line. Test: `test_unterminated_final_line_is_delivered_at_eof`.
10. **Line buffering never exceeds `sse_line_bytes`.** The check happens before
    the append (`sse_lines`). Test:
    `test_line_at_the_limit_is_accepted_and_one_byte_more_is_rejected`.
11. **Every raw byte read is counted before framing**, including comments and
    framing overhead (`sse_lines` → `src/blackrose/backend/stream_limits.py::StreamBudget.add_response_bytes`). Test:
    `test_response_budget_counts_framing_bytes_that_are_not_data_lines`.
12. **Output bytes are charged when the fragment arrives, not when a call
    completes** (`src/blackrose/backend/stream_limits.py::StreamBudget.add_output`; caller `src/blackrose/turns.py::collect_tool_call`). A call
    that never stops still consumes the reply budget. Test:
    `test_incomplete_tool_call_fragments_still_consume_the_reply_budget`.
13. **A budget spans every provider request in a reply and is never reset**
    (the `src/blackrose/backend/stream_limits.py::StreamBudget` docstring; construction `src/blackrose/turns.py::TurnRunner._start_reply`, per-request pass-through
    `src/blackrose/turns.py::TurnRunner._stream_round`, `src/blackrose/backend/opencode.py::OpenCodeGoClient.stream`, `src/blackrose/backend/router.py::RouterClient.stream`). Tests:
    `test_one_budget_accumulates_across_two_provider_requests`,
    `test_reasoning_and_reply_budgets_are_independent_and_count_utf8_bytes`.
14. **A fragment refused by the budget is not merged into the call.** The charge
    happens first, the raise second (both in `src/blackrose/backend/stream_limits.py::StreamBudget.add_output`), and the
    merge in `src/blackrose/turns.py::collect_tool_call` never runs. Test:
    `test_incomplete_tool_call_fragments_still_consume_the_reply_budget`.
15. **The normalisation `stop → tool_calls` belongs to the local router only**
    (`src/blackrose/backend/router.py::RouterClient.stream`). The `chat` decoder passes the provider reason through
    untouched, so the OpenCode Go path cannot relabel a stop. Test:
    `test_decoder_does_not_normalise_stop_after_tool_deltas`,
    `test_stop_after_a_tool_delta_is_normalised_to_tool_calls`,
    `test_stop_without_a_tool_delta_stays_stop`.
16. **A call set that disagrees with the finish reason is refused before any tool
    runs** (`src/blackrose/turns.py::require_normal_finish`, `src/blackrose/turns.py::TurnRunner._run_tools`). This is the reconciliation point that
    makes `calls` a decoder-side counter rather than a decoder-side obligation.

## Hazards and open questions

### H1 — duplicate `content_block_*` events silently corrupted the next request — `defect` (fixed)

`src/blackrose/backend/opencode_protocols.py::StreamDecoder.anthropic`, before this change. `blocks[index] = block`
overwrote an open block and `arguments[index] = ''` discarded its argument
prefix; `content_block_stop` appended the block to `native` every time it was
seen. Demonstrated, not theoretical:

- duplicate `content_block_start` at index 0 (a retried event): the decoder
  re-emitted the id/name fragment, so `src/blackrose/turns.py::collect_tool_call`
  concatenated the name — the round produced
  `name == 'write_filewrite_file'` and `arguments == '{"a":1}{"a":2}'`,
  with no exception raised anywhere.
- duplicate `content_block_stop`: `native` held two identical `tool_use`
  blocks, and `prepare('messages', …)` replayed both in the next request
  (`src/blackrose/backend/opencode_protocols.py::_prepare`), i.e. a duplicate `tool_use` id on the wire.
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

`src/blackrose/backend/opencode_protocols.py::StreamDecoder.responses`. The decoder emits a function call only from
`response.output_item.added` (with `arguments=''`) and from
`response.function_call_arguments.delta`. `response.output_item.done` is only
appended to `native`. A provider or gateway that buffers arguments and sends them
complete in the done item therefore yields a call whose
`function.arguments == ''`; `collect_tool_call` merges nothing, and tool
execution rejects the call for invalid JSON *after* the round has been decoded
and the reason `tool_calls` accepted. The round fails without any effect, so the
failure is fail-closed, but the call never runs.

No revision of the decoder has had this fallback (`git log -S call_indices`
and `git log -S next_index` find none). Test that records the gap:
`test_function_call_arguments_completed_only_in_the_done_item_are_not_recovered`
— invert it when the proposal below lands.

### H3 — `end()` hands out the live `native` list — `fragile`, demonstrated by probe

`src/blackrose/backend/opencode_protocols.py::StreamDecoder.end` passes `self.native` by reference into
`_provider_content`. On the `google` branch a part in a packet after
`finishReason` is still appended (`src/blackrose/backend/opencode_protocols.py::StreamDecoder.feed`) and therefore appears inside the
already-yielded terminal packet's content. `src/blackrose/turns.py::TurnRunner._stream_round` rejects the round,
so no committed history can contain it, but `src/blackrose/repairs.py::repair_text` consumes choices
without a finish check and would keep appending the late text to a repair. Not
pinned by a test (it would cement the aliasing as expected); reproduced with a
five-line probe feeding
`{'candidates': [{'content': {'parts': [{'text': 'hi'}]}, 'finishReason': 'STOP'}]}`
then `{'candidates': [{'content': {'parts': [{'text': 'late'}]}}]}` and observing
the first terminal packet's `content` grow to two parts. A copy in `end()`
(`'content': list(self.native)`) would remove the aliasing; not changed because
the observable path is already guarded.

### H4 — terminal-state enforcement is layered and asymmetric — `fragile`, documented

`messages` refuses output after its stop reason (`src/blackrose/backend/opencode_protocols.py::StreamDecoder.anthropic`); `chat` and `google`
(`src/blackrose/backend/opencode_protocols.py::StreamDecoder.feed`) and `responses` (`src/blackrose/backend/opencode_protocols.py::StreamDecoder.responses`) do not, and none of the four
refuses a duplicate terminal event. The universal guard is
the post-latch check in `src/blackrose/turns.py::TurnRunner._stream_round` plus its idempotent-from-the-turn's-perspective
finish-reason latch. A duplicate terminal packet is harmless there
(`delta == {}`, same reason). A consumer that does not reuse `_stream_round`
would lose that guard, but the backend boundary still enforces completion
(invariant 3). Tests:
`test_repeated_stop_reason_and_message_stop_keep_the_first_reason` and
`tests/test_stream_outcomes.py::StreamOutcomeTests.test_output_after_finish_and_conflicting_reasons_are_rejected`.

### H5 — the bead's state inventory does not match the tree — `documentation-gap`

`bra-vom.4` places `StreamDecoder` in the wrong part of the file, claims
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

Both in `src/blackrose/backend/stream_limits.py::sse_lines`:

- `if line.endswith(b'\r')` cannot fire: `pending` is only extended
  with `chunk[start:end]` where `end` is the terminator index,
  so a CR never survives into `pending`.
- The post-loop `pending` size check cannot fire: the in-loop size check raises
  before `pending` can exceed the limit, and the final segment is checked by the
  same expression.

Both are cheap and harmless. Removing them is churn and would shrink the
defence if the scan loop is ever changed, so no change is proposed.

### H8 — `add_output` charges before it refuses — `none-found` (by design)

`src/blackrose/backend/stream_limits.py::StreamBudget.add_output` increments the counter and then compares, so a fragment
that trips the limit has already been counted, while `src/blackrose/turns.py::collect_tool_call`
does not merge it. The reply aborts either way, so this is
fail-closed; the asymmetry is recorded because it is surprising in isolation.
`add_output` also does not call `src/blackrose/backend/stream_limits.py::StreamBudget.check_time`; a burst of already-read
choices is bounded by the byte caps and by `src/blackrose/backend/stream_limits.py::bounded_choices`' per-choice deadline
check, not by `add_output`. Pinned by
`test_incomplete_tool_call_fragments_still_consume_the_reply_budget`.

### H9 — `StreamBudget` requires a running loop and an absolute deadline — `fragile`

`src/blackrose/backend/stream_limits.py::StreamBudget.__init__` calls `asyncio.get_running_loop()`, so a
budget cannot be built or reset off-loop, and `deadline` cannot be extended. All
current construction sites are inside async code
(`src/blackrose/turns.py::TurnRunner._start_reply` with `stream_bounds`, `src/blackrose/backend/opencode.py::OpenCodeGoClient.stream`, `src/blackrose/backend/router.py::RouterClient.stream`), and
the single deadline is the intended "whole reply" bound. Anything that retries a
reply by reusing the budget keeps the original deadline; anything that retries by
building a new budget resets it. Not demonstrated as a problem.

### H10 — a finished stream is drained to EOF rather than closed — `fragile`

`src/blackrose/backend/router.py::RouterClient.stream` sets `completed` and keeps reading; `src/blackrose/backend/opencode.py::OpenCodeGoClient.stream`
likewise consumes lines until the body closes. A provider that keeps the
connection open after its finish reason is bounded only by
`budget.duration_seconds` (the `asyncio.timeout_at` in `src/blackrose/backend/stream_limits.py::sse_lines`). Closing the response on
the first finish reason would save that wait but changes who owns the socket and
would drop the usage packet that OpenAI-compatible servers send *after* the
finish reason (`test_usage_packet_after_the_finish_reason_is_delivered_without_content`),
so no change is proposed.

## Formalization decisions

### Applied: refuse block-lifecycle violations in the `messages` branch (H1)

`src/blackrose/backend/opencode_protocols.py::StreamDecoder.anthropic`:

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
- The raises are `RuntimeError`, which `src/blackrose/backend/opencode.py::OpenCodeGoClient.stream` does not swallow
  into "invalid stream response", so the failure names the protocol violation.

No other source change is proposed. In particular the duplicated-terminal and
output-after-finish behaviour (H4) is left to `src/blackrose/turns.py::TurnRunner._stream_round`, because moving
that guard into four decoders would duplicate a policy the turn already owns and
would change the emitted-choice contract for consumers that only read text.

### Proposed, not applied: recover buffered `responses` arguments (H2)

Smallest patch that adds the fallback, in
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
of every `responses` call, which is a transport contract; (b) the existing tests construct `output_item.done` packets
without `output_index` (`tests/test_opencode.py::ProviderTests.test_responses_stream_preserves_encrypted_reasoning_and_call_ids`), so the `call_id` map is
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

Run from the repository root:

```
.venv/bin/python scripts/check_unit_tests.py -p test_sm_04_sse_stream_decoder.py
.venv/bin/python scripts/check_unit_tests.py -p test_telemetry.py
.venv/bin/python scripts/check_unit_tests.py -p test_opencode.py
.venv/bin/python scripts/check_unit_tests.py -p test_stream_outcomes.py
.venv/bin/python scripts/check_unit_tests.py -p test_interactive_ui_evaluation.py
.venv/bin/python scripts/check_unit_tests.py -p test_stream_limits.py
.venv/bin/python -m ruff check src/blackrose/backend/opencode_protocols.py tests/test_sm_04_sse_stream_decoder.py
.venv/bin/python -m mypy
```

The first command runs the tests that pin this machine's transitions and
invariants; the rest are the pre-existing suites for the modules it touches.
Two `test_stream_limits.py` tests,
`tests/test_stream_limits.py::SseLimitTests.test_ui_repair_substream_uses_the_same_turn_byte_budget`
and
`tests/test_stream_limits.py::SseLimitTests.test_ui_repair_failure_logs_only_operation_and_class`,
go through the UI repair path and need the generated `ui-runtime` contract from
[checkout setup](../../CONTRIBUTING.md#set-up-a-checkout).

Regression evidence for H1: with the guard in
`src/blackrose/backend/opencode_protocols.py::StreamDecoder.anthropic` reverted,
four tests fail:

```
test_duplicate_block_start_at_an_open_index_is_rejected
test_duplicate_block_stop_is_rejected
test_block_stop_for_an_unopened_index_is_rejected
test_reusing_a_stopped_index_starts_with_a_clean_argument_buffer
```

Not needed: the native `--gui` probes and any live provider probe. The change is
a pure guard inside one decoder and the protocol matrix is covered offline by
`tests/test_stream_outcomes.py` and `tests/test_opencode.py`.
