"""Local listening-report server — the coordinated-network view.

The first slice of the paid listening report (see ``DESIGN.md``). It serves a
localhost dashboard over an existing redlens SQLite file, framed as a
*coordinated network*: every account in the DB is treated as one cohort and the
report surfaces the deterministic, keyless coordination signals between them —

- who the accounts are and how much each posts/comments,
- the **subreddit footprint** they share (subs ≥2 accounts are active in),
- the **threads they co-occur in** (``link_id`` touched by ≥2 accounts) — the
  strongest cheap co-activity signal,

and lets you drill from any account into its raw posts and comments.

    redlens serve                          # over the default DB
    redlens --db redrover.db serve         # dogfood on the redrover network
    redlens serve --port 9000 --no-browser

The database is opened **read-only**; nothing here can mutate data and no LLM
key is required. Per-account ``gpt-4o-mini`` profiles with a ``coordinated?``
flag, brand share-of-voice, and view-time NL-plots are later slices.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

MAX_ROWS = 60        # shared-subreddit / co-commented-thread rows shown
MAX_CONTENT = 100    # account drill-down page cap


# --------------------------------------------------------------------------- #
# Data access (every request gets its own read-only connection)               #
# --------------------------------------------------------------------------- #

class Network:
    """Read-only queries that describe the account network in one DB."""

    def __init__(self, path: str) -> None:
        self.path = str(Path(path).resolve())

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def overview(self) -> dict[str, Any]:
        with closing(self._conn()) as con:
            row = con.execute(
                """
                SELECT
                  (SELECT count(*) FROM post)                        AS posts,
                  (SELECT count(*) FROM comment)                     AS comments,
                  (SELECT count(DISTINCT subreddit_name) FROM (
                     SELECT subreddit_name FROM post
                     UNION SELECT subreddit_name FROM comment))      AS subreddits,
                  (SELECT min(t) FROM (
                     SELECT min(created_utc) t FROM post
                     UNION SELECT min(created_utc) FROM comment))    AS first_utc,
                  (SELECT max(t) FROM (
                     SELECT max(created_utc) t FROM post
                     UNION SELECT max(created_utc) FROM comment))    AS last_utc
                """
            ).fetchone()
            out = dict(row)
            out["accounts"] = len(self._authors(con))
            return out

    def _authors(self, con: sqlite3.Connection) -> list[str]:
        return [
            r[0] for r in con.execute(
                "SELECT author_username FROM post "
                "UNION SELECT author_username FROM comment ORDER BY 1"
            )
        ]

    def accounts(self) -> list[dict[str, Any]]:
        """Per-account volume, karma, active window, and busiest subreddit."""
        with closing(self._conn()) as con:
            rows = con.execute(
                """
                WITH activity AS (
                  SELECT author_username AS u, subreddit_name AS sub,
                         created_utc AS t, 'post' AS kind FROM post
                  UNION ALL
                  SELECT author_username, subreddit_name, created_utc, 'comment'
                  FROM comment
                )
                SELECT
                  a.u                                              AS username,
                  sum(a.kind = 'post')                             AS posts,
                  sum(a.kind = 'comment')                          AS comments,
                  min(a.t)                                         AS first_utc,
                  max(a.t)                                         AS last_utc,
                  count(DISTINCT a.sub)                            AS subreddits,
                  u.post_karma                                     AS post_karma,
                  u.comment_karma                                  AS comment_karma
                FROM activity a
                LEFT JOIN user u ON u.username = a.u
                GROUP BY a.u
                """
            ).fetchall()
            top = self._top_subreddit(con)
            out = []
            for r in rows:
                d = dict(r)
                d["total"] = d["posts"] + d["comments"]
                d["top_subreddit"] = top.get(d["username"], "")
                out.append(d)
            out.sort(key=lambda d: d["total"], reverse=True)
            return out

    def _top_subreddit(self, con: sqlite3.Connection) -> dict[str, str]:
        """Busiest subreddit per author, across posts and comments."""
        rows = con.execute(
            """
            SELECT u, sub FROM (
              SELECT u, sub, row_number() OVER (
                       PARTITION BY u ORDER BY n DESC, sub) AS rn
              FROM (
                SELECT author_username u, subreddit_name sub, count(*) n
                FROM (SELECT author_username, subreddit_name FROM post
                      UNION ALL
                      SELECT author_username, subreddit_name FROM comment)
                GROUP BY u, sub))
            WHERE rn = 1
            """
        ).fetchall()
        return {r["u"]: r["sub"] for r in rows}

    def subreddits(self) -> dict[str, Any]:
        """Shared-subreddit footprint: subs where ≥2 accounts are active.

        Long tails are common (a real network shares hundreds of subs), so this
        returns the ``MAX_ROWS`` widest-shared plus ``total`` for a "top N of M"
        caption.
        """
        with closing(self._conn()) as con:
            total = con.execute(
                """
                SELECT count(*) FROM (
                  SELECT subreddit_name FROM (
                    SELECT author_username u, subreddit_name FROM post
                    UNION ALL SELECT author_username, subreddit_name FROM comment)
                  GROUP BY subreddit_name
                  HAVING count(DISTINCT u) >= 2)
                """
            ).fetchone()[0]
            rows = con.execute(
                """
                SELECT sub                              AS subreddit,
                       count(DISTINCT u)                AS accounts,
                       sum(kind = 'post')               AS posts,
                       sum(kind = 'comment')            AS comments,
                       group_concat(DISTINCT u)         AS members
                FROM (
                  SELECT author_username u, subreddit_name sub, 'post' kind
                  FROM post
                  UNION ALL
                  SELECT author_username, subreddit_name, 'comment' FROM comment)
                GROUP BY sub
                HAVING accounts >= 2
                ORDER BY accounts DESC, (posts + comments) DESC, subreddit
                LIMIT ?
                """,
                (MAX_ROWS,),
            ).fetchall()
            return {"total": total, "rows": [self._with_members(r) for r in rows]}

    def threads(self) -> dict[str, Any]:
        """Threads (``link_id``) commented in by ≥2 accounts — co-activity."""
        with closing(self._conn()) as con:
            total = con.execute(
                """
                SELECT count(*) FROM (
                  SELECT link_id FROM comment
                  GROUP BY link_id
                  HAVING count(DISTINCT author_username) >= 2)
                """
            ).fetchone()[0]
            rows = con.execute(
                """
                SELECT link_id                          AS link_id,
                       subreddit_name                   AS subreddit,
                       count(DISTINCT author_username)  AS accounts,
                       count(*)                         AS comments,
                       group_concat(DISTINCT author_username) AS members
                FROM comment
                GROUP BY link_id
                HAVING accounts >= 2
                ORDER BY accounts DESC, comments DESC
                LIMIT ?
                """,
                (MAX_ROWS,),
            ).fetchall()
            out = []
            for r in rows:
                d = self._with_members(r)
                title = con.execute(
                    "SELECT title FROM post WHERE post_id = ?", (d["link_id"],)
                ).fetchone()
                d["title"] = title[0] if title and title[0] else ""
                out.append(d)
            return {"total": total, "rows": out}

    def content(self, username: str, kind: str, *, limit: int,
                offset: int) -> dict[str, Any]:
        """One account's raw posts or comments, newest first (drill-down)."""
        limit = max(1, min(limit, MAX_CONTENT))
        offset = max(0, offset)
        with closing(self._conn()) as con:
            if kind == "comments":
                total = con.execute(
                    "SELECT count(*) FROM comment WHERE author_username = ?",
                    (username,),
                ).fetchone()[0]
                rows = con.execute(
                    "SELECT subreddit_name AS subreddit, body, score, "
                    "created_utc AS created_utc, link_id "
                    "FROM comment WHERE author_username = ? "
                    "ORDER BY created_utc DESC LIMIT ? OFFSET ?",
                    (username, limit, offset),
                ).fetchall()
            else:
                total = con.execute(
                    "SELECT count(*) FROM post WHERE author_username = ?",
                    (username,),
                ).fetchone()[0]
                rows = con.execute(
                    "SELECT subreddit_name AS subreddit, title, selftext, url, "
                    "score, num_comments, created_utc AS created_utc, post_id "
                    "FROM post WHERE author_username = ? "
                    "ORDER BY created_utc DESC LIMIT ? OFFSET ?",
                    (username, limit, offset),
                ).fetchall()
            return {"kind": kind, "total": total, "limit": limit,
                    "offset": offset, "items": [dict(r) for r in rows]}

    @staticmethod
    def _with_members(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["members"] = sorted((d.pop("members") or "").split(","))
        return d


# --------------------------------------------------------------------------- #
# HTTP handler                                                                 #
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    net: Network  # injected on the server

    def log_message(self, format: str, *args: Any) -> None:  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)

        def one(k: str, d: str = "") -> str:
            return q.get(k, [d])[0]

        try:
            if u.path == "/":
                self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
            elif u.path == "/api/overview":
                self._json({"db": self.net.path, **self.net.overview()})
            elif u.path == "/api/accounts":
                self._json({"accounts": self.net.accounts()})
            elif u.path == "/api/subreddits":
                self._json(self.net.subreddits())
            elif u.path == "/api/threads":
                self._json(self.net.threads())
            elif u.path == "/api/content":
                self._json(self.net.content(
                    one("u"),
                    one("kind", "posts"),
                    limit=int(one("limit", "50") or 50),
                    offset=int(one("offset", "0") or 0),
                ))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 400)


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

def serve(db: str | Path, *, host: str = "127.0.0.1", port: int = 8000,
          open_browser: bool = True) -> int:
    net = Network(str(db))
    net.overview()  # fail fast if the DB is missing or unreadable

    handler = type("BoundHandler", (Handler,), {"net": net})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"redlens listening report → {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------- #
# Frontend (single self-contained page, no external assets)                   #
# --------------------------------------------------------------------------- #

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>redlens · coordinated network</title>
<style>
  :root { --bg:#0b0e14; --panel:#12161f; --fg:#e6edf3; --mut:#8b96a5;
          --line:#232a35; --hl:#1a1f2b; --accent:#00b4d8; --warn:#f04a6b;
          --alt:#0f131b; }
  * { box-sizing: border-box; }
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin:0;
         color:var(--fg); background:var(--bg); font-size:13px; }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  header { padding:20px 24px; border-bottom:1px solid var(--line); }
  header h1 { font-size:15px; margin:0 0 4px; letter-spacing:.04em; }
  header .db { font-size:11px; color:var(--mut); word-break:break-all; }
  .stats { display:flex; flex-wrap:wrap; gap:24px; margin-top:14px; }
  .stat b { display:block; font-size:20px; color:var(--accent);
            font-variant-numeric:tabular-nums; }
  .stat span { font-size:11px; color:var(--mut); text-transform:uppercase;
               letter-spacing:.06em; }
  main { padding:24px; display:grid; gap:28px; max-width:1200px; }
  section h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em;
               color:var(--mut); margin:0 0 4px; }
  section .sub { font-size:11px; color:var(--mut); margin:0 0 12px; }
  section h2 .count { color:var(--fg); font-weight:400; text-transform:none;
                      letter-spacing:0; }
  table { border-collapse:collapse; width:100%; font-size:12px; }
  th, td { padding:6px 10px; text-align:left; border-bottom:1px solid var(--line);
           vertical-align:top; }
  th { color:var(--mut); font-weight:600; white-space:nowrap; cursor:pointer;
       user-select:none; }
  tbody tr:nth-child(even) { background:var(--alt); }
  tbody tr:hover { background:var(--hl); }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .u { color:var(--accent); cursor:pointer; }
  .members { color:var(--mut); font-size:11px; }
  .bar { height:3px; background:var(--accent); border-radius:2px; margin-top:3px; }
  .pill { display:inline-block; background:var(--panel); border:1px solid var(--line);
          border-radius:10px; padding:1px 8px; margin:0 3px 3px 0; font-size:11px; }
  .drawer { position:fixed; top:0; right:0; width:min(680px,92vw); height:100vh;
            background:var(--panel); border-left:1px solid var(--line);
            transform:translateX(100%); transition:transform .15s ease; overflow-y:auto;
            box-shadow:-12px 0 30px rgba(0,0,0,.4); }
  .drawer.open { transform:translateX(0); }
  .drawer .dh { position:sticky; top:0; background:var(--panel); padding:16px 20px;
                border-bottom:1px solid var(--line); display:flex; align-items:center;
                justify-content:space-between; gap:12px; }
  .drawer .dh h3 { margin:0; font-size:14px; }
  .drawer .tabs { display:flex; gap:4px; }
  .drawer .tab { padding:3px 10px; border:1px solid var(--line); border-radius:4px;
                 cursor:pointer; color:var(--mut); }
  .drawer .tab.active { color:var(--fg); border-color:var(--accent); }
  .drawer .close { cursor:pointer; color:var(--mut); font-size:18px; border:none;
                   background:none; }
  .drawer .body { padding:12px 20px 40px; }
  .item { border-bottom:1px solid var(--line); padding:10px 0; }
  .item .meta { color:var(--mut); font-size:11px; margin-bottom:3px; }
  .item .meta b { color:var(--fg); }
  .item .txt { white-space:pre-wrap; word-break:break-word; color:#c9d4e0; }
  .item .title { color:var(--fg); font-weight:600; }
  .pager { display:flex; gap:10px; align-items:center; margin-top:12px; }
  .pager button { background:var(--panel); color:var(--fg); border:1px solid var(--line);
                  border-radius:4px; padding:4px 12px; cursor:pointer; }
  .pager button:disabled { opacity:.35; cursor:default; }
  .muted { color:var(--mut); }
  .warn { color:var(--warn); }
</style>
</head>
<body>
<header>
  <h1>coordinated network</h1>
  <div class="db" id="db">…</div>
  <div class="stats" id="stats"></div>
</header>

<main>
  <section>
    <h2>Accounts</h2>
    <p class="sub">Every account in this database, treated as one cohort. Click a
      name to drill into its raw posts and comments.</p>
    <div style="overflow:auto"><table id="accounts"></table></div>
  </section>

  <section>
    <h2>Shared subreddit footprint <span class="count" id="sub-count"></span></h2>
    <p class="sub">Subreddits where ≥2 accounts are active — where the network
      overlaps. A wide footprint across the same subs is a coordination signal.</p>
    <div style="overflow:auto"><table id="subreddits"></table></div>
  </section>

  <section>
    <h2>Co-commented threads <span class="count" id="thread-count"></span></h2>
    <p class="sub">Threads touched by ≥2 accounts — the strongest cheap
      co-activity signal (they show up in the same conversations).</p>
    <div style="overflow:auto"><table id="threads"></table></div>
  </section>
</main>

<div class="drawer" id="drawer">
  <div class="dh">
    <h3 id="d-user"></h3>
    <div class="tabs">
      <div class="tab active" data-kind="posts" id="tab-posts">posts</div>
      <div class="tab" data-kind="comments" id="tab-comments">comments</div>
    </div>
    <button class="close" id="d-close">✕</button>
  </div>
  <div class="body" id="d-body"></div>
</div>

<script>
const $ = s => document.querySelector(s);
const fmt = n => (n ?? 0).toLocaleString();
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const day = t => t ? new Date(t*1000).toISOString().slice(0,10) : '—';

async function getJSON(url){ const r = await fetch(url); const j = await r.json();
  if(!r.ok || j.error) throw new Error(j.error || r.statusText); return j; }

// ---- overview ----
async function loadOverview(){
  const o = await getJSON('/api/overview');
  $('#db').textContent = o.db;
  $('#stats').innerHTML = [
    ['accounts', o.accounts], ['posts', o.posts], ['comments', o.comments],
    ['subreddits', o.subreddits],
  ].map(([k,v]) => `<div class="stat"><b>${fmt(v)}</b><span>${k}</span></div>`).join('')
    + `<div class="stat"><b>${day(o.first_utc)}</b><span>first seen</span></div>`
    + `<div class="stat"><b>${day(o.last_utc)}</b><span>last seen</span></div>`;
}

// ---- accounts ----
function sortable(table, rows, cols, render){
  let sort = cols.find(c => c.def) || cols[0], asc = false;
  const draw = () => {
    const data = [...rows].sort((a,b) => {
      const x=a[sort.key], y=b[sort.key];
      const c = (x<y?-1:x>y?1:0); return asc ? c : -c;
    });
    table.innerHTML =
      '<thead><tr>' + cols.map(c =>
        `<th class="${c.num?'num':''}" data-k="${c.key}">${c.label}` +
        (c.key===sort.key ? (asc?' ▲':' ▼') : '') + '</th>').join('') + '</tr></thead>' +
      '<tbody>' + data.map(render).join('') + '</tbody>';
    table.querySelectorAll('th').forEach(th => th.onclick = () => {
      const k = th.dataset.k;
      if(sort.key===k) asc=!asc; else { sort=cols.find(c=>c.key===k); asc=false; }
      draw();
    });
  };
  draw();
}

async function loadAccounts(){
  const { accounts } = await getJSON('/api/accounts');
  const max = Math.max(1, ...accounts.map(a => a.total));
  sortable($('#accounts'), accounts, [
    {key:'username', label:'account'},
    {key:'total', label:'total', num:true, def:true},
    {key:'posts', label:'posts', num:true},
    {key:'comments', label:'comments', num:true},
    {key:'subreddits', label:'subs', num:true},
    {key:'post_karma', label:'post karma', num:true},
    {key:'comment_karma', label:'cmt karma', num:true},
    {key:'first_utc', label:'first'},
    {key:'last_utc', label:'last'},
    {key:'top_subreddit', label:'top sub'},
  ], a => `<tr>
    <td><span class="u" onclick="openUser('${esc(a.username)}')">${esc(a.username)}</span></td>
    <td class="num">${fmt(a.total)}<div class="bar" style="width:${100*a.total/max}%"></div></td>
    <td class="num">${fmt(a.posts)}</td>
    <td class="num">${fmt(a.comments)}</td>
    <td class="num">${fmt(a.subreddits)}</td>
    <td class="num">${a.post_karma==null?'—':fmt(a.post_karma)}</td>
    <td class="num">${a.comment_karma==null?'—':fmt(a.comment_karma)}</td>
    <td class="muted">${day(a.first_utc)}</td>
    <td class="muted">${day(a.last_utc)}</td>
    <td><a href="https://reddit.com/r/${esc(a.top_subreddit)}" target="_blank">${esc(a.top_subreddit)}</a></td>
  </tr>`);
}

function members(list){
  return list.map(u => `<span class="u" onclick="openUser('${esc(u)}')">${esc(u)}</span>`).join(', ');
}

function topOf(total, shown){
  return total > shown ? `top ${fmt(shown)} of ${fmt(total)}` : `${fmt(total)}`;
}

async function loadSubreddits(){
  const { total, rows } = await getJSON('/api/subreddits');
  $('#sub-count').textContent = rows.length ? topOf(total, rows.length) : '';
  sortable($('#subreddits'), rows, [
    {key:'subreddit', label:'subreddit'},
    {key:'accounts', label:'accounts', num:true, def:true},
    {key:'posts', label:'posts', num:true},
    {key:'comments', label:'comments', num:true},
  ], s => `<tr>
    <td><a href="https://reddit.com/r/${esc(s.subreddit)}" target="_blank">r/${esc(s.subreddit)}</a></td>
    <td class="num">${fmt(s.accounts)}</td>
    <td class="num">${fmt(s.posts)}</td>
    <td class="num">${fmt(s.comments)}</td>
    <td class="members">${members(s.members)}</td>
  </tr>`);
  if(!rows.length) $('#subreddits').innerHTML =
    '<tbody><tr><td class="muted">No subreddit is shared by ≥2 accounts.</td></tr></tbody>';
}

async function loadThreads(){
  const { total, rows } = await getJSON('/api/threads');
  $('#thread-count').textContent = rows.length ? topOf(total, rows.length) : '';
  sortable($('#threads'), rows, [
    {key:'accounts', label:'accounts', num:true, def:true},
    {key:'comments', label:'comments', num:true},
    {key:'subreddit', label:'subreddit'},
    {key:'title', label:'thread'},
  ], t => `<tr>
    <td class="num">${fmt(t.accounts)}</td>
    <td class="num">${fmt(t.comments)}</td>
    <td><a href="https://reddit.com/r/${esc(t.subreddit)}" target="_blank">r/${esc(t.subreddit)}</a></td>
    <td><a href="https://redd.it/${esc(t.link_id)}" target="_blank">${esc(t.title) || t.link_id}</a>
        <div class="members">${members(t.members)}</div></td>
  </tr>`);
  if(!rows.length) $('#threads').innerHTML =
    '<tbody><tr><td class="muted">No thread is shared by ≥2 accounts.</td></tr></tbody>';
}

// ---- drawer ----
let cur = { user:null, kind:'posts', offset:0, limit:50 };
function openUser(u){ cur = { user:u, kind:'posts', offset:0, limit:50 };
  $('#drawer').classList.add('open'); setTab('posts'); loadContent(); }
$('#d-close').onclick = () => $('#drawer').classList.remove('open');
function setTab(kind){
  cur.kind = kind; cur.offset = 0;
  $('#tab-posts').classList.toggle('active', kind==='posts');
  $('#tab-comments').classList.toggle('active', kind==='comments');
}
$('#tab-posts').onclick = () => { setTab('posts'); loadContent(); };
$('#tab-comments').onclick = () => { setTab('comments'); loadContent(); };

function renderItem(kind, it){
  const head = `<div class="meta">r/${esc(it.subreddit)} · <b>${fmt(it.score)}</b> pts · ${day(it.created_utc)}</div>`;
  if(kind==='comments')
    return `<div class="item">${head}<div class="txt">${esc(it.body||'')}</div></div>`;
  return `<div class="item">${head}
    <div class="title">${esc(it.title||'(link)')}</div>
    ${it.selftext ? `<div class="txt">${esc(it.selftext)}</div>` : ''}
    ${it.url ? `<div><a href="${esc(it.url)}" target="_blank">${esc(it.url)}</a></div>` : ''}</div>`;
}

async function loadContent(){
  $('#d-user').textContent = cur.user;
  const q = `/api/content?u=${encodeURIComponent(cur.user)}&kind=${cur.kind}&limit=${cur.limit}&offset=${cur.offset}`;
  const r = await getJSON(q);
  const from = r.total ? r.offset+1 : 0, to = Math.min(r.offset+r.limit, r.total);
  $('#d-body').innerHTML =
    (r.items.map(it => renderItem(r.kind, it)).join('') ||
      '<p class="muted">Nothing here.</p>') +
    `<div class="pager">
       <button id="p-prev" ${r.offset<=0?'disabled':''}>‹ prev</button>
       <button id="p-next" ${to>=r.total?'disabled':''}>next ›</button>
       <span class="muted">${from}–${to} of ${fmt(r.total)}</span></div>`;
  $('#p-prev').onclick = () => { cur.offset=Math.max(0,cur.offset-cur.limit); loadContent(); };
  $('#p-next').onclick = () => { cur.offset+=cur.limit; loadContent(); };
  $('#d-body').scrollTo(0,0);
}
document.onkeydown = e => { if(e.key==='Escape') $('#drawer').classList.remove('open'); };

// ---- boot ----
(async () => {
  try {
    await loadOverview();
    await Promise.all([loadAccounts(), loadSubreddits(), loadThreads()]);
  } catch (e) { document.body.insertAdjacentHTML('afterbegin',
    `<p class="warn" style="padding:20px">${esc(e.message)}</p>`); }
})();
</script>
</body>
</html>
"""
