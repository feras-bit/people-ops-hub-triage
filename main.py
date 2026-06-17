"""
People & Ops Hub — Asana triage Cloud Function.

Does what Asana's paid "Rules" would do, but via the Asana API so it runs on
GCP without an Asana Advanced licence. Idempotent reconcile sweep:

  • New form submission lands in "New (Untriaged)" with a Category set
        → move to the matching category lane, assign the owner,
          set Status = New, and set an SLA due date from Priority.
  • Status = Resolved   → move to "Resolved / Done" and mark complete.
  • Status = Waiting on Requester → move to the Waiting lane.

Only fills blanks (assignee / status / due date) — never overwrites values a
human already set — so it's safe to run every 15 min or fire on a webhook.

Trigger options:
  • Cloud Scheduler → HTTP (e.g. every 15 min), OR
  • Asana webhook on the project (near real-time). The handshake is handled.

Env vars (set via env or Secret Manager):
  ASANA_PAT          — Asana Personal Access Token (required)
  SLACK_BOT_TOKEN    — optional, for the Urgent ping
  SLACK_CHANNEL_ID   — optional, channel for Urgent pings (e.g. #help-desk)
"""

import json
import os
import urllib.request
import urllib.error
from datetime import date, timedelta

import functions_framework

# ── Board wiring (GIDs captured when the board was built) ──────────────────────
PROJECT_GID = "1215627193057064"
OWNER_GID   = "1210463619512303"   # Feras Tayeb — single owner for all lanes

# Sections (lanes)
SEC_NEW      = "1215627191691478"  # 📥 New (Untriaged)
SEC_QUINYX   = "1215627277462372"  # ⚡ Quinyx
SEC_IT       = "1215627135802517"  # 💻 IT Support
SEC_HR       = "1215627193570443"  # 👥 HR
SEC_HIRING   = "1215627276613031"  # 🆕 Hiring / New Joiner
SEC_CONTRACT = "1215627136413850"  # 📄 Contract Creation
SEC_WAITING  = "1215627193660301"  # ⏳ Waiting on Requester
SEC_RESOLVED = "1215627439831358"  # ✅ Resolved / Done
SEC_JOB_ORG  = os.environ.get("SEC_JOB_ORG", "")  # 🏢 Job & Org Change — fill once the lane exists

# Custom fields
F_CATEGORY = "1215681186120906"
F_PRIORITY = "1215688982374676"
F_STATUS   = "1215688982374682"

# Category options → target lane
CAT_QUINYX, CAT_JOBORG, CAT_IT  = "1215681186120907", "1215681186120908", "1215681186120909"
CAT_HR, CAT_HIRING, CAT_CONTRACT = "1215681186120910", "1215681186120911", "1215681186120912"
CAT_OTHER = "1215681186120913"

CATEGORY_TO_SECTION = {
    CAT_QUINYX:   SEC_QUINYX,
    CAT_IT:       SEC_IT,
    CAT_HR:       SEC_HR,
    CAT_HIRING:   SEC_HIRING,
    CAT_CONTRACT: SEC_CONTRACT,
    CAT_JOBORG:   SEC_JOB_ORG,   # only routes if SEC_JOB_ORG is set
    # CAT_OTHER intentionally unmapped → stays in New (Untriaged) for manual triage
}

# Priority options
PRI_LOW, PRI_MED, PRI_HIGH, PRI_URGENT = (
    "1215688982374677", "1215688982374678", "1215688982374679", "1215688982374680")
# SLA: business days from "today" to the due date
PRIORITY_SLA_DAYS = {PRI_URGENT: 0, PRI_HIGH: 2, PRI_MED: 5}  # Low → no due date

# Status options
ST_NEW, ST_INPROG, ST_WAITING, ST_RESOLVED = (
    "1215688982374683", "1215688982374684", "1215688982374685", "1215688982374686")

ASANA_BASE = "https://app.asana.com/api/1.0"


# ── Asana API helpers ─────────────────────────────────────────────────────────

def _asana(method, path, body=None):
    url = path if path.startswith("http") else f"{ASANA_BASE}{path}"
    data = json.dumps({"data": body}).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {os.environ['ASANA_PAT']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Asana {method} {path} -> {e.code}: {e.read().decode()}") from e


def fetch_open_tasks():
    """Incomplete tasks in the project with the fields we triage on."""
    opt = ("memberships.section.gid,memberships.project.gid,assignee.gid,"
           "due_on,completed,name,"
           "custom_fields.gid,custom_fields.enum_value.gid")
    tasks, url = [], f"{ASANA_BASE}/tasks?project={PROJECT_GID}&completed_since=now&opt_fields={opt}&limit=100"
    while url:
        resp = _asana("GET", url)
        tasks.extend(resp.get("data", []))
        nxt = (resp.get("next_page") or {}).get("uri")
        url = nxt
    return tasks


def enum_gid(task, field_gid):
    for cf in task.get("custom_fields", []):
        if cf.get("gid") == field_gid:
            ev = cf.get("enum_value")
            return ev.get("gid") if ev else None
    return None


def current_section(task):
    for m in task.get("memberships", []):
        if (m.get("project") or {}).get("gid") == PROJECT_GID:
            return (m.get("section") or {}).get("gid")
    return None


def move_to_section(task_gid, section_gid):
    _asana("POST", f"/sections/{section_gid}/addTask", {"task": task_gid})


def update_task(task_gid, fields):
    _asana("PUT", f"/tasks/{task_gid}", fields)


# ── Business-day SLA ──────────────────────────────────────────────────────────

def add_business_days(start, n):
    d = start
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:          # Mon–Fri
            added += 1
    return d


# ── Core reconcile ────────────────────────────────────────────────────────────

def triage_task(task, today):
    """Return a short log line if anything changed, else None."""
    gid      = task["gid"]
    sec      = current_section(task)
    cat      = enum_gid(task, F_CATEGORY)
    status   = enum_gid(task, F_STATUS)
    priority = enum_gid(task, F_PRIORITY)
    actions  = []

    # 1. Lifecycle takes precedence over routing.
    if status == ST_RESOLVED:
        if not task.get("completed"):
            update_task(gid, {"completed": True})
            actions.append("closed")
        if sec != SEC_RESOLVED:
            move_to_section(gid, SEC_RESOLVED)
            actions.append("→Resolved")
        return f"{task['name'][:40]}: {', '.join(actions)}" if actions else None

    if status == ST_WAITING:
        if sec != SEC_WAITING:
            move_to_section(gid, SEC_WAITING)
            return f"{task['name'][:40]}: →Waiting"
        return None

    # 2. Active tickets — only triage ones that carry a Category.
    if not cat or cat == CAT_OTHER:
        return None  # leave in New (Untriaged) for manual handling

    target = CATEGORY_TO_SECTION.get(cat)

    # Route out of New (Untriaged) into the category lane (don't disturb if
    # already moved by a human).
    if target and sec == SEC_NEW:
        move_to_section(gid, target)
        actions.append("routed")
        if priority == PRI_URGENT:   # one-time ping the moment an Urgent ticket lands
            notify_slack(f"🔴 URGENT ticket: *{task['name']}* — routed, same-day SLA.")

    # Fill blanks only — never overwrite human-set values.
    updates = {}
    if task.get("assignee") is None:
        updates["assignee"] = OWNER_GID
        actions.append("assigned")
    if status is None:
        updates.setdefault("custom_fields", {})[F_STATUS] = ST_NEW
        actions.append("status=New")
    if task.get("due_on") is None and priority in PRIORITY_SLA_DAYS:
        due = add_business_days(today, PRIORITY_SLA_DAYS[priority])
        updates["due_on"] = due.isoformat()
        actions.append(f"due={due.isoformat()}")

    if updates:
        update_task(gid, updates)

    return f"{task['name'][:40]}: {', '.join(actions)}" if actions else None


def run_sweep():
    today = date.today()
    tasks = fetch_open_tasks()
    changed, errors = [], []
    for t in tasks:
        try:
            line = triage_task(t, today)   # Urgent pings happen here, once, on routing
            if line:
                changed.append(line)
        except Exception as e:             # one bad ticket must never stop the rest
            errors.append(f"{t.get('name', '?')[:40]}: {e}")
    if errors:
        notify_slack("⚠️ People & Ops triage hit per-ticket errors:\n• " + "\n• ".join(errors))
    summary = {"scanned": len(tasks), "changed": len(changed),
               "errors": len(errors), "details": changed, "error_details": errors}
    print(json.dumps(summary))
    return summary


def digest_run():
    """Once-a-day summary of open Urgent + overdue tickets. Triggered via ?mode=digest."""
    today = date.today()
    iso = today.isoformat()
    urgent_open, overdue = [], []
    for t in fetch_open_tasks():
        if enum_gid(t, F_STATUS) == ST_RESOLVED:
            continue
        if enum_gid(t, F_PRIORITY) == PRI_URGENT:
            urgent_open.append(t["name"])
        due = t.get("due_on")
        if due and due < iso:
            overdue.append(f"{t['name']} (due {due})")
    blocks = []
    if urgent_open:
        blocks.append("🔴 *Open Urgent:*\n• " + "\n• ".join(urgent_open))
    if overdue:
        blocks.append("⏰ *Overdue:*\n• " + "\n• ".join(overdue))
    body = "\n\n".join(blocks) if blocks else "All clear — nothing urgent or overdue. ✅"
    notify_slack("📋 *People & Ops Hub — daily digest*\n" + body)
    return {"urgent_open": len(urgent_open), "overdue": len(overdue)}


def config_check():
    """Verify every hard-coded section/field/option GID still exists on the board.
    Cheap — run it via ?mode=selfcheck after any change to the board structure."""
    problems = []
    live_secs = {s["gid"] for s in _asana("GET", f"/projects/{PROJECT_GID}/sections")["data"]}
    expected = {"New": SEC_NEW, "Quinyx": SEC_QUINYX, "IT": SEC_IT, "HR": SEC_HR,
                "Hiring": SEC_HIRING, "Contract": SEC_CONTRACT,
                "Waiting": SEC_WAITING, "Resolved": SEC_RESOLVED}
    if SEC_JOB_ORG:
        expected["JobOrg"] = SEC_JOB_ORG
    for name, gid in expected.items():
        if gid not in live_secs:
            problems.append(f"section missing: {name} ({gid})")

    cfs = _asana("GET", f"/projects/{PROJECT_GID}?opt_fields="
                 "custom_field_settings.custom_field.gid,"
                 "custom_field_settings.custom_field.enum_options.gid")
    fields = {}
    for s in cfs["data"].get("custom_field_settings", []):
        cf = s["custom_field"]
        fields[cf["gid"]] = {o["gid"] for o in cf.get("enum_options", [])}
    for fname, fgid, opts in [
        ("Category", F_CATEGORY, [CAT_QUINYX, CAT_IT, CAT_HR, CAT_HIRING, CAT_CONTRACT, CAT_JOBORG, CAT_OTHER]),
        ("Priority", F_PRIORITY, [PRI_LOW, PRI_MED, PRI_HIGH, PRI_URGENT]),
        ("Status",   F_STATUS,   [ST_NEW, ST_INPROG, ST_WAITING, ST_RESOLVED]),
    ]:
        if fgid not in fields:
            problems.append(f"field missing: {fname} ({fgid})")
            continue
        for o in opts:
            if o not in fields[fgid]:
                problems.append(f"{fname}: option {o} missing")

    if problems:
        notify_slack("⚠️ People & Ops triage CONFIG DRIFT detected:\n• " + "\n• ".join(problems))
    return {"ok": not problems, "problems": problems}


# ── Optional Slack ping ───────────────────────────────────────────────────────

def notify_slack(text):
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL_ID")
    if not (token and channel):
        return
    try:
        from slack_sdk import WebClient
        WebClient(token=token).chat_postMessage(channel=channel, text=text)
    except Exception as e:  # never let a Slack hiccup fail the triage run
        print(f"slack ping failed: {e}")


# ── Cloud Function entry point ────────────────────────────────────────────────

@functions_framework.http
def triage(request):
    # Asana webhook handshake: echo the X-Hook-Secret on first registration.
    secret = request.headers.get("X-Hook-Secret")
    if secret:
        return ("", 200, {"X-Hook-Secret": secret})

    mode = request.args.get("mode")
    # Config self-check mode: ?mode=selfcheck → validate all GIDs, alert on drift.
    if mode == "selfcheck":
        report = config_check()
        return (json.dumps(report), 200 if report["ok"] else 409,
                {"Content-Type": "application/json"})
    # Daily digest mode: ?mode=digest → Slack summary of open Urgent + overdue.
    if mode == "digest":
        return (json.dumps(digest_run()), 200, {"Content-Type": "application/json"})

    # Normal triage run. Any uncaught failure → alert + HTTP 500 so the failure is
    # VISIBLE (Cloud Scheduler marks the job failed; Cloud Monitoring can alert).
    try:
        summary = run_sweep()
        return (json.dumps(summary), 200, {"Content-Type": "application/json"})
    except Exception as e:
        import traceback
        traceback.print_exc()
        notify_slack(f"🚨 People & Ops triage RUN FAILED: {e}")
        return (json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"})
