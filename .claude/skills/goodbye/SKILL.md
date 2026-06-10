---
name: goodbye
description: Use when wrapping up or ending a Claude Code session (user says "goodbye", "/goodbye", "log this session", "save learnings", "write up what we did") to distill the session's durable, reusable LEARNINGS into the user's Obsidian vault as polished evergreen notes — and optionally a lightweight session summary.
---

# Goodbye — Distill Session Learnings → Obsidian

The user cares most about **durable, reusable learning points** — not session diaries. The primary job of this skill is to mine the session for genuinely useful, evergreen insights and write them as **atomic learning notes** in the vault's `Learnings/` folder. A session summary is **secondary** and optional.

This is a **flexible** skill. Quality over quantity: a few sharp, reusable notes beat a long transcript. **Never invent facts** — only record what actually happened and what was genuinely learned.

## Vault Location

- **Vault root:** `C:\Users\kakha\Dev\notes`
- **Learnings (PRIMARY output):** `C:\Users\kakha\Dev\notes\Learnings\` — one note per concept, concept-titled (e.g. `Helm Release Name Suffixes & Duplicate Config Drift.md`). **Not** dated.
- **Session summaries (SECONDARY, optional):** `C:\Users\kakha\Dev\notes\Claude Sessions\` — `YYYY-MM-DD-<slug>.md`.

The vault has **no community plugins** — do **NOT** use Dataview. Use only native Obsidian features (below).

## Process

Create a TodoWrite list and work through it:

1. **Mine the session for learnings.** Scan the whole conversation. For each candidate insight ask: *Is this reusable beyond this one task? Would future-me, out of context, find it valuable?* Keep only those. Good sources: non-obvious gotchas + their root cause, mental models that clicked, recipes that worked, tool/system behaviors that surprised you, design principles earned the hard way.
2. **Check existing `Learnings/` first.** Read `Learnings/README.md` (the MOC) and any same-topic notes. **Extend or link** existing notes rather than duplicating. If another session already covered a learning, don't rewrite it — link to it.
3. **Write each new learning** as an atomic note in `Learnings/` (template + convention below).
4. **Update `Learnings/README.md`** — add a row to the notes table (one-liner per note) and any new topic tags.
5. **(Optional) Write a session summary** in `Claude Sessions/` only if the session had a meaningful arc worth a timeline. Keep it light and link out to the learning notes it produced. Skip it for small sessions. Ensure `Claude Sessions/` scaffolding (MOC + `.base`) exists if you write one — see end of file.
6. **Capture outstanding todos.** If the session leaves genuine open action items or follow-ups, add them to the vault's todo area — but only if they aren't already recorded there. Don't manufacture busywork; skip when there's nothing real to track.
7. **Report** what was written: the learning notes (the headline), any todos captured, and the summary if any.

## `Learnings/` Convention (match the existing notes exactly)

- **Filename = the concept**, Title Case, no date. Pick a title that reads as a reusable idea.
- **Frontmatter:**
  ```yaml
  ---
  title: {{Same as filename}}
  tags:
    - learnings
    - {{topic}}        # e.g. git, kubernetes, helm, gitops, ci, observability, workflow, claude, aws
  created: {{YYYY-MM-DD}}
  severity: {{optional flavor: bit-me-once | bit-me-twice | design-smell | dont-wait-forever}}
  ---
  ```
- **Body:** lead with the core insight, then make it self-contained — the *why* and the exact recipe, not just the headline. Use Obsidian features liberally:

| Feature | How |
|---|---|
| **Callouts** | `> [!danger]`/`> [!warning]` the trap; `> [!tip]` the rule of thumb; `> [!check]` verification; `> [!example]` snippets; `> [!info]` context |
| **Mermaid** | ` ```mermaid ` for flows/relationships when a picture helps |
| **Code fences** | always language-tagged (` ```bash `, ` ```yaml `, ` ```python `) |
| **Wikilinks** | cross-link sibling learnings `[[Other Learning]]` and end with `- [[README|Learnings Home]]`; dangling links are fine (they seed future notes) |
| **Tables** | for comparisons / "what looks like X vs Y" |
| **Tags** | `#learnings` always + nested/topic tags so the tag pane is useful |

Then add the note to the `## 📚 Notes` table in `Learnings/README.md` with a crisp one-liner.

## Session Summary (secondary) — `Claude Sessions/YYYY-MM-DD-<slug>.md`

Only when warranted. Lighter than a learning note: frontmatter (`date`, `tags: [claude-session, topic/...]`, `project`, `repo`, `branch`, `pr`, `status`, `up: "[[Claude Sessions]]"`), a `> [!abstract]` TL;DR, a short "what happened" timeline, and a **Related** section that links the learning notes this session produced. Don't duplicate the learnings inline — link to them.

### Claude Sessions scaffolding (create if missing, idempotent)

`Claude Sessions/Claude Sessions.md` (MOC):
````markdown
---
title: Claude Sessions — Index
tags: [moc, claude-session-index]
up: "[[README]]"
---
# 🤖 Claude Sessions
Timeline of Claude Code working sessions. The durable takeaways live in [[README|Learnings]].

```base
filters:
  and:
    - file.hasTag("claude-session")
    - file.name != "Claude Sessions"
views:
  - type: table
    name: Sessions
    order: [file.name, date, project, status]
    sort:
      - property: date
        direction: DESC
```

```query
tag:#claude-session -path:"Claude Sessions/Claude Sessions"
```
````

`Claude Sessions/Claude Sessions.base`:
```yaml
filters:
  and:
    - file.hasTag("claude-session")
    - file.name != "Claude Sessions"
views:
  - type: table
    name: All sessions
    order: [file.name, date, project, status]
    sort:
      - property: date
        direction: DESC
```

Ensure the root `README.md` structure table links both `[[Claude Sessions]]` and the `Learnings/` MOC (add rows only if missing).
