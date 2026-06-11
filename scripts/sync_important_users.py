"""Sync 100 hand-curated "important" Reddit users into a shared DB.

Caps per-user event count so the whole batch finishes in minutes, not hours.
Runs N workers in parallel against arctic-shift.
"""

from __future__ import annotations

import concurrent.futures as cf
import sys
import time

from redditpages import arctic
from redditpages.db import connect, data_db, init_schema
from redditpages.errors import NotFound, RedditPagesError
from redditpages.ingest import sync_user

# Cap each kind (posts, comments) at this many items. Most users below the
# leaderboard tier complete fully under 10K; only true power users
# (GallowBoob, kn0thing, spez) hit the ceiling.
arctic.MAX_ITEMS_PER_STREAM = 50_000

# Usernames we never sync, even if they appear in USERS. Bots dominate
# every analytic — AutoModerator alone has activity in tens of thousands
# of subs — and they don't say anything interesting about people.
DISALLOW: frozenset[str] = frozenset({
    "AutoModerator",
    "RemindMeBot",
    "WikiTextBot",
    "TotesMessenger",
    "GoodBot_BadBot",
    "TheNitromeFan",
    "VredditDownloader",
    "RepostSleuthBot",
    "transcribersofreddit",
    "converter-bot",
    "Sub_Corrector_Bot",
    "haikubot-1911",
    "YoMommaJokeBot",
    "LimbRetrieval-Bot",
    "StreetlightManager",
})

USERS: list[tuple[str, str, str]] = [
    # ── Reddit founders & admins (12) ────────────────────────────────────
    ("spez", "admin", "co-founder, CEO"),
    ("kn0thing", "admin", "co-founder Alexis Ohanian"),
    ("yishan", "admin", "ex-CEO Yishan Wong"),
    ("KeyserSosa", "admin", "CTO Chris Slowe"),
    ("Deimorz", "admin", "built AutoModerator"),
    ("raldi", "admin", "early admin"),
    ("jedberg", "admin", "first paid employee"),
    ("hueypriest", "admin", "former GM"),
    ("alienth", "admin", "former sysadmin"),
    ("redtaboo", "admin", "long-tenured admin"),
    ("powerlanguage", "admin", "admin"),
    ("ekjp", "admin", "Ellen Pao, former interim CEO"),

    # ── Famous AMAs / public figures (25) ────────────────────────────────
    ("thisisbillgates", "celeb", "Bill Gates, recurring AMAs"),
    ("PresidentObama", "celeb", "Barack Obama AMA"),
    ("ChrisHadfield", "celeb", "astronaut, viral AMA"),
    ("NeildeGrasseTyson", "celeb", "astrophysicist, regular AMA"),
    ("BillNyeOfficial", "celeb", "Bill Nye"),
    ("realwilliamshatner", "celeb", "William Shatner"),
    ("Schwarzenegger", "celeb", "Arnold"),
    ("ThisIsKevinSmith", "celeb", "Kevin Smith"),
    ("AndySamberg", "celeb", "Andy Samberg"),
    ("realhughlaurie", "celeb", "Hugh Laurie"),
    ("Snoop_Dogg", "celeb", "Snoop"),
    ("Mike_Tyson", "celeb", "Mike Tyson"),
    ("madonna", "celeb", "Madonna"),
    ("EzraKlein", "celeb", "Ezra Klein, journalist"),
    ("RealAndrewYang", "celeb", "Andrew Yang"),
    ("SenSanders", "celeb", "Bernie Sanders"),
    ("SenSchumer", "celeb", "Chuck Schumer"),
    ("SenWarren", "celeb", "Elizabeth Warren"),
    ("MarkRuffalo", "celeb", "Mark Ruffalo"),
    ("eddie_huang", "celeb", "Eddie Huang"),
    ("AntoineDodson_", "celeb", "Antoine Dodson"),
    ("RealJamieFoxx", "celeb", "Jamie Foxx"),
    ("RealGeneSimmons", "celeb", "Gene Simmons of KISS"),
    ("realdanecook", "celeb", "Dane Cook"),
    ("RealAndrewWK", "celeb", "Andrew W.K."),

    # ── Infrastructure bots (15) ─────────────────────────────────────────
    ("AutoModerator", "bot", "ubiquitous moderation bot"),
    ("RemindMeBot", "bot", "reminders"),
    ("WikiTextBot", "bot", "Wikipedia snippets"),
    ("TotesMessenger", "bot", "cross-sub mentions"),
    ("GoodBot_BadBot", "bot", "bot rating bot"),
    ("TheNitromeFan", "bot", "numbers/counting bot"),
    ("VredditDownloader", "bot", "v.redd.it downloader"),
    ("RepostSleuthBot", "bot", "repost detection"),
    ("transcribersofreddit", "bot", "transcription queue"),
    ("converter-bot", "bot", "unit conversion"),
    ("Sub_Corrector_Bot", "bot", "subreddit typo fixer"),
    ("haikubot-1911", "bot", "haiku detector"),
    ("YoMommaJokeBot", "bot", "joke responder"),
    ("LimbRetrieval-Bot", "bot", "/me arm bot"),
    ("StreetlightManager", "bot", "streetlight tracking"),

    # ── Top moderators of major subs (13 — already verified in arctic) ───
    ("funny_mod", "mod", "r/funny mod"),
    ("DuckDragon", "mod", "r/funny mod"),
    ("Kylde", "mod", "r/funny mod, 'Janitor'"),
    ("BestRbx", "mod", "r/funny mod, AutoMod Jr"),
    ("llehsadam", "mod", "r/funny mod"),
    ("RamsesThePigeon", "mod", "r/funny mod"),
    ("verdatum", "mod", "r/funny mod"),
    ("MrAwkwardCrotch", "mod", "r/funny mod"),
    ("Umdlye", "mod", "r/funny mod, 'Steph'"),
    ("N8theGr8", "mod", "r/politics mod"),
    ("PoppinKREAM", "mod", "noted journalism"),
    ("Cribsby_critter", "mod", "moderates several large subs"),
    ("BurnedOutTriangle", "mod", "active default-sub mod"),

    # ── High-karma power users (15) ──────────────────────────────────────
    ("GallowBoob", "power", "long-time repost king, top karma"),
    ("Maxwellhill", "power", "top-ranked karma for years, dormant since 2021"),
    ("Apostolate", "power", "high comment karma"),
    ("LearnedHand_", "power", "askhistorians prolific"),
    ("Dragonsandman", "power", "high karma comments"),
    ("Erin960", "power", "high karma news"),
    ("9c6", "power", "consistent top-karma"),
    ("Slick_Wylde", "power", "high karma"),
    ("PoeticGopher", "power", "high-karma writer"),
    ("FloofusMaximus", "power", "high karma"),
    ("Ralph_Marbler", "power", "high karma"),
    ("YoungInChicago", "power", "high-karma poster"),
    ("Brrrrrrrro", "power", "high karma"),
    ("davidreiss666", "power", "long-tenured controversial mod/poster"),
    ("kn0thingsKarma", "power", "alt account for kn0thing"),

    # ── Notable / infamous (10) ──────────────────────────────────────────
    ("unidan", "infamous", "biologist banned for vote manipulation, 2014"),
    ("shittymorph", "infamous", "Hell-in-a-Cell pasta bait copypasta master"),
    ("violentacrez", "infamous", "doxxed in 2012 Gawker article"),
    ("Apostolate", "infamous", "(also in power) AskScience figure"),
    ("Warlizard", "infamous", "the 'Warlizard gaming forum' meme"),
    ("Andrew_Jackson_Jihad", "infamous", "famous spam/joke account"),
    ("rampaige", "infamous", "Paige (LinusTechTips) — known YouTuber"),
    ("Quouar", "infamous", "AskHistorians prolific historian"),
    ("nascent_aviator", "infamous", "Notable longform commenter"),
    ("HotDog_lover", "infamous", "joke handle, high engagement"),

    # ── Tech & culture figures (10) ──────────────────────────────────────
    ("JeffArnold", "tech", "EBT pioneer (claim, verify)"),
    ("notch", "tech", "Markus Persson, Minecraft creator"),
    ("paulg", "tech", "Paul Graham, YC"),
    ("officialgaben", "tech", "Gabe Newell, Valve"),
    ("UncannyMagicalNoise", "tech", "Veritasium adjacent"),
    ("WilliamShatnerBleep", "celeb", "alt for Shatner"),
    ("RIP_Aaron_Swartz", "tech", "memorial account"),
    ("ChrisGuillebeau", "tech", "writer/entrepreneur"),
    ("kn0thingofficial", "tech", "alt for Ohanian"),
    ("LeoLaporte", "tech", "Leo Laporte, TWIT"),
]


def _sync_one(user_tuple: tuple[str, str, str], db_path: str) -> dict:
    username, category, reason = user_tuple
    engine = connect(db_path)
    t0 = time.monotonic()
    try:
        r = sync_user(username, engine)
        return {
            "username": username, "category": category, "reason": reason,
            "status": "ok",
            "posts": r.posts_written, "comments": r.comments_written,
            "elapsed_s": round(time.monotonic() - t0, 1),
        }
    except NotFound:
        return {"username": username, "category": category, "reason": reason,
                "status": "not_in_arctic",
                "posts": 0, "comments": 0, "elapsed_s": round(time.monotonic() - t0, 1)}
    except RedditPagesError as e:
        return {"username": username, "category": category, "reason": reason,
                "status": f"error: {e}",
                "posts": 0, "comments": 0, "elapsed_s": round(time.monotonic() - t0, 1)}


def main() -> int:
    db_path = data_db("redditpages.db")
    engine = connect(db_path)
    init_schema(engine)

    # Filter out users on the disallow list (mainly bots).
    todo = [u for u in USERS if u[0] not in DISALLOW]
    disallowed = len(USERS) - len(todo)

    # Skip users we already have data for (incremental re-runs).
    from sqlmodel import Session, select
    from redditpages.models import User
    with Session(engine) as s:
        already = {u.username.lower() for u in s.exec(select(User))}
    todo = [u for u in todo if u[0].lower() not in already]
    skipped = (len(USERS) - disallowed) - len(todo)

    print(f"syncing into {db_path}  "
          f"(cap {arctic.MAX_ITEMS_PER_STREAM}/stream, sequential)")
    print(f"{disallowed} on disallow list; {skipped} already in DB; "
          f"{len(todo)} to sync")
    print()

    start = time.monotonic()
    results: list[dict] = []
    for i, u in enumerate(todo, 1):
        r = _sync_one(u, db_path)
        results.append(r)
        tag = (
            f"+{r['posts']:>4}p +{r['comments']:>4}c"
            if r["status"] == "ok"
            else r["status"]
        )
        print(f"[{i:3d}/{len(todo)}] {r['username']:30s} {r['category']:8s} "
              f"{tag:>20s}  {r['elapsed_s']:>5.1f}s",
              flush=True)

    elapsed = time.monotonic() - start
    ok = [r for r in results if r["status"] == "ok"]
    not_found = [r for r in results if r["status"] == "not_in_arctic"]
    errors = [r for r in results if r["status"].startswith("error")]
    print()
    print(f"done in {elapsed/60:.1f} min — "
          f"{len(ok)} synced, {len(not_found)} not in arctic, {len(errors)} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
