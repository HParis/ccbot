# Message Handling

## Message Queue Architecture

Per-**topic** message queues + worker pattern for all send tasks, keyed `(user_id, thread_id_or_0)`:
- Messages are sent in receive order (FIFO) *within* a topic
- Status messages always follow content messages
- Topics (and users) process concurrently without interference

**Why per topic, not per user**: every topic lives in one Telegram supergroup and shares its 20-messages-per-minute budget, so `AIORateLimiter` paces sends at roughly one per three seconds no matter how many topics are active. A single per-user queue handed that budget out strictly first-come — a chatty topic's backlog of tool calls sat in front of every other topic, and a quiet one could go minutes without a word (observed: a picker taking 31s to appear, then 3m40s of silence). Per-topic queues keep the shared budget but let each topic compete for it directly. `clear_topic_state` stops a topic's worker when the topic goes away.

Flood control (`_flood_until`) stays keyed by user: a 429 throttles the whole supergroup, so every topic's worker backs off together.

**Message merging**: The worker automatically merges consecutive mergeable content messages on dequeue:
- Content messages for the same window can be merged (including text, thinking)
- tool_use breaks the merge chain and is sent separately (message ID recorded for later editing)
- tool_result breaks the merge chain and is edited into the tool_use message (preventing order confusion)
- Merging stops when combined length exceeds 3800 characters (to avoid pagination)

## Status Message Handling

**Conversion**: The status message is edited into the first content message, reducing message count:
- When a status message exists, the first content message updates it via edit
- Subsequent content messages are sent as new messages

**Polling**: Background task polls terminal status for all active windows at 1-second intervals. Send-layer rate limiting ensures flood control is not triggered.

**Throttling**: The status line carries a live counter — `Gusting… (1m 45s · ↓ 5.7k tokens)` is a different string every second — so exact-text dedup never hit while Claude worked and every 1s poll became an edit: up to 60 API calls/min per active topic against a 20/min group budget. `enqueue_status_update` now compares `_status_state_key()` (the line with its parenthesised counter and digits stripped): a change of *state* edits immediately, a counter tick waits `STATUS_REFRESH_INTERVAL` (15s) so the seconds still visibly advance. The worker also skips the edit when `last_text` is unchanged.

## API Call Accounting

Every send, edit, delete and chat action spends the same 20-per-minute per-group budget, but only content sends were logged — status edits, tool_result edits and picker renders were invisible, so a topic could go silent with nothing in the log to explain it. `telemetry.py` counts every outbound call: `CountingRateLimiter` (the rate limiter is the one chokepoint every request passes through) records `api:<endpoint>`, and call sites tag semantic kinds (`status:*`, `content:*`, `picker:*`). One summary line per minute lands in the log.

Findings acted on so far:
- **Topic existence probe** (`TOPIC_CHECK_INTERVAL`) was one call per bound topic per minute — 7/min with 7 topics, 35% of the budget. Raised to 300s; a deleted topic is gone either way, so noticing it later costs nothing.
- **tool_result edits no longer churn the status message.** That branch edits the tool_use message already sitting above the status, so nothing moves — the old clear-then-repost spent two calls per tool call for no visible change.

## Rate Limiting

- `AIORateLimiter(max_retries=5)` on the Application (30/s global)
- On 429, AIORateLimiter pauses all concurrent requests (`_retry_after_event`) and retries after the ban
- On restart, the global bucket is pre-filled (`_level=max_rate`) to avoid burst against Telegram's persisted server-side counter
- Status polling interval: 1 second (skips enqueue when queue is non-empty)

## Performance Optimizations

**mtime cache**: The monitoring loop maintains an in-memory file mtime cache, skipping reads for unchanged files.

**Byte offset incremental reads**: Each tracked session records `last_byte_offset`, reading only new content. File truncation (offset > file_size) is detected and offset is auto-reset.

## No Message Truncation

Historical messages (tool_use summaries, tool_result text, user/assistant messages) are always kept in full — no character-level truncation at the parsing layer. Long text is handled exclusively at the send layer: `split_message` splits by Telegram's 4096-character limit; real-time messages get `[1/N]` text suffixes, history pages get inline keyboard navigation.
