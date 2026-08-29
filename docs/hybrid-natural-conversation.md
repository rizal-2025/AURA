# Hybrid natural conversation

AURA routes each message through a deterministic-first boundary. Existing
handoff locks and active reservation workflows are authoritative. Outside an
active workflow, explicit create, view, update, and cancel requests use the
existing reservation agents; explicit human-assistance requests use the
controlled handoff path; greetings remain deterministic; and conservatively
recognized synthetic gibberish returns the localized friendly fallback without
calling an AI provider. Only the remaining general-conversation route can
generate free-form text.

`GeneralConversationService` reuses the configured AURA `AIProvider`. It is a
text-only component and receives no database, reservation repository, tool
manager, filesystem, shell, scheduler, web, or secret dependency. Its prompt
states that reservation operations belong to the deterministic application and
that generated text must never claim an operation succeeded. Demo reservations
are described as portfolio demonstrations rather than real-world bookings, and
the service does not claim a real operator or live-data connection.

The selected request locale is authoritative: `id-ID` requests ask for natural
Indonesian and `en-US` requests ask for American English. Switching locale does
not reset the session, reservation workflow state, or stored historical
messages.

For the internal portfolio demo, previously persisted, safety-versioned chat
messages are read only for the resolved internal demo session. The model gets
at most the newest 8 messages and 4,000 history characters; each historical
message is capped at 1,000 characters. Current chat input remains subject to the
existing API input limit. User text and history are serialized as untrusted JSON
data, never promoted to system instructions.

General responses request at most 300 output tokens and reject empty, invalid,
or responses longer than 2,000 code points. The existing provider timeout and
zero-retry SDK policy remain in force, with the existing 30-second overall demo
turn timeout as a second bound. Timeout, network, provider, empty-response, and
invalid-response failures return a localized safe message. Logs contain only
status, locale, elapsed time, history count, and a safe exception class; message
content, credentials, session tokens, and raw provider errors are not logged.

No Website/BFF request or response field changes are required. The existing
message, locale header, and resolved demo session supply all routing and context
needed by the backend.
