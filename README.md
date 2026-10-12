# ntu-now-mcp

A personal, **read-only** connector that lets Claude see your NTU NOW
(Brightspace) account: modules, coursework and quiz deadlines, submission
status, announcements, calendar, grades, group membership, discussions, and
every lecture file — downloaded and converted to text so Claude can read and
search them.

It never writes anything to NOW. Your NTU login never leaves the machine the
connector runs on: Claude only ever sees the tool results.

Two ways in, same session: the Brightspace REST API for structured data, and a
real headless browser for anything that only renders in a browser (the
Work-To-Do widget, the newer content view, StudentHub pages) — Claude can read
the rendered page, follow links, and look at screenshots to find where
everything lives.

```
Claude (phone / desktop)  ──MCP──▶  ntu-now-mcp  ──your session──▶  now.ntu.ac.uk
```

## What Claude gets

| Tool | What it returns |
|---|---|
| `list_modules`, `get_module` | your modules with their org-unit ids |
| `list_assignments` | due dates, marks, group flag, instructions, briefs, **submitted / not submitted** |
| `list_quizzes` | quiz windows and due dates |
| `get_deadlines` | one merged, sorted list of everything due (assignments + quizzes + calendar), overdue flagged |
| `get_calendar`, `list_announcements`, `get_grades` | what they say |
| `list_content`, `download_content`, `download_assignment_attachments` | the Content tree (with *unread* flags) and the files themselves — including documents a page only links to ("slides available from this link") |
| `read_document`, `search_downloads`, `list_downloads` | read PDF/PPTX/DOCX/HTML as text, search across everything |
| `get_study_pack`, `list_units` | **one lecture/week, whole**: slides, lecture page, notes, lab sheets and readings in reading order with full text, plus the neighbouring weeks and the module's assessment pages — what Claude loads when you say "let's do lecture 4" |
| `list_groups`, `get_classlist`, `list_discussions`, `get_discussion_posts` | group work and forums |
| `list_new_content`, `download_file` | what appeared or changed since the last check (for the scheduled watcher); fetch any NOW-hosted file such as slides linked from a page |
| `sync_all` | pull everything into `data/snapshot.json` and download all new files |
| `now_api_get`, `now_page_text` | escape hatches: any Brightspace API GET, any NOW page as text |
| `get_timetable` | the teaching timetable from NOW's Timetable tool: every session with day, time, room, lecturer, type, module and group for this week and the next, plus what's on in the next 48 hours; cached 12 h |
| `now_browse`, `now_click`, `now_screenshot`, `now_browser_page` | a persistent logged-in browser on the server: JavaScript-rendered pages exactly as you see them — including what NOW embeds in frames (external tools such as the Timetable) — link maps, tables, screenshots Claude can look at |
| `now_status`, `now_login_*` | session health; sign back in from your phone (MFA approval in Authenticator) |

## How sign-in works

NOW hands sign-in to NTU's ADFS (`fs.ntu.ac.uk`), which asks for username,
password and usually an Authenticator prompt. The connector:

1. **First time** — you sign in yourself in a real browser window
   (`now-login`), or headlessly with credentials from `.env`. The resulting
   cookies are saved to `state/now.storage.json`.
2. **Day to day** — API calls use those cookies. A keep-alive ping every 20
   minutes stops the Brightspace session idling out, so one login lasts a long
   time.
3. **When it does lapse** — the connector re-runs the sign-in headlessly
   using the saved SSO cookies ("Keep me signed in" is ticked for you). If
   NTU wants MFA again, Claude will tell you to approve the prompt in
   Authenticator (`now_login_start` / `now_login_poll`); you never have to be
   at the laptop.

## Setup on your laptop (10 minutes)

Requires Python 3.11+.

```bash
git clone <this repo> ntu-now-mcp && cd ntu-now-mcp
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
playwright install chromium
cp .env.example .env                                    # defaults are fine for local use
now-login                                               # a window opens: sign in to NOW as normal
python -m pytest -q                                     # optional: unit tests
```

`now-login` closes itself once NOW's home page loads and prints where the
session was saved.

### Use it from the Claude desktop app

Add the server to Claude's MCP config (Claude desktop app → Settings →
Developer → Edit Config), using `deploy/claude_desktop_config.example.json`
as the template with the full path to your venv's Python. Restart Claude.
Then ask: *"check my NOW deadlines"*.

## Use it today without a server (laptop + tunnel)

If the VPS isn't ready yet, the laptop can serve the connector for now.  After
the laptop setup above, generate a secret and start HTTP mode:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # paste as NOW_HTTP_SECRET in .env
now-mcp --http                                                 # leave this running
```

In a second terminal, open a temporary public tunnel (no account needed):

```bash
# macOS: brew install cloudflared     Windows: winget install Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:8765
```

It prints a `https://….trycloudflare.com` address. In claude.ai go to
Settings → Connectors → *Add custom connector* and enter
`https://<that address>/<NOW_HTTP_SECRET>/mcp`. The URL changes each time the
tunnel restarts and only works while the laptop is awake; move to the server
below when you want it permanent — the saved session file moves with you.

## Zero-touch server (recommended)

Put this code in a GitHub repository (Claude can push it for you once GitHub
is connected in claude.ai), point a DNS name at a new Ubuntu 24.04 server,
and paste `deploy/cloud-init.yaml` (with your repo URL and hostname filled
in) as the server's cloud-init / user data when you create it. The server
installs Docker, locks the firewall, gets an HTTPS certificate, starts the
connector, pulls code updates every night at 04:17 and installs OS security
updates by itself. Then from the laptop:

```bash
scp state/now.storage.json root@YOUR-HOST:/opt/ntu-now-mcp/state/         # your NOW session
ssh root@YOUR-HOST cat /opt/ntu-now-mcp/CONNECTOR_URL /opt/ntu-now-mcp/OPS_URL   # paste both into claude.ai
```

## Maintenance by Claude (the optional ops service)

Off by default. With `OPS_ENABLE=1` in `.env` (then re-run
`deploy/setup-vps.sh`), the server also runs a small maintenance service,
`ops/ops_server.py`, as a systemd unit on the host, reachable at
`https://<host>/<OPS_SECRET>/mcp`.
Added to claude.ai as a second custom connector ("NTU Server Ops"), it lets
Claude check status and logs, pull and deploy code updates, restart the
connector and read or write config files — from any chat, with no SSH keys
to manage. The URL is in `OPS_URL`. A general command tool exists but is off
unless you set `OPS_ALLOW_SHELL=1` in `.env`.

Treat that URL as full access to the machine, because it is. Keep it only in
claude.ai's connector settings; if it ever leaks, change `OPS_SECRET` in
`.env` and run `systemctl restart ntu-now-ops && docker compose up -d`.

## Setup on a server (manual)

The desktop route only works while the laptop is on. For always-on access —
including the scheduled nagging — run it on a small VPS (any £4–6/month box
with Docker; 1 GB RAM is enough) and connect it to claude.ai as a custom
connector.

1. Point a DNS name at the server, e.g. `now-mcp.yourdomain.com`.
2. Copy the code up and run the setup script (installs Docker, firewall,
   secret, HTTPS, and starts everything):
   ```bash
   scp ntu-now-mcp.zip root@now-mcp.yourdomain.com:~        # from the laptop
   ssh root@now-mcp.yourdomain.com
   apt-get install -y unzip && unzip ntu-now-mcp.zip && cd ntu-now-mcp
   bash deploy/setup-vps.sh now-mcp.yourdomain.com
   ```
   It ends by printing the connector URL for claude.ai.
3. Get a session onto it, either way:
   - **copy the laptop's session**: `scp state/now.storage.json server:ntu-now-mcp/state/`
   - **or sign in headlessly on the server**: put `NOW_USERNAME` and
     `NOW_PASSWORD` in the server's `.env`, then
     `docker compose exec now-mcp python -m now_mcp.login --headless`
     and approve the MFA prompt on your phone. (With credentials in `.env`,
     Claude can also trigger this itself later via `now_login_start`.)
4. In claude.ai: Settings → Connectors → *Add custom connector* →
   URL `https://now-mcp.yourdomain.com/<NOW_HTTP_SECRET>/mcp`, no OAuth.
   Enable it in a chat and ask for your deadlines.

Custom connectors need a paid claude.ai plan; check the current plan details
if the option isn't there.

### Security model, honestly

- The endpoint is unlisted (`/<long-random-secret>/mcp`) over HTTPS. Anyone
  with the full URL can read your NOW account through it, so treat the URL
  like a password and rotate `NOW_HTTP_SECRET` if it ever leaks.
- `state/now.storage.json` **is** your NOW login. It is created with
  owner-only permissions and git-ignored; don't copy it anywhere else.
- `NOW_USERNAME` / `NOW_PASSWORD` in `.env` are optional. They let the server
  sign back in without you, at the cost of your password sitting on that
  server. Skip them if you'd rather re-run `now-login` on the laptop (or
  `scp` the state file) when the session eventually dies.
- Keep the VPS boring: firewall everything but 22/80/443, enable automatic
  updates, use SSH keys. Delete the server and change your NTU password if
  it is ever compromised.
- Everything is GET. The connector cannot submit, post, or change anything.
  The browser tools navigate and click but never type into forms.

## Day-to-day

- Session dead? Claude's tools return `error: needs_login` with what to do.
  From your phone: ask Claude to run `now_login_start`, approve the
  Authenticator prompt, done.
- Fresh materials: `download_content` only fetches new or changed files.
  `sync_all` does every module at once.
- Downloads live in `data/<MODULE CODE>/<content path>/…`; extracted text is
  cached in `data/_text_cache/`. Panopto/YouTube/SharePoint links can't be
  downloaded and are listed as `external_links_not_downloadable`.
- Studying one lecture: `get_study_pack(ou, "lecture 4")` (or `"week 12"`,
  `"activity 03"`, `"sequential data structures"`) returns that unit's files
  in full — slides first, then the lecture page, notes, lab sheets, readings —
  up to `max_chars`, with the rest listed under `read_separately` for
  `read_document`. `list_units(ou)` is the menu. Pages that only link to the
  real slides are followed automatically when content is downloaded, so the
  slides are in the pack too.
- Timetable: `get_timetable()` prefers the personal iCal feed (the "Subscribe to
  iCal" link on NOW's Timetable page) — give it once as `ical_url` or set
  `NOW_TIMETABLE_ICS`; it is remembered, needs no sign-in, and covers the whole
  year. Without a feed it drives NOW's Timetable tool through the browser (that
  tool lives on services.ntu.ac.uk and wants its own sign-in, so the feed is the
  reliable route). Cached in `data/_timetable.json` for 12 hours.
- The download index (`data/_index.json`) is written under a lock and merged,
  so parallel downloads can't lose each other's entries; if it is ever lost,
  the next `download_content` adopts the files already on disk instead of
  re-downloading or duplicating them.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `needs_login` immediately after login | Session file not where the server looks: check `NOW_STATE_DIR` and `now_status`. |
| `api_error 403` on one tool only, others fine | That API needs a permission students don't have (groups and classlist sometimes). `list_groups` falls back to the My Groups page; `now_page_text` reads anything else. |
| `api_error 404` | Endpoint/version not offered by NTU's instance — `now_status.api_versions` shows the live versions; `now_api_get` lets you try another path. |
| Browser doesn't launch | `playwright install chromium` in the venv (Docker image already has it). |
| Headless login stuck on "Waiting on Microsoft sign-in" | Microsoft is showing something new. `now_login_poll` returns the page text — read it, use `now_login_submit_code` if it wants a code, otherwise sign in on the laptop and copy the state file. |
| `Invalid Host header` in HTTP mode | `NOW_PUBLIC_HOST` must equal the hostname in the URL Claude uses. |
| Behind a corporate proxy | Set `NOW_BROWSER_PROXY=http://proxy:port` for the browser; httpx honours `HTTPS_PROXY`. |

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q
python -m now_mcp.server --verbose          # stdio, logs to stderr
python -m now_mcp.server --http --verbose   # Streamable HTTP on NOW_HTTP_HOST:NOW_HTTP_PORT
```

Layout: `config.py` (env), `session.py` (Playwright sign-in state machine +
cookie export), `client.py` (httpx Brightspace client with silent re-login),
`service.py` (shaping, downloads, deadline merge), `study.py` (study packs:
unit matching, reading order, link extraction), `timetable.py` (timetable
parsing), `browser.py` (persistent browser, frame-aware), `extract.py` (file → text),
`server.py` (the MCP tools), `login.py` (CLI).

Brightspace API reference: https://docs.valence.desire2learn.com/
