---
name: team-wiki
description: Write finished work to the team wiki, an organisation vault on the mindin server. Only for sessions that reach an organisation vault (session start lists "organisation pages", or search rows name an organisation vault); without one, do not use it. Use when a project is closed or archived, when an issue or task closes with something others will need, when a decision is taken at the start of a project, when an incident is closed, or when the user asks to put something in the wiki.
---

# Team wiki

The wiki is an organisation vault that every colleague's Claude reads at session start and in search. It holds **finished work** only: what was built, how to use it, what was decided, what an incident taught. Tasks, plans, logs and work in progress stay in the user's own vault.

## When a page is due

| Moment | Page |
| --- | --- |
| A project is closed or archived | its summary in `Projects/` (what was built and why), and a guide in `Guides/` for what others will use |
| An issue or task closes with something worth keeping | a guide in `Guides/`, or a correction to the area's page in `Areas/` |
| A decision is taken at the start of a project | a page in `Decisions/`: the choice, the alternatives dropped and why, written before the build starts |
| An incident is closed | a review in `Incidents/`: what happened, the cause, the fix, how to avoid it |

An open incident or a project still being built gets its page once it is closed.

## Steps

0. **A wiki to write to.** If no search row and no session-start row names an organisation vault, this session has no wiki: stop here and say nothing about it.
1. **Rules.** Call `rules` with `vault` set to the wiki's slug, as search rows name it (`rules` without `vault` when this session is the wiki). Its folders, required fields and own rules win over this skill. Done when you know the wiki's folders and required fields.
2. **Search.** `search` for the subject in the wiki and read every row that could cover it with `read_file(path, vault=<wiki>)`. Done when you know whether a page already covers it: that page is the one you update, keeping what still holds.
3. **Draft.** Write the page in the folder from the table, with `team`, `author` (the user's name), `area` (its page in `Areas/`, as `"[[stem]]"`), `repos` (the repos it covers) and a one-line `summary`. A project or incident page has status `complete`. Name where a secret lives, such as the secret manager's entry, in place of the secret, and use made-up examples in place of personal data. Done when the draft holds every required field and reads as the present state.
4. **Ask.** Show the whole draft, and its path, to the user and ask whether to write it. Done when the user answered.
5. **Write.** On an explicit yes, `write_file` with `vault` set to the wiki; for an existing page, pass the `etag` from step 2 as `expected_etag`. Any other answer leaves the wiki as it was. When the server refuses the write, fix what its answer names and go back to step 4. Done when the server answers `Written:`.
