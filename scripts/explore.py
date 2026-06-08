"""Interactive, zero-dependency SQLite browser for the redditpages DB.

Replaces the old pandas notebook with a local web app: list tables, inspect
schema, page/sort/search rows, and run read-only SQL — all in the browser,
using nothing but the Python standard library.

    python scripts/explore.py                 # opens ../data/redditpages.db
    python scripts/explore.py --db other.db   # any SQLite file
    python scripts/explore.py --port 9000 --no-browser

The database is opened read-only; the SQL console only accepts a single
SELECT / WITH / PRAGMA / EXPLAIN statement, so nothing here can mutate data.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_ROWS = 500          # browse page cap
MAX_QUERY_ROWS = 1000   # SQL console cap


def default_db() -> str:
    """Mirror redditpages.db.data_db without importing the package, so this
    explorer stays pure-stdlib and runnable with no install."""
    base = os.environ.get("REDDITPAGES_DATA") or Path(__file__).resolve().parents[2] / "data"
    return str(Path(base) / "redditpages.db")


# --------------------------------------------------------------------------- #
# Data access (every request gets its own read-only connection)               #
# --------------------------------------------------------------------------- #

class DB:
    def __init__(self, path: str) -> None:
        self.path = str(Path(path).resolve())

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def tables(self) -> list[dict]:
        con = self._conn()
        try:
            names = [
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            out = []
            for name in names:
                cols = [
                    {"name": c["name"], "type": c["type"] or "", "pk": bool(c["pk"]),
                     "notnull": bool(c["notnull"])}
                    for c in con.execute(f'PRAGMA table_info("{name}")')
                ]
                rows = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                out.append({"name": name, "rows": rows, "columns": cols})
            return out
        finally:
            con.close()

    def _columns(self, con: sqlite3.Connection, table: str) -> list[str]:
        return [c["name"] for c in con.execute(f'PRAGMA table_info("{table}")')]

    def rows(self, table: str, *, limit: int, offset: int,
             order: str | None, direction: str, col: str, q: str) -> dict:
        con = self._conn()
        try:
            cols = self._columns(con, table)
            if not cols:
                raise ValueError(f"unknown table: {table}")

            where, params = "", []
            if q:
                like = f"%{q}%"
                targets = [col] if col in cols else cols
                clause = " OR ".join(f'CAST("{c}" AS TEXT) LIKE ?' for c in targets)
                where = f" WHERE {clause}"
                params = [like] * len(targets)

            total = con.execute(
                f'SELECT count(*) FROM "{table}"{where}', params
            ).fetchone()[0]

            order_sql = ""
            if order in cols:
                d = "DESC" if direction.lower() == "desc" else "ASC"
                order_sql = f' ORDER BY "{order}" {d}'

            limit = max(1, min(limit, MAX_ROWS))
            sql = f'SELECT * FROM "{table}"{where}{order_sql} LIMIT ? OFFSET ?'
            cur = con.execute(sql, [*params, limit, max(0, offset)])
            data = [list(r) for r in cur.fetchall()]
            return {"columns": cols, "rows": data, "total": total,
                    "limit": limit, "offset": max(0, offset)}
        finally:
            con.close()

    def query(self, sql: str) -> dict:
        stmt = sql.strip().rstrip(";").strip()
        if not stmt:
            raise ValueError("empty query")
        if ";" in stmt:
            raise ValueError("only one statement at a time")
        head = stmt.split(None, 1)[0].lower()
        if head not in {"select", "with", "pragma", "explain"}:
            raise ValueError("read-only console: SELECT / WITH / PRAGMA / EXPLAIN only")
        con = self._conn()
        try:
            cur = con.execute(stmt)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchmany(MAX_QUERY_ROWS)]
            truncated = cur.fetchone() is not None
            return {"columns": cols, "rows": rows, "truncated": truncated}
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# HTTP handler                                                                 #
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    db: DB  # injected on the server

    def log_message(self, format: str, *args) -> None:  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)

        def one(k: str, d: str = "") -> str:
            return q.get(k, [d])[0]

        try:
            if u.path == "/":
                self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
            elif u.path == "/api/meta":
                self._json({"db": self.db.path, "tables": self.db.tables()})
            elif u.path == "/api/rows":
                self._json(self.db.rows(
                    one("table"),
                    limit=int(one("limit", "50") or 50),
                    offset=int(one("offset", "0") or 0),
                    order=one("order") or None,
                    direction=one("dir", "asc"),
                    col=one("col"),
                    q=one("q"),
                ))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 400)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/query":
            self._json({"error": "not found"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
            self._json(self.db.query(payload.get("sql", "")))
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 400)


# --------------------------------------------------------------------------- #
# Frontend (single self-contained page, no external assets)                   #
# --------------------------------------------------------------------------- #

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>redditpages · db explorer</title>
<style>
  :root { --bg:#fff; --fg:#1a1a1a; --mut:#6b7280; --line:#e5e7eb; --accent:#d93a00;
          --hl:#fff7f3; --code:#f6f8fa; }
  * { box-sizing: border-box; }
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin:0;
         color:var(--fg); background:var(--bg); font-size:13px; }
  a { color:var(--accent); }
  .app { display:grid; grid-template-columns:240px 1fr; height:100vh; }
  aside { border-right:1px solid var(--line); overflow-y:auto; padding:16px 0; }
  aside h1 { font-size:13px; margin:0 16px 4px; letter-spacing:.04em; }
  aside .db { font-size:11px; color:var(--mut); margin:0 16px 14px; word-break:break-all; }
  .tlist { list-style:none; margin:0; padding:0; }
  .tlist li { padding:7px 16px; cursor:pointer; display:flex; justify-content:space-between;
              gap:8px; border-left:3px solid transparent; }
  .tlist li:hover { background:var(--hl); }
  .tlist li.active { background:var(--hl); border-left-color:var(--accent); font-weight:600; }
  .tlist .n { color:var(--mut); font-variant-numeric:tabular-nums; }
  main { display:flex; flex-direction:column; min-width:0; }
  .tabs { display:flex; gap:2px; border-bottom:1px solid var(--line); padding:0 16px; }
  .tab { padding:10px 14px; cursor:pointer; border-bottom:2px solid transparent; color:var(--mut); }
  .tab.active { color:var(--fg); border-bottom-color:var(--accent); font-weight:600; }
  .pane { flex:1; overflow:auto; padding:16px; min-height:0; }
  .pane.hidden { display:none; }
  .schema { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px; }
  .chip { background:var(--code); border:1px solid var(--line); border-radius:4px;
          padding:2px 8px; font-size:11px; }
  .chip b { color:var(--accent); }
  .chip .pk { color:#16a34a; font-weight:700; }
  .chip .ty { color:var(--mut); }
  .toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:12px; }
  input, select, button, textarea { font:inherit; }
  input[type=text], select { border:1px solid var(--line); border-radius:4px; padding:5px 8px;
          background:#fff; }
  input[type=text] { min-width:200px; }
  button { background:var(--accent); color:#fff; border:none; border-radius:4px;
           padding:6px 12px; cursor:pointer; }
  button.ghost { background:#fff; color:var(--fg); border:1px solid var(--line); }
  button:disabled { opacity:.4; cursor:default; }
  .muted { color:var(--mut); }
  table { border-collapse:collapse; width:100%; font-size:12px; }
  th, td { border:1px solid var(--line); padding:5px 8px; text-align:left; vertical-align:top;
           max-width:420px; }
  th { position:sticky; top:0; background:var(--code); cursor:pointer; white-space:nowrap;
       user-select:none; }
  th .arrow { color:var(--accent); }
  td { max-height:120px; overflow:hidden; }
  td .cell { display:block; max-height:120px; overflow:hidden; white-space:pre-wrap;
             word-break:break-word; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  td.null { color:#c026d3; font-style:italic; }
  tr:hover td { background:var(--hl); }
  .pager { display:flex; gap:8px; align-items:center; margin-top:12px; }
  textarea { width:100%; height:140px; border:1px solid var(--line); border-radius:6px;
             padding:10px; background:var(--code); resize:vertical; }
  .examples { display:flex; flex-wrap:wrap; gap:6px; margin:10px 0; }
  .examples button { background:#fff; color:var(--accent); border:1px solid var(--line);
                     font-size:11px; padding:4px 8px; }
  .err { color:#b91c1c; background:#fef2f2; border:1px solid #fecaca; border-radius:4px;
         padding:8px 10px; white-space:pre-wrap; }
  .note { color:var(--mut); margin:8px 0; }
</style>
</head>
<body>
<div class="app">
  <aside>
    <h1>db explorer</h1>
    <div class="db" id="dbpath">…</div>
    <ul class="tlist" id="tables"></ul>
  </aside>
  <main>
    <div class="tabs">
      <div class="tab active" data-tab="browse">Browse</div>
      <div class="tab" data-tab="sql">SQL</div>
    </div>

    <section class="pane" id="pane-browse">
      <div class="schema" id="schema"></div>
      <div class="toolbar">
        <select id="searchCol"><option value="">all columns</option></select>
        <input type="text" id="search" placeholder="search… (LIKE)">
        <select id="pageSize">
          <option>25</option><option selected>50</option><option>100</option><option>250</option>
        </select>
        <span class="muted" id="rowinfo"></span>
      </div>
      <div style="overflow:auto"><table id="grid"></table></div>
      <div class="pager">
        <button class="ghost" id="prev">‹ prev</button>
        <button class="ghost" id="next">next ›</button>
        <span class="muted" id="pageinfo"></span>
      </div>
    </section>

    <section class="pane hidden" id="pane-sql">
      <textarea id="sql" spellcheck="false" placeholder="SELECT * FROM user LIMIT 20"></textarea>
      <div class="examples" id="examples"></div>
      <div class="toolbar">
        <button id="run">Run ▸ <span class="muted" style="color:#fff">(⌘/Ctrl+↵)</span></button>
        <span class="muted" id="qinfo"></span>
      </div>
      <div id="qerr"></div>
      <div style="overflow:auto"><table id="qgrid"></table></div>
    </section>
  </main>
</div>

<script>
const $ = s => document.querySelector(s);
const fmt = n => n.toLocaleString();
let META = null, state = { table:null, offset:0, order:null, dir:'asc', cols:[] };

async function getJSON(url){ const r = await fetch(url); const j = await r.json();
  if(!r.ok || j.error) throw new Error(j.error || r.statusText); return j; }

function cellHTML(v, numeric){
  if(v === null) return '<td class="null">NULL</td>';
  const s = String(v);
  const cls = numeric ? 'num' : '';
  return `<td class="${cls}"><span class="cell">${escapeHTML(s)}</span></td>`;
}
function escapeHTML(s){ return s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

// ---- tabs ----
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  $('#pane-browse').classList.toggle('hidden', t.dataset.tab !== 'browse');
  $('#pane-sql').classList.toggle('hidden', t.dataset.tab !== 'sql');
});

// ---- sidebar ----
async function loadMeta(){
  META = await getJSON('/api/meta');
  $('#dbpath').textContent = META.db;
  const ul = $('#tables'); ul.innerHTML = '';
  META.tables.forEach(t => {
    const li = document.createElement('li');
    li.innerHTML = `<span>${t.name}</span><span class="n">${fmt(t.rows)}</span>`;
    li.onclick = () => selectTable(t.name);
    li.dataset.name = t.name;
    ul.appendChild(li);
  });
  if(META.tables.length) selectTable(META.tables[0].name);
  buildExamples();
}

function selectTable(name){
  state = { table:name, offset:0, order:null, dir:'asc', cols:[] };
  document.querySelectorAll('#tables li').forEach(li =>
    li.classList.toggle('active', li.dataset.name === name));
  const meta = META.tables.find(t => t.name === name);
  $('#schema').innerHTML = meta.columns.map(c =>
    `<span class="chip">${c.pk?'<span class="pk">⚷ </span>':''}<b>${c.name}</b> `
    + `<span class="ty">${c.type||'?'}</span></span>`).join('');
  const sel = $('#searchCol');
  sel.innerHTML = '<option value="">all columns</option>' +
    meta.columns.map(c => `<option>${c.name}</option>`).join('');
  $('#search').value = '';
  loadRows();
}

async function loadRows(){
  const limit = +$('#pageSize').value;
  const p = new URLSearchParams({ table:state.table, limit, offset:state.offset,
    col:$('#searchCol').value, q:$('#search').value });
  if(state.order){ p.set('order', state.order); p.set('dir', state.dir); }
  const data = await getJSON('/api/rows?' + p);
  state.cols = data.columns;
  const meta = META.tables.find(t => t.name === state.table);
  const numeric = new Set(meta.columns.filter(c =>
    /INT|REAL|NUM|FLOAT|DOUB/i.test(c.type)).map(c => c.name));

  const head = '<tr>' + data.columns.map(c => {
    const a = state.order===c ? `<span class="arrow">${state.dir==='asc'?'▲':'▼'}</span>` : '';
    return `<th data-c="${c}">${c} ${a}</th>`;
  }).join('') + '</tr>';
  const body = data.rows.map(r => '<tr>' +
    r.map((v,i) => cellHTML(v, numeric.has(data.columns[i]))).join('') + '</tr>').join('');
  $('#grid').innerHTML = head + body;
  $('#grid').querySelectorAll('th').forEach(th => th.onclick = () => {
    const c = th.dataset.c;
    if(state.order === c){ state.dir = state.dir==='asc'?'desc':'asc'; }
    else { state.order = c; state.dir = 'asc'; }
    state.offset = 0; loadRows();
  });

  const from = data.total ? data.offset+1 : 0;
  const to = Math.min(data.offset+limit, data.total);
  $('#rowinfo').textContent = `${fmt(data.total)} rows`;
  $('#pageinfo').textContent = `${fmt(from)}–${fmt(to)} of ${fmt(data.total)}`;
  $('#prev').disabled = data.offset <= 0;
  $('#next').disabled = to >= data.total;
}

$('#prev').onclick = () => { state.offset = Math.max(0, state.offset - +$('#pageSize').value); loadRows(); };
$('#next').onclick = () => { state.offset += +$('#pageSize').value; loadRows(); };
$('#pageSize').onchange = () => { state.offset = 0; loadRows(); };
let t; $('#search').oninput = () => { clearTimeout(t); t = setTimeout(() => { state.offset=0; loadRows(); }, 250); };
$('#searchCol').onchange = () => { state.offset=0; loadRows(); };

// ---- SQL console ----
const EXAMPLES = [
  ['Most active users',
   'SELECT author_username,\n'
 + '  (SELECT count(*) FROM post p WHERE p.author_username=u.username) AS posts,\n'
 + '  (SELECT count(*) FROM comment c WHERE c.author_username=u.username) AS comments\n'
 + 'FROM user u ORDER BY posts+comments DESC LIMIT 20'],
  ['Top karma',
   'SELECT author_username AS user, sum(score) AS karma, count(*) AS events\n'
 + 'FROM (SELECT author_username, score FROM post\n'
 + '      UNION ALL SELECT author_username, score FROM comment)\n'
 + 'GROUP BY 1 ORDER BY karma DESC LIMIT 20'],
  ['NSFW share by user',
   'SELECT author_username, count(*) posts, sum(over_18) nsfw,\n'
 + '  round(100.0*sum(over_18)/count(*),1) AS pct\n'
 + 'FROM post GROUP BY 1 HAVING nsfw>0 ORDER BY pct DESC'],
  ['Busiest subreddits',
   'SELECT subreddit_name, count(*) AS events FROM\n'
 + '  (SELECT subreddit_name FROM post UNION ALL SELECT subreddit_name FROM comment)\n'
 + 'GROUP BY 1 ORDER BY events DESC LIMIT 25'],
  ['Top link domains',
   "SELECT lower(substr(url, instr(url,'//')+2,\n"
 + "  instr(substr(url,instr(url,'//')+2)||'/', '/')-1)) AS host,\n"
 + "  count(*) n FROM post WHERE url LIKE 'http%' GROUP BY 1 ORDER BY n DESC LIMIT 20"],
  ['Most-moderated mods',
   'SELECT moderator_username, count(*) AS subs FROM moderator\n'
 + 'GROUP BY 1 ORDER BY subs DESC LIMIT 20'],
  ['Karma split (user)',
   'SELECT username, post_karma, comment_karma, num_posts, num_comments\n'
 + 'FROM user ORDER BY post_karma DESC LIMIT 20'],
];
function buildExamples(){
  $('#examples').innerHTML = '';
  EXAMPLES.forEach(([label, sql]) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.onclick = () => { $('#sql').value = sql; runQuery(); };
    $('#examples').appendChild(b);
  });
}
async function runQuery(){
  $('#qerr').innerHTML = ''; $('#qinfo').textContent = 'running…';
  try {
    const r = await fetch('/api/query', { method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ sql: $('#sql').value }) });
    const j = await r.json();
    if(!r.ok || j.error) throw new Error(j.error);
    const head = '<tr>' + j.columns.map(c => `<th>${escapeHTML(c)}</th>`).join('') + '</tr>';
    const body = j.rows.map(row => '<tr>' +
      row.map(v => cellHTML(v, typeof v === 'number')).join('') + '</tr>').join('');
    $('#qgrid').innerHTML = head + body;
    $('#qinfo').textContent = `${fmt(j.rows.length)} rows` + (j.truncated ? ` (capped)` : '');
  } catch(e){
    $('#qgrid').innerHTML = ''; $('#qinfo').textContent = '';
    $('#qerr').innerHTML = `<div class="err">${escapeHTML(e.message)}</div>`;
  }
}
$('#run').onclick = runQuery;
$('#sql').addEventListener('keydown', e => {
  if((e.metaKey || e.ctrlKey) && e.key === 'Enter') runQuery();
});

loadMeta().catch(e => $('#tables').innerHTML = `<li class="err">${e.message}</li>`);
</script>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# Entrypoint                                                                   #
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Local web browser for the redditpages SQLite DB.")
    ap.add_argument("--db", default=default_db())
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if not Path(args.db).exists():
        ap.error(f"database not found: {args.db}")

    Handler.db = DB(args.db)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"exploring {Path(args.db).resolve()}")
    print(f"  → {url}   (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
