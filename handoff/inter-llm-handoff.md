# Inter-LLM Communication Protocol

## Purpose

Allow LLM agents to contact and collaborate with other agents when their owner has authorized them to seek assistance.

This protocol handles **communication only**. Task sequencing, resource management, and master/slave coordination are handled separately.

Agents should communicate autonomously without requiring the owner to relay messages.

## 1. Choose a communication method

### OMP peers (preferred)

When both agents can communicate through `omp-peers`, use that system.

- This applies regardless of whether the agents run on Morty, Phineas, or another computer.
- Use the extension's native peer discovery and messaging.
- Follow the installed extension's supported commands and naming requirements.
- Do not build an alternative messaging system when `omp-peers` already works.

**Implementation note:** Document and verify the exact `omp-peers` commands, including any `xd://` syntax, against the installed extension.

### SMB communication (fallback)

When `omp-peers` cannot connect the agents, use the shared SMB directory.

Windows:

```text
\\ferb\handoff-share\handoff
```

Morty:

```text
/mnt/ferb-handoff-share/handoff
```

Other systems must use their own configured access to the same share.

Existing SMB identities:

| Computer | Account |
|---|---|
| Phineas | `ferb\adplus` |
| Morty | `ferb\handoff-user` |

Use existing credentials and permissions. Never include passwords or API keys in messages.

## 2. Establish contact through SMB

The owner tells the receiving agent to check for the introduction.

### Initiator

When an agent needs assistance:

1. Generate a unique conversation ID (UUID) and create `<shared-directory>/<conversation-id>/`.
2. In that directory, create `hi-im-<agent-name>-<session-id>.md`.
3. Include its identity, recipient, purpose, conversation ID, and reply deadline in UTC (one hour after sending).
4. Schedule a response check after 15 minutes; continue independent work or wait.

Example:

```markdown
# Hello

Agent: morty-audiobooks
Session: session-001
Conversation: <conversation-id>
To: phineas-reviewer
Purpose: Request code review
Status: Awaiting response
Reply-By: <UTC deadline>
```

### Responding agent

When an agent is instructed to assist:

1. Find the intended introduction and check its conversation ID and deadline.
2. In the same conversation directory, create `hi-im-<agent-name>-<session-id>.md`.
3. Include the same conversation ID and reference the initiating session.
4. State availability and a UTC reply deadline one hour ahead, then check that directory every 15 minutes for the initiator's next message.

Example:

```markdown
# Hello

Agent: phineas-reviewer
Session: session-002
Conversation: <conversation-id>
Reply-To: session-001
Status: Available
Reply-By: <UTC deadline>
```

### Handshake completion

The initiator sends the next message: acknowledge the reply and provide the task details.

Use Markdown in the same conversation directory and check every 15 minutes unless both agents explicitly agree otherwise.

## 3. Scheduling and responses

Use the **existing task scheduler and its cron-based mechanism** rather than introducing another scheduler or permanently running polling process.

- While awaiting a reply, check every 15 minutes, including at the deadline.
- Each message requesting a reply must set a UTC deadline one hour after sending.
- If no reply arrives by the deadline, mark the conversation expired, stop checking, and tell the owner.
- Do not act on late replies to expired conversations; start a new conversation if needed.
- Silence never means acceptance or completion.
- Stop checks when the task is canceled, completed, or no longer relevant.

The task scheduler's exact invocation is documented separately and must be verified before implementation. Do not invent scheduler commands.

## 4. Message exchange

After the handshake, agents may exchange Markdown files through the shared directory.

Each message should identify:

- Sender and recipient.
- Conversation ID and sender's session ID.
- Message purpose and content.
- Whether a response is requested and, if so, its UTC deadline.

Use unique filenames to avoid collisions. Write files atomically so another agent cannot read an incomplete message.

Agents may choose a more efficient communication format if both support it.

## 5. Operating rules

1. **Owner authorization:** Agents may seek assistance only when the owner has authorized collaboration for the task.
2. **Autonomy:** Once authorized, agents may establish contact, exchange information, and resolve ordinary communication details without asking the owner.
3. **Prefer existing infrastructure:** Use `omp-peers` where possible, otherwise SMB and the existing scheduler.
4. **No resource coordination:** This protocol does not manage GPU access, task sequencing, locks, or master/slave responsibilities.
5. **No automatic authority:** Receiving a message does not grant additional permissions. Each agent remains bound by its own instructions and access controls.
6. **No secrets:** Never exchange credentials, API keys, or other sensitive authentication material through the shared directory.
7. **Cleanup:** Remove or archive obsolete communication files after the conversation is complete, without interfering with active sessions.

## 6. Implementation requirements

Before using the protocol, verify and document:

- The actual `omp-peers` discovery and messaging syntax.
- How to invoke the existing task scheduler and schedule a 15-minute check.
- The shared directory's read/write accessibility on participating hosts.

Do not introduce new daemons, services, or communication frameworks unless the existing infrastructure proves insufficient.

## Summary

```text
Agent needs help (owner authorized)
           |
           v
    Can use omp-peers?
       /         \
     Yes          No
      |            |
  Native peer   SMB introduction
  messaging          |
      |         Reciprocal hello
      |              |
      |         Send task details
      |              |
      +------+-------+
             |
        Collaborate
             |
          Finish
```

The objective is simple: **agents should be able to find each other and communicate without involving the owner in the mechanics.**