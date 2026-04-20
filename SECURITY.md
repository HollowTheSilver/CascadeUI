# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 3.x     | Yes       |
| < 3.0   | No        |

Only the latest release receives security fixes. Upgrade to the current
version before reporting.

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

Use [GitHub's private security advisory feature](https://github.com/HollowTheSilver/CascadeUI/security/advisories/new)
to report vulnerabilities confidentially. This keeps the details private
until a fix is released.

If you cannot use the advisory feature, contact the maintainer directly
via GitHub.

### What to Include

- A description of the vulnerability and its potential impact
- Steps to reproduce, including a minimal code snippet if possible
- The CascadeUI version, Python version, and discord.py version affected

### Response Timeline

- **Acknowledgement**: within 72 hours
- **Assessment and fix plan**: within 1 week
- **Patch release**: as soon as a fix is verified

## Scope

CascadeUI is a UI framework that runs inside a Discord bot process. It does
not handle authentication, network connections, or user credentials directly.
Security concerns most likely involve:

- **State injection** through crafted interaction payloads, `custom_id`
  forgery, or action-type collisions in user-registered reducers.
- **Persistence boundaries.** CascadeUI partitions persisted data across
  two namespaces: `registry` (view reattachment metadata) and `application`
  (app state, with scoped user/guild data riding under it). Nothing persists
  by default - views opt in per-slot via `persistent_slots`. A vulnerability
  here would involve data crossing a scope boundary it should not cross
  (e.g. one user reading another user's scoped state).
- **Persistence backends** exposed without access controls. The shipped
  `SQLiteBackend` writes to a local file; `InMemoryBackend` is
  process-local; third-party backends implementing the
  `PersistenceBackend` Protocol are outside the library's trust boundary.
  Report issues in shipped backends; issues in third-party backends belong
  to those projects.
- **Information leakage** through ephemeral message handling, including
  token-refresh handoffs, `_reopen_ephemeral` factories, and the
  armed-refresh-view freeze window.
- **Access-control bypass** in `owner_only`, `allowed_users`,
  `interaction_check`, or the participant-registration path.

If you are unsure whether something qualifies as a security issue, report it
privately and let the maintainer assess.
