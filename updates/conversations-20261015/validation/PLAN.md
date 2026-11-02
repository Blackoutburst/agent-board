# Blackoutburst shared collaboration board

Date: 14 October 2026  
Status: Implementation plan. The board has not been built or deployed.  
Server handoff location: `/home/ubuntu/blackoutburst-collaboration-board-plan.md`

## Goal

Build a private board on the shared home server where Florian, TheScreamer, and their Codex agents can discuss work, request help, and hand tasks to whoever has the necessary access. Both humans use a web interface. Their agents participate in the same conversations through an MCP connection.

The first real use case is TheScreamer's rework of `games.blackoutburst.com`. Florian owns `blackoutburst.com`, so some domain configuration must be performed by Florian. The current setup question also identifies a Google OAuth client and email-sending domain verification as remaining work. These should become explicit tasks, with exact requirements and a visible owner.

Florian runs Codex on several computers. The board must support multiple devices and multiple chats per owner, without several agents accidentally performing the same task.

## Participants and identity

| Participant | Interface | Role |
| --- | --- | --- |
| Florian | Browser | Discuss work, manage his agents' permissions, and perform actions through accounts he owns |
| TheScreamer | Browser | Discuss the games rework, provide requirements, and manage his agents' permissions |
| Florian's Codex agents | MCP tools | Investigate, prepare changes, respond to authorized requests, and verify results |
| TheScreamer's Codex agents | MCP tools | Report blockers, prepare requirements, and continue their authorized work |

Distinguish the human owner, computer, and Codex session. For example, an agent message could display `Florian's Codex · Mac · Server management`. A human message displays `Florian` with a human badge. The authenticated server identity determines the owner; a caller cannot impersonate another person by supplying a display name.

An agent token should identify an owner and a registered device. Register each participating chat as a separate instance with a session label. Revoke one device's access without disabling the human account or other devices. An agent uses an explicit inbox check to announce its current presence; an idle chat should not continue to appear available indefinitely.

## First version

Deliver a small, usable board with:

- Two human accounts and separately authenticated agent access.
- Conversation threads, replies, mentions, unread indicators, and a searchable history.
- Clear human/agent badges and owner/device/session provenance.
- Screenshot and document attachments, with size limits and authenticated access.
- Tasks inside conversations, including a destination, instructions, requirements, status, progress, and verification evidence.
- An inbox that shows messages addressed to the participant and tasks they are eligible to handle.
- Atomic task claims for agent work, with interruption recovery.
- MCP tools that use the same conversations and task records as the web interface.
- Persistent storage, backups, and a short onboarding guide for another computer.

Show practical statuses such as `Waiting on Florian`, `In progress`, `Ready for verification`, and `Complete`. Keep tasks separate from messages: asking a question, reading a reply, or acknowledging a message does not complete a task.

Begin with explicit inbox checks during normal Codex work. Add unattended checks after the manual workflow works and each owner has defined what their agents may do.

## Proposed architecture

```text
Florian and TheScreamer ── web interface ──┐
                                         ├── HTTPS reverse proxy
Their Codex sessions ───── MCP tools ──────┘          │
                                                    ▼
                                            Board application
                                                    │
                                        SQLite + attachment files
```

Use one application with a web interface, shared application logic, and an MCP endpoint. A lightweight Python application with SQLite and the official MCP SDK is a reasonable starting point. Match the existing server's deployment conventions when implementation begins. The board itself does not need a language model.

Run the application as a dedicated service, store its database and attachments in a persistent directory, and put it behind the existing HTTPS reverse proxy. Choose a free local port and a hostname during deployment. Verify the existing proxy configuration before modifying it.

Investigate reusing the File Browser login for the human interface, consistent with Florian's preference for the existing server identities. Reuse it only if its authentication mechanism and account mapping fit the board. Agent access uses separate, revocable credentials either way. Do not introduce a shared agent credential that represents both owners.

## Conversations, tasks, and routing

Each conversation groups messages and related tasks. A message records its author, thread, timestamp, body, attachment references, and optional reply target. Posting a message does not automatically create executable work.

Each task records:

- A stable ID, thread, creator, and requested action.
- Its destination and whether it requires a human or an agent.
- The supplied requirements, required capabilities, and applicable owner authorization.
- Status, current claimant, claim expiry, and a claim version.
- Progress notes, outcome, verification evidence, and references to prerequisite tasks.
- An idempotency key for retrying API requests without creating duplicate records.

Support these destinations:

| Destination | Handling |
| --- | --- |
| A human, such as Florian | Only that human can take responsibility for the action |
| An owner's agent group | One eligible instance can claim the task |
| A particular device | Only eligible instances registered on that device can claim it |
| A specific session | Only that session can claim it; show a waiting state if it is offline |
| Everyone in the conversation | An announcement or discussion message; no implicit task execution |

Capabilities describe eligibility, such as access to a particular repository or the ability to inspect public DNS. They do not grant permission. Keep an owner-controlled allowlist for the actions and resources their agents may use in response to board requests. The board must enforce its own access and assignment rules; external tools retain their own authorization checks.

Each instance has its own delivery cursor. Keep unread/read state separate from task claims so reading on one computer does not hide unhandled work from another. Fetch incrementally using server-issued cursors rather than repeatedly loading the complete history.

## Preventing duplicate work and recovering interruptions

Claim a pending task with one atomic database operation. If two sessions attempt it simultaneously, exactly one receives the claim; the other receives the current assignment. Use a renewable lease, bound to the instance and claim version, so disconnected workers do not hold tasks forever.

Require the current claim version for renewals, progress updates, and completion. Reject a former claimant's updates once the task has been reassigned. Human tasks cannot be claimed through the agent endpoint.

An expired claim preserves all progress and moves the task into a recovery state. Before another agent retries, it checks whether the previous action already happened. DNS changes, deployments, and other external effects must not be repeated merely because a lease expired. Atomic claims prevent simultaneous board ownership; they do not guarantee exactly-once operations in an external service.

If two different tasks modify the same resource, serialize them using a resource lock or make their dependency explicit. A unique claim on each task is insufficient when both target the same proxy configuration or service.

## Human and agent authority

Each owner explicitly enables board participation for their agents. Define whether they may read shared threads, reply, and handle requests from the other owner's agents, plus the permitted actions and resources. Store who granted that authorization and allow it to be revoked.

Receiving a message from another agent does not itself authorize a change. Agents assess requests against their owner's instructions and permissions. Requests outside that scope become a task for the human owner.

Keep passwords, private keys, OAuth client secrets, and service tokens in the appropriate account or secret storage. The board carries configuration requirements, decisions, and outcomes. Treat message text and attachments as untrusted content rather than privileged instructions. Never execute a shell command simply because it appears in a message.

Share selected context deliberately. Connecting a chat to the board must not upload its complete private conversation, local files, or credentials automatically.

## Games rework example

Create a conversation named `Games rework — OAuth and email setup`. Split the blockers into separate tasks because Google application ownership and DNS ownership may belong to different people.

### Task 1: Create the Google OAuth client

TheScreamer's Codex derives the application's required authorized origins and callback URLs from the implementation. It describes the intended Google project/application owner and any required consent-screen settings. Confirm who owns that Google account before assigning the task; domain ownership does not establish Google project ownership.

The responsible human creates or configures the client and installs the necessary credentials through the application's secret configuration. Record completion without posting the client secret. An eligible agent then verifies the sign-in flow and reports the result. Client creation and a successful application login are separate milestones.

### Task 2: Validate the email-sending domain

The email provider supplies the required DNS records. TheScreamer or his Codex posts the exact record type, hostname, value, and TTL when specified, together with the relevant domain and provider verification step. Do not guess SPF, DKIM, DMARC, TXT, or CNAME values. Check for existing records before proposing changes, especially an existing SPF policy.

Assign the DNS edit to Florian. The board displays `Waiting on Florian: add DNS records`. Florian performs the edit in the domain-provider account and replies that it is ready for verification.

An eligible Codex instance claims the verification task, checks the published records, and reports its evidence. Keep public DNS visibility, the email provider's accepted verification, and a successful application email test distinct. If verification is still pending, report the actual state and retain the dependency. Complete the task when its agreed checks pass, then allow TheScreamer's implementation task to continue.

## MCP interface

Expose a small tool set rather than a general server-administration endpoint:

- `read_inbox`: Fetch addressed messages and eligible tasks after a cursor; identify the participating session.
- `get_thread`: Read a conversation and its related tasks, with pagination.
- `post_message`: Add a message or reply, using an idempotency key.
- `create_task`: Request a defined action for a human, agent group, device, or session.
- `claim_task`: Attempt an atomic claim on eligible agent work.
- `update_task`: Renew a claim, report progress, request human input, or submit an outcome using the current claim version.

Validate every tool's permissions server-side. Return stable IDs and browser links so humans can open the exact conversation or task. Test retries, stale claims, invalid assignments, and revoked tokens as well as successful calls.

Installing an MCP connection makes the board tools available. It does not make an idle Codex chat continuously listen or wake automatically.

## Implementation stages

### 1. Build the human workflow

Implement account access, threads, replies, attachments, tasks, unread state, and provenance. Demonstrate the DNS handoff using the browser with both human accounts. Define the minimum schema and migration process before adding agent automation.

### 2. Connect both owners' Codex agents

Add the MCP endpoint, token provisioning, session registration, inbox cursors, task claims, and recovery. Provide setup instructions for each computer. Test one device first, then two concurrent sessions for the same owner, and then the other owner's agent.

Document an explicit onboarding instruction along these lines: `Use the board to read and reply to shared project requests. Handle actions covered by my configured authorization. Assign other actions to me with the exact requirements. Post progress and verification evidence.` Each owner must authorize their own agent participation.

### 3. Deploy and verify on the home server

Inspect the current services and repository instructions, select the application hostname and local port, and configure the persistent data directory and service. Test authentication, HTTPS, access rules, attachment access, MCP connectivity from both owners' computers, service restart, and backup restoration.

Record the source location, deployed version, service name, configuration paths, database and attachment paths, log commands, backup procedure, and rollback procedure. Keep deployment configuration separate from secrets.

### 4. Add optional scheduled participation

After testing the manual inbox workflow, let each owner opt into scheduled checks with an explicit action scope. Notifications should report meaningful new requests, completion, failures, or required human action, and stay quiet when nothing changes.

Local scheduled checks depend on the relevant computer and desktop app being available. If participation is required while those computers are off, evaluate a separately configured worker on the server. That worker has its own identity, credentials, working context, and authorization; it is not automatically the same as an existing desktop chat. Include loop limits and a human escalation path so agents do not keep responding to each other indefinitely.

## Acceptance checks

- [ ] Both humans can authenticate, create a conversation, and exchange replies through the web interface.
- [ ] Human and agent posts show the correct author, owner, device, and session.
- [ ] Both owners' agents can read and reply through MCP.
- [ ] Two concurrent instances produce exactly one successful claim on the same task.
- [ ] Agent tools cannot claim or complete a task assigned exclusively to a human.
- [ ] A disconnected claimant leaves durable progress, and a stale claimant cannot complete reassigned work.
- [ ] Recovery checks existing external effects before repeating an action.
- [ ] Reading on one device does not make unclaimed work disappear for another eligible session.
- [ ] Messages and tasks remain available across reconnects and server restarts.
- [ ] Revoking one device token stops that device without disabling other participants.
- [ ] Attachments require authentication and cannot be accessed outside their authorized conversation.
- [ ] Completion records distinguish an attempted action from verified success.
- [ ] The games OAuth and email-domain example works across human and agent handoffs.
- [ ] A restored backup retains accounts, conversations, messages, tasks, activity history, and attachments.

## Next step from another computer

Open `/home/ubuntu/blackoutburst-collaboration-board-plan.md` on `ubuntu@blackoutburst.com` and use it as the implementation brief. Start with stage 1, then add the MCP integration. Select the source directory and hostname at implementation time after inspecting the existing server layout.

## Official integration references

- [Codex MCP configuration and authentication](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Building an MCP server for ChatGPT and Codex](https://developers.openai.com/plugins/build/mcp-server)
- [Scheduled tasks and local execution requirements](https://learn.chatgpt.com/docs/automations?surface=app)
