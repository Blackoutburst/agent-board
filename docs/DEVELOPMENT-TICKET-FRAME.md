# Standalone Development ticket observations

Board 2.17.3/schema 17 and adapter 0.34.0 add
`get_development_ticket_frame(session_id, ticket_id)`. The two arguments are
required; additional fields are refused. It is an authenticated read for the
current standalone agent/profile/device/session and selected project/ticket.
Tickets attached to a delegation family use their existing family protocol.

The response has schema `blackoutburst-development-ticket-frame-v1`, with
`selection` fixing owner, agent, device, profile version, project, ticket and
session, and `selected` containing `policy_first`, `first`, `pages`, `last` and
`policy_last`. Policies use the normal effective profile policy; tickets and
thread pages retain the normal full read shapes. At most three history pages
and 40 unique messages are collected, with a 384 KiB data bound and a 1536 KiB
wire bound. An unavailable or inconsistent observation is refused.

The server authenticates each read and rechecks selection at the end. These
are sequential observations, not an atomic database snapshot or a reusable
grant. The SDK validates the closed envelope, both policies, ticket versions,
history pagination and context/source hashes with its existing composer.
The endpoint performs no registration, claim, lease renewal, queue sweep,
legacy expiration maintenance, notification or execution.

Development clients probe the exact tool signature when registering or adopting
their current registration. Missing tools, malformed responses and failed reads
stop the operation; there is no fallback to the previous multi-request path.
Family clients retain their existing frame and authorization guards.

The combined read removes network round trips from native qualification and
from authorization of named tests after the model. The native 1.5-second control
window, SOURCE release checks and revocation checks remain unchanged. An
observation that arrives after the preceding deadline remains expired.
Installation or synthetic tests do not establish live cloud execution success.
