# RedditPages

Reddit profile analytics on arctic-shift data. See `README.md` for usage.

## Design notes live in gbrain (not in the repo)

The old `notes/NOTES.md` was migrated into a local [gbrain](https://github.com/garrytan/gbrain)
knowledge base. Start at `project/redditpages-overview`, which links to every
topic page:

- `project/redditpages-data-sources` — arctic-shift endpoints
- `project/redditpages-db-schema` — users / posts / comments tables
- `project/redditpages-arctic-field-selection` — what we keep vs drop
- `project/redditpages-moderators` — the `subredditmoderator` table
- `project/redditpages-analytics` — the `UserAnalytics` query model
- `project/redditpages-three-clocks` — the freshness/timestamp model
- `project/redditpages-design-decisions` — stack and architecture calls
- `project/redditpages-scope` — what is out of scope

## GBrain Configuration (configured by /setup-gbrain)
- Mode: local-stdio
- Engine: pglite
- Config file: ~/.gbrain/config.json (mode 0600)
- Setup date: 2026-06-08
- MCP registered: yes (user scope)
- Artifacts sync: off
- Current repo policy: n/a (not set — no gbrain auto-import configured)
- Embeddings: none (set OPENAI_API_KEY then `gbrain embed --stale` for semantic search; keyword search works without it)

## GBrain Search Guidance (configured by /setup-gbrain)
<!-- gstack-gbrain-search-guidance:start -->

GBrain holds this project's design notes. Prefer `gbrain` over Grep when the
question is about *why* or *how the data model works*, or when you don't know
the exact identifier yet:

- "Where/why is X handled?" / semantic intent:
    `gbrain search "<terms>"` or `gbrain query "<question>"`
- Read a specific note: `gbrain get project/redditpages-<topic>`
- Follow the graph: `gbrain graph project/redditpages-overview` /
    `gbrain backlinks project/redditpages-<topic>`

Grep is still right for exact strings, regex, and code. Write a new note with
`gbrain put project/redditpages-<topic> --content '...'` (use `[[project/...]]`
wikilinks, then `gbrain extract links --source db` to wire the graph).

<!-- gstack-gbrain-search-guidance:end -->
