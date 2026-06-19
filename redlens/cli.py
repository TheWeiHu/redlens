from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from redlens import __version__, completions, discovery, export, onboarding
from redlens.analytics import (
    compute_topic_analytics,
    compute_user_analytics,
    list_users,
)
from redlens.config import default_report_dir, llm_api_key, resolve_db
from redlens.constants import SUMMARY_DEFAULT_DEPTH, SUMMARY_DEPTHS
from redlens.db import connect, init_schema, session
from redlens.doctor import run_doctor
from redlens.errors import MissingKey, NotFound, RedlensError
from redlens.ingest import sync_user
from redlens.models import Profile, TopicAnalytics, TopicSummary
from redlens.reporting import explore
from redlens.reporting.page import render_all, render_topic_page, slug
from redlens.summarize import summarize_topic, summarize_user
from redlens.topics import (
    SubredditCandidate,
    get_topic,
    list_topics,
    pull_topic_comments,
    query_terms,
    search_subreddits,
    track_topic,
    untrack_topic,
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


def _emit_json(obj: Any) -> None:
    """Print a pydantic model — or a list of them — as indented JSON to stdout."""
    if isinstance(obj, list):
        print(json.dumps([r.model_dump() for r in obj], indent=2))
    else:
        print(obj.model_dump_json(indent=2))


def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    """Ask a destructive y/N question. --yes skips it; a non-interactive run
    without --yes declines (so a pipe/cron never deletes by surprise)."""
    if assume_yes:
        return True
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    print(f"{prompt} [y/N] ", end="", file=sys.stderr, flush=True)
    return input().strip().lower() in ("y", "yes")


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


def _format_topic_summary(s: TopicSummary) -> str:
    """Render a structured TopicSummary as readable terminal text (the same
    fields are in ``--json``)."""
    lines = [f"{s.topic!r} (via {s.model}, {s.depth} depth):", ""]
    if s.overview:
        lines += [s.overview]
    if s.themes:
        lines += ["", "Themes:"]
        lines += [f"  • {t.title}: {t.summary}".rstrip(": ") for t in s.themes]
    for heading, body in (("Sentiment", s.sentiment),
                          ("Viewpoints", s.viewpoints)):
        if body:
            lines += ["", f"{heading}: {body}"]
    return "\n".join(lines)


def _print_topic_analytics(ta: TopicAnalytics) -> None:
    """Render a topic roll-up as readable terminal text (full ranked lists are
    in ``--json``; here we show the headline numbers and the leaders)."""
    print(f"{ta.name!r}: {ta.matched_posts:,} matched posts across "
          f"{ta.distinct_subreddits:,}/{ta.net_size:,} subreddits · "
          f"{ta.total_score:+,} score")
    print(f"  keywords {', '.join(ta.keywords)!r} · "
          f"dates {_ts(ta.first_post_at)} – {_ts(ta.last_post_at)} · "
          f"tracked {_ts(ta.last_tracked_at)}")
    if ta.top_subreddits:
        print("  top subs: " + ", ".join(
            f"r/{s.name} ({s.count:,})" for s in ta.top_subreddits[:5]))
    if ta.top_authors:
        print("  top authors: " + ", ".join(
            f"u/{a.name} ({a.count:,})" for a in ta.top_authors[:5]))


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="redlens")
    p.add_argument("--version", action="version", version=f"redlens {__version__}")
    p.add_argument("--db", default=None, help="SQLite file (default: REDLENS_DB, "
                   "config.toml, or the per-user data dir)")
    # metavar keeps the hidden verbs (the `analytics` deprecation alias and the
    # `__complete` helper, both registered without help=) out of the usage
    # line's {choices} brace; they're already absent from the command list.
    sub = p.add_subparsers(dest="verb", required=True, metavar="<command>")
    sub.add_parser("init")
    sy = sub.add_parser("sync", help="archive a user's history (incremental by default)")
    sy.add_argument("username")
    sy.add_argument("--full", action="store_true",
                    help="ignore saved cursors and re-pull the entire history")
    sh = sub.add_parser("show", help="print a user's (or --topic's) roll-up stats")
    sh.add_argument("username", nargs="?", help="user to roll up (omit when using --topic)")
    sh.add_argument("--topic", help="roll up a tracked topic instead of a user")
    sh.add_argument("--json", action="store_true")
    # `analytics` is the old name for `show`; kept as a hidden alias for one
    # release (no help= so it stays out of `--help`).
    al = sub.add_parser("analytics")
    al.add_argument("username")
    al.add_argument("--json", action="store_true")
    ls = sub.add_parser("list", help="list every user in the DB")
    ls.add_argument("--json", action="store_true")
    tp = sub.add_parser("topics", help="list every tracked topic")
    tp.add_argument("--json", action="store_true")
    ex = sub.add_parser(
        "export", help="dump a user's — or a tracked topic's — posts and comments")
    ex.add_argument("username", nargs="?",
                    help="the user to export (omit when using --topic)")
    ex.add_argument("--topic",
                    help="export this tracked topic's matched posts/comments "
                    "instead of a user")
    ex.add_argument("--format", choices=export.FORMATS, default="json",
                    help=f"output format: {', '.join(export.FORMATS)} (default: json)")
    ex.add_argument("-o", "--out", help="write to PATH (default: stdout)")
    sm = sub.add_parser(
        "summarize",
        help="AI summary from the archived data: a user's profile, or "
             "--topic's discussion (BYO LLM key)")
    sm.add_argument("username", nargs="?",
                    help="user to profile (omit when using --topic)")
    sm.add_argument("--topic", help="summarize a tracked topic's discussion "
                    "instead of a user")
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
    doc.add_argument("--no-network", action="store_true",
                     help="skip the arctic-shift reachability probe (offline "
                     "diagnosis); reports it as skipped, not failed")
    g = sub.add_parser("page", help="render a tracked topic as a standalone HTML page")
    g.add_argument("topic", nargs="?", help="topic to render (omit with --all)")
    g.add_argument("--all", action="store_true", dest="all_topics",
                   help="render every tracked topic plus an index.html into -o "
                   "(a directory)")
    g.add_argument("-o", "--out", help="single topic: output file "
                   "(default: ./<topic>.html); --all: output directory "
                   "(default: the per-user reports dir)")
    g.add_argument("--open", action="store_true",
                   help="open the rendered page (or the index, with --all) in a "
                   "browser after writing it")
    g.add_argument("--no-browser", action="store_true",
                   help="never open a browser, even with --open (for scripts/CI)")
    ut = sub.add_parser(
        "untrack", help="stop tracking a topic and drop its orphaned matches")
    ut.add_argument("topic")
    ut.add_argument("-y", "--yes", action="store_true",
                    help="delete without the confirmation prompt")
    c = sub.add_parser(
        "completions", help="print a shell completion script (eval or save it)")
    c.add_argument("shell", choices=completions.SHELLS)
    # Hidden helper the generated completion scripts shell out to for DB-backed
    # value completion (usernames / topic names). No help= and a `__` prefix so
    # it stays out of `--help` and out of the completion scripts themselves.
    cmp = sub.add_parser(completions.HELPER_VERB)
    cmp.add_argument("kind", choices=("users", "topics"))
    if onboarding.ENABLED:
        sub.add_parser("setup")
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)

    try:
        if args.verb == "completions":
            print(completions.generate(args.shell, p), end="")
            return 0
        if args.verb == completions.HELPER_VERB:
            # Read-only value completion for the generated scripts; resolve the
            # DB path but never create it (completion has no side effects).
            for value in completions.complete(args.kind, resolve_db(args.db)):
                print(value)
            return 0
        if args.verb == "setup":
            return onboarding.run_wizard()
        if args.verb == "doctor":
            return run_doctor(args.db, as_json=args.json,
                              no_network=args.no_network)
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
            if args.all_topics:
                out_dir = Path(args.out) if args.out else default_report_dir()
                results = render_all(engine, out_dir)
                written = [pg for pg in results if pg.written]
                skipped = [pg for pg in results if not pg.written]
                for pg in written:
                    print(f"wrote {out_dir / (pg.slug + '.html')}")
                if skipped:
                    print(f"skipped {len(skipped)} topic(s) with no matched "
                          f"posts: {', '.join(pg.name for pg in skipped)}",
                          file=sys.stderr)
                index = out_dir / "index.html"
                print(f"index: {index} "
                      f"({len(written)} topic{'' if len(written) == 1 else 's'})")
                if args.open and not args.no_browser:
                    webbrowser.open(index.resolve().as_uri())
            elif args.topic:
                html_doc = render_topic_page(engine, args.topic)
                out = Path(args.out or f"{slug(args.topic)}.html")
                out.write_text(html_doc, encoding="utf-8")
                print(f"wrote {out} ({len(html_doc):,} bytes)")
                if args.open and not args.no_browser:
                    webbrowser.open(out.resolve().as_uri())
            else:
                raise RedlensError("page: give a topic or pass --all")
        elif args.verb == "untrack":
            with session(engine) as s:
                if get_topic(s, args.topic) is None:
                    raise NotFound(f"topic {args.topic!r} is not tracked")
            if not _confirm(
                f"delete topic {args.topic!r} and its orphaned posts/comments?",
                assume_yes=args.yes,
            ):
                print("untrack: aborted (pass -y to confirm non-interactively)",
                      file=sys.stderr)
                return 1
            ur = untrack_topic(engine, args.topic)
            print(f"untracked {ur.name!r}: removed {ur.links_removed:,} topic "
                  f"links, {ur.posts_deleted:,} orphaned posts, "
                  f"{ur.comments_deleted:,} orphaned comments")
        elif args.verb == "summarize":
            if args.topic:
                with session(engine) as s:
                    tsumm = summarize_topic(s, args.topic, depth=args.depth)
                if args.json:
                    _emit_json(tsumm)
                else:
                    print(_format_topic_summary(tsumm))
            else:
                if not args.username:
                    raise RedlensError(
                        "summarize: give a username or --topic <topic>")
                with session(engine) as s:
                    summ = summarize_user(s, args.username, depth=args.depth)
                if args.json:
                    _emit_json(summ)
                else:
                    print(_format_profile(summ))
        elif args.verb == "list":
            with session(engine) as s:
                rows = list_users(s)
            if args.json:
                _emit_json(rows)
            elif not rows:
                print("no users in DB — sync one with: redlens sync <user>",
                      file=sys.stderr)
            else:
                for row in rows:
                    print(f"u/{row.username}: {row.total_posts:,} posts, "
                          f"{row.total_comments:,} comments · "
                          f"last event {_ts(row.last_event_at)} · "
                          f"synced {_ts(row.last_synced_at)}")
        elif args.verb == "topics":
            with session(engine) as s:
                topic_rows = list_topics(s)
            if args.json:
                _emit_json(topic_rows)
            elif not topic_rows:
                print("no topics tracked — start one with: redlens track <topic>",
                      file=sys.stderr)
            else:
                for trow in topic_rows:
                    print(f"{trow.name}: {trow.matched_posts:,} posts across "
                          f"{trow.subreddit_count:,} subreddits · "
                          f"keywords {', '.join(trow.keywords)!r} · "
                          f"tracked {_ts(trow.last_tracked_at)}")
        elif args.verb == "export":
            if (args.username is None) == (args.topic is None):
                raise RedlensError(
                    "export needs exactly one of <username> or --topic")

            def _do_export(out: TextIO) -> tuple[int, int]:
                if args.topic:
                    return export.export_topic(s, args.topic, args.format, out)
                return export.export_user(s, args.username, args.format, out)

            scope = f"topic {args.topic!r}" if args.topic else f"u/{args.username}"
            with session(engine) as s:
                if args.out:
                    with open(args.out, "w", encoding="utf-8", newline="") as fh:
                        n_posts, n_comments = _do_export(fh)
                    print(f"wrote {n_posts:,} posts + {n_comments:,} comments "
                          f"for {scope} to {args.out}", file=sys.stderr)
                else:
                    if args.format == "csv":
                        # csv writers emit their own \r\n terminators; a text
                        # stream that also translates \n would double them into
                        # \r\r\n (blank rows) on Windows. Disable translation,
                        # matching the newline="" used for the file path. Guard
                        # with getattr so a wrapped stream (e.g. test capture)
                        # without reconfigure degrades instead of crashing.
                        reconfigure = getattr(sys.stdout, "reconfigure", None)
                        if reconfigure is not None:
                            reconfigure(newline="")
                    _do_export(sys.stdout)
        elif getattr(args, "topic", None):  # show --topic <topic>
            with session(engine) as s:
                ta = compute_topic_analytics(s, args.topic)
            if args.json:
                _emit_json(ta)
            else:
                _print_topic_analytics(ta)
        else:  # "show <user>" or its hidden alias "analytics"
            if args.verb == "analytics":
                print("note: 'analytics' is deprecated; use 'show' "
                      "(this alias is kept for one release)", file=sys.stderr)
            if not args.username:
                raise RedlensError("show: give a username or --topic <topic>")
            with session(engine) as s:
                an = compute_user_analytics(s, args.username)
            if args.json:
                _emit_json(an)
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
