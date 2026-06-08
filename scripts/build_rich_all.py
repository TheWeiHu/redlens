"""For every user in the DB, build the rich render.py HTML page.

Writes to ``<out-dir>/u/{username}.html``. The companion ``build_index.py``
regenerates ``<out-dir>/index.html`` linking to these.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sqlmodel import Session, select

from redditpages.db import DATA_DIR, connect, data_db
from redditpages.models import User
from scripts.build_payload import build_payload

# render.py lives at the repo root (one level up from scripts/).
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
from render import render  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=data_db("redditpages.db"))
    p.add_argument("--out-dir", default=str(DATA_DIR / "important"))
    args = p.parse_args()

    out = Path(args.out_dir).resolve() / "u"
    out.mkdir(parents=True, exist_ok=True)

    engine = connect(args.db)
    with Session(engine) as s:
        users = sorted(u.username for u in s.exec(select(User)))

    start = time.monotonic()
    total_bytes = 0
    for i, username in enumerate(users, 1):
        t0 = time.monotonic()
        try:
            payload = build_payload(args.db, username)
            html = render(payload)
        except Exception as exc:
            print(f"[{i:2d}/{len(users)}] {username:30s} ERROR: {exc}",
                  flush=True)
            continue
        (out / f"{username}.html").write_text(html)
        total_bytes += len(html)
        print(f"[{i:2d}/{len(users)}] {username:30s} "
              f"{len(html):>10,} bytes  ({time.monotonic() - t0:.1f}s)",
              flush=True)

    print(f"\ndone in {time.monotonic() - start:.1f}s. "
          f"wrote {len(users)} pages, {total_bytes / 1_048_576:.1f} MB total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
