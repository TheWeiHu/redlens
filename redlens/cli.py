from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from redlens import __version__, discovery, onboarding
from redlens.analytics import compute_user_analytics
from redlens.config import llm_api_key, resolve_db
from redlens.constants import SUMMARY_DEFAULT_DEPTH, SUMMARY_DEPTHS
from redlens.db import connect, init_schema, session
from redlens.doctor import run_doctor
from redlens.errors import MissingKey, NotFound, RedlensError
from redlens.ingest import sync_user
from redlens.models import Profile
from redlens.reporting import explore
from redlens.reporting.page import render_topic_page
from redlens.summarize import summarize_user
from redlens.topics import (
    SubredditCandidate,
    get_topic,
    pull_topic_comments,
    query_terms,
    search_subreddits,
    track_topic,
)

# Discovery sources for a topic's subreddit net, in display order.
# (key, label, on by default)
SOURCES = (
    ("name", "subreddits whose name matches (keyless, via arctic)", True),
    ("global", "subreddits with matching posts (keyless, via PullPush)", True),
    ("web", "web search (DuckDuckGo; may hit bot walls)", False),
    ("popular", f"cast over the {len(discovery.POPULAR_SUBREDDITS)} most "
                "popular subreddits", False),
    ("llm", "LLM suggestions", False),
)


def _ts(s: int | None) -> str:
    if not s:
        return "—"
    return datetime.fromtimestamp(s, tz=UTC).strftime("%Y-%m-%d %H:%MZ")


def _slug(name: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", name.lower())) or "topic"


# Demographic fields in display order, with their headings.
_DEMOGRAPHIC_FIELDS = (
    ("gender", "Gender"), ("age_range", "Age"), ("country", "Country"),
    ("state", "State"), ("city", "City"),
)


def _format_profile(p: Profile) -> str:
    """Render a structured Profile as readable terminal text (reasons are in
    ``--json``; here we show the labels + confidences)."""
    lines = [f"u/{p.username} (via {p.model}, {p.depth} depth):", ""]
    for field, heading in _DEMOGRAPHIC_FIELDS:
        guesses = p.demographics.get(field) or []
        if guesses:
            joined = ", ".join(f"{g.label} ({g.confidence}%)" for g in guesses)
            lines.append(f"  {heading + ':':<9}{joined}")
    if p.big_five:
        lines += ["", "  Big Five: " + " · ".join(
            f"{trait.capitalize()} {t.score}%" for trait, t in p.big_five.items())]
    for heading, body in (("Interests", p.interests), ("Beliefs", p.beliefs),
                          ("Tone", p.tone)):
        if body:
            lines += ["", f"{heading}: {body}"]
    return "\n".join(lines)


def _resolve_sources(sources_arg: str | None, *, assume_yes: bool) -> list[str]:
    """Discovery sources for a new topic: an explicit --sources list wins
    (the only way to request web/global/llm non-interactively), otherwise
    fall back to the interactive picker / name-only default."""
    if sources_arg is None:
        return _choose_sources(assume_yes=assume_yes)
    valid = {key for key, _, _ in SOURCES}
    chosen: list[str] = []
    unknown: list[str] = []
    for tok in (t.strip() for t in sources_arg.split(",")):
        if not tok:
            continue
        (chosen if tok in valid else unknown).append(tok)
    if unknown:
        print(f"warning: ignoring unknown --sources: {', '.join(unknown)} "
              f"(valid: {', '.join(k for k, _, _ in SOURCES)})", file=sys.stderr)
    if not chosen:
        raise RedlensError(
            f"--sources {sources_arg!r} has no valid source "
            f"(valid: {', '.join(k for k, _, _ in SOURCES)})")
    return chosen


def _choose_sources(*, assume_yes: bool) -> list[str]:
    """Ask which discovery sources to use for a new topic's net.

    Non-interactive runs and --yes use name matching only — the other
    sources (web scrape, 100-subreddit cast, paid LLM call) are opt-in
    choices a human should make, via the picker or --sources.
    """
    if assume_yes or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return ["name"]

    llm_ready = llm_api_key() is not None
    print("how should redlens find subreddits?", file=sys.stderr)
    for i, (key, label, default) in enumerate(SOURCES, 1):
        mark = " *" if default else ""
        note = "" if key != "llm" or llm_ready else \
            "  (needs an LLM API key — not configured)"
        print(f"  [{i}] {label}{mark}{note}", file=sys.stderr)
    print('sources ("1 2 4"), Enter for defaults (*), "s" skips discovery',
          file=sys.stderr)
    print("> ", end="", file=sys.stderr, flush=True)
    line = input().strip().lower()

    if line == "s":
        return []
    if not line:
        chosen = [key for key, _, default in SOURCES if default]
    else:
        chosen = [SOURCES[int(tok) - 1][0] for tok in line.split()
                  if tok.isdigit() and 1 <= int(tok) <= len(SOURCES)]
    if "llm" in chosen and not llm_ready:
        print("  skipping llm: set OPENAI_API_KEY/REDLENS_LLM_API_KEY or "
              "[llm] api_key in config.toml", file=sys.stderr)
        chosen.remove("llm")
    return chosen


def _gather_candidates(
    terms: list[str], sources: list[str]
) -> tuple[list[SubredditCandidate], list[str]]:
    """Run the chosen discovery sources, fanned across all query terms.

    Returns (candidates for the picker, net additions that bypass it —
    the popular-subreddits cast is all-or-nothing, not row-by-row).
    """
    merged: dict[str, SubredditCandidate] = {}

    def add(candidates: list[SubredditCandidate]) -> None:
        for c in candidates:
            existing = merged.get(c.name.lower())
            if existing:
                if c.source not in existing.source:
                    merged[c.name.lower()] = dataclasses.replace(
                        existing, source=f"{existing.source}+{c.source}")
            else:
                merged[c.name.lower()] = c

    if "name" in sources:
        for term in terms:
            add(search_subreddits(term))
    for key, fetch in (("global", discovery.search_global),
                       ("web", discovery.search_web),
                       ("llm", discovery.suggest_llm)):
        if key not in sources:
            continue
        names: list[str] = []
        try:
            if key == "llm":  # one call covers every term
                names = fetch(", ".join(terms))
            else:
                for term in terms:
                    names += fetch(term)
        except RedlensError as exc:
            print(f"warning: {key} discovery failed: {exc}", file=sys.stderr)
        if not names:
            print(f"note: {key} search found no subreddits", file=sys.stderr)
        add([SubredditCandidate(name=n, subscribers=0, description="",
                                over_18=False, source=key)
             for n in names])

    popular = list(discovery.POPULAR_SUBREDDITS) if "popular" in sources else []
    return list(merged.values()), popular


def _pick_subreddits(
    candidates: list[SubredditCandidate], *, assume_yes: bool
) -> list[str]:
    """Show the found subreddits and let the user curate the net.

    Edits are '-N' to drop a row and '+name' to add a subreddit; Enter
    accepts. Non-interactive runs (pipes, cron) and --yes take the list
    as-is.
    """
    if assume_yes or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return [c.name for c in candidates]

    print("subreddits found — this is the net:", file=sys.stderr)
    for i, c in enumerate(candidates, 1):
        tag = " [nsfw]" if c.over_18 else ""
        members = f"{c.subscribers:,}" if c.subscribers else "—"
        desc = c.description[:40] + ("…" if len(c.description) > 40 else "")
        print(f"  [{i:2d}] r/{c.name:<24} {members:>10} members "
              f"({c.source}){tag}  {desc}", file=sys.stderr)
    print('edit with "-2 -5" (drop) and "+popheads" (add); Enter accepts',
          file=sys.stderr)

    keep = dict(enumerate((c.name for c in candidates), 1))
    extras: list[str] = []
    while True:
        print("> ", end="", file=sys.stderr, flush=True)
        line = input().strip()
        if not line:
            return [n for _, n in sorted(keep.items())] + extras
        for tok in line.split():
            if tok.startswith("+") and len(tok) > 1:
                extras.append(tok[1:].removeprefix("r/"))
            elif tok.startswith("-") and tok[1:].isdigit() and int(tok[1:]) in keep:
                del keep[int(tok[1:])]
        current = [n for _, n in sorted(keep.items())] + extras
        print(f"net: {', '.join('r/' + n for n in current) or '(empty)'}",
              file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="redlens")
    p.add_argument("--version", action="version", version=f"redlens {__version__}")
    p.add_argument("--db", default=None, help="SQLite file (default: REDLENS_DB, "
                   "config.toml, or the per-user data dir)")
    sub = p.add_subparsers(dest="verb", required=True)
    sub.add_parser("init")
    sy = sub.add_parser("sync", help="archive a user's history (incremental by default)")
    sy.add_argument("username")
    sy.add_argument("--full", action="store_true",
                    help="ignore saved cursors and re-pull the entire history")
    a = sub.add_parser("analytics")
    a.add_argument("username")
    a.add_argument("--json", action="store_true")
    sm = sub.add_parser(
        "summarize", help="AI profile summary from the archived data (BYO LLM key)")
    sm.add_argument("username")
    sm.add_argument("--json", action="store_true")
    sm.add_argument("--depth", choices=tuple(SUMMARY_DEPTHS),
                    help="how much of the archive to sample (top-voted + recent): "
                    f"{', '.join(SUMMARY_DEPTHS)} (default: {SUMMARY_DEFAULT_DEPTH})")
    e = sub.add_parser("explore")
    e.add_argument("--host", default="127.0.0.1")
    e.add_argument("--port", type=int, default=8000)
    e.add_argument("--no-browser", action="store_true")
    t = sub.add_parser(
        "track", help="follow a topic across public discussion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Build a subreddit *net* and archive every post matching the\n"
            "topic's keywords across it (arctic has no global text search).\n"
            "On a topic's first track you choose discovery sources and curate\n"
            "the found list; the net is remembered, and re-tracking is\n"
            "incremental.\n\n"
            "Discovery sources (--sources name,global,web,popular,llm; the\n"
            "interactive picker offers them when --sources is omitted):\n"
            "  name    subreddits whose NAME matches the topic (keyless)\n"
            "  global  subreddits whose POSTS match, via PullPush (keyless)\n"
            "  web     subreddits from a DuckDuckGo search (keyless, flaky)\n"
            "  popular cast over the ~100 largest subreddits\n"
            "  llm     one cheap LLM-suggested list (needs an LLM key)\n"
            "--discover adds a round that follows authors of matching posts\n"
            "to other subreddits where they discuss the topic."
        ),
    )
    t.add_argument("topic")
    t.add_argument("--query", help="full-text query; comma-separated terms "
                   "are OR'd, e.g. 'ubi, universal basic income' "
                   "(default: the topic name)")
    t.add_argument("--days", type=int, help="trailing window (default: 180)")
    t.add_argument("--subreddits", help="comma-separated subreddits to add to the net")
    t.add_argument("--exclude", help="comma-separated terms; posts containing "
                   "any are dropped, e.g. 'ubisoft, rainbow six' for topic ubi")
    t.add_argument("--discover", action="store_true",
                   help="widen the net one round via authors of matching posts")
    t.add_argument("--comments", action="store_true",
                   help="also pull comment threads under matched posts")
    t.add_argument("--reset", action="store_true",
                   help="clear this topic's matches and re-pull from scratch "
                   "(use when narrowing keywords, not broadening)")
    t.add_argument("-y", "--yes", action="store_true",
                   help="accept the found subreddit list without the picker")
    t.add_argument("--sources", help="comma-separated discovery sources "
                   "(name, global, web, popular, llm) — lets you request "
                   "web/global non-interactively; default is the picker")
    doc = sub.add_parser(
        "doctor", help="diagnose the environment (DB, config, arctic-shift, LLM key)")
    doc.add_argument("--json", action="store_true")
    g = sub.add_parser("page", help="render a tracked topic as a standalone HTML page")
    g.add_argument("topic")
    g.add_argument("-o", "--out", help="output path (default: ./<topic>.html)")
    if onboarding.ENABLED:
        sub.add_parser("setup")
    args = p.parse_args(argv)

    try:
        if args.verb == "setup":
            return onboarding.run_wizard()
        if args.verb == "doctor":
            return run_doctor(args.db, as_json=args.json)
        onboarding.offer_setup_on_first_run()
        db = resolve_db(args.db)
        if args.verb == "explore":
            return explore.serve(db, host=args.host, port=args.port,
                                 open_browser=not args.no_browser)
        engine = connect(db)
        init_schema(engine)
        if args.verb == "init":
            print(f"schema applied to {db}")
        elif args.verb == "sync":
            r = sync_user(args.username, engine, full=args.full)
            print(f"u/{r.user.username}: "
                  f"{r.posts_written:,} posts, {r.comments_written:,} comments")
        elif args.verb == "track":
            subs = ([s.strip() for s in args.subreddits.split(",") if s.strip()]
                    if args.subreddits else None)
            # First track of a topic: pick discovery sources, gather, and
            # let the user curate the resulting net. Re-tracks reuse the
            # stored net without asking again.
            with session(engine) as s:
                existing = get_topic(s, args.topic)
            if not (existing and existing.subreddit_list):
                sources = _resolve_sources(args.sources, assume_yes=args.yes)
                terms = query_terms(args.query) if args.query else [args.topic]
                found, popular = _gather_candidates(terms, sources)
                if found:
                    subs = (subs or []) + _pick_subreddits(
                        found, assume_yes=args.yes)
                if popular:
                    print(f"+ casting over {len(popular)} popular subreddits",
                          file=sys.stderr)
                    subs = (subs or []) + popular
            res = track_topic(
                engine, args.topic,
                query=args.query, subreddits=subs,
                days=args.days, exclude=args.exclude, discover=args.discover,
                reset=args.reset,
                on_progress=lambda sub, n: print(
                    f"  r/{sub}: {n} new", file=sys.stderr),
            )
            if res.discovered:
                print(f"discovered: {', '.join('r/' + s for s in res.discovered)}",
                      file=sys.stderr)
            for failed_sub, err in res.failed.items():
                print(f"warning: r/{failed_sub} skipped: {err}", file=sys.stderr)
            print(f"{res.topic.name!r}: {res.posts_new:,} new posts across "
                  f"{res.subreddits_searched} subreddits "
                  f"(keywords {', '.join(res.topic.keyword_list)!r}, "
                  f"last {res.topic.days} days)")
            if args.comments:
                print("pulling comment threads under matched posts…",
                      file=sys.stderr)
                n = pull_topic_comments(
                    engine, args.topic,
                    on_progress=lambda i, total: print(
                        f"  comments: {i}/{total} posts", file=sys.stderr),
                )
                print(f"{res.topic.name!r}: {n:,} comments stored")
            print(f"next: redlens page {res.topic.name!r}")
        elif args.verb == "page":
            html_doc = render_topic_page(engine, args.topic)
            out = Path(args.out or f"{_slug(args.topic)}.html")
            out.write_text(html_doc, encoding="utf-8")
            print(f"wrote {out} ({len(html_doc):,} bytes)")
        elif args.verb == "summarize":
            with session(engine) as s:
                summ = summarize_user(s, args.username, depth=args.depth)
            if args.json:
                print(summ.model_dump_json(indent=2))
            else:
                print(_format_profile(summ))
        else:
            with session(engine) as s:
                an = compute_user_analytics(s, args.username)
            if args.json:
                print(an.model_dump_json(indent=2))
            else:
                print(f"u/{an.username}: {an.total_posts:,} posts, "
                      f"{an.total_comments:,} comments, "
                      f"karma {an.total_karma:+,} "
                      f"(posts {an.post_karma:+,}, comments {an.comment_karma:+,})")
                print(f"  active {an.active_days:,} days · "
                      f"{an.distinct_subreddits:,} subs · "
                      f"top r/{an.top_subreddit} "
                      f"({an.top_subreddit_event_count:,} events)")
                print(f"  first {_ts(an.first_event_at)} · last {_ts(an.last_event_at)}")
        return 0
    except NotFound as e:
        print(f"not found: {e}", file=sys.stderr)
        return 2
    except MissingKey as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except RedlensError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
