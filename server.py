#!/usr/bin/env python3
# Eclipse Apply MCP
# Copyright © 2026 Cyan Eclipse Technologies LLC · cetech.ai

"""Eclipse Apply MCP — v0.1.0.

Powered by Cyan Eclipse Technologies (cetech.ai).

The auto-apply tool that shows its work. The Agent Wallet pattern applied
to job hunting — POLICY-SCOPED AUTONOMY:

  * The human grants a written policy: target roles, salary floor,
    locations/remote, company allowlist + blocklist, daily cap, approval
    threshold, expiry, pause/kill switch. NOTHING is prepared or submitted
    outside that grant; refusals are first-class outcomes with reasons.
  * Review-first: matched jobs land as PREPARED applications. A human
    approves before anything moves.
  * Truthfulness is a hard, machine-checked rule. Tailoring re-orders and
    re-emphasizes REAL resume entries only. Every tailored resume ships
    with a verifiable diff: zero added facts, or it does not ship.
  * Every application, approval and refusal emits a RECEIPT into an
    append-only, hash-chained ledger.

v0.1 SUBMISSION IS DRY-RUN ONLY. The Greenhouse submission adapter builds
and validates the exact payload an application WOULD send, records it in
the receipt, and TRANSMITS NOTHING to any employer. There is deliberately
no code path in this file that posts an application. Live transmission is
a future, separately-gated feature (see README: live-submit unlock
criteria).

Transport: MCP over stdio (newline-delimited JSON-RPC 2.0), stdlib only.
State file: $ECLIPSE_APPLY_STATE or ./state.json next to this file.
"""
import difflib
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import discovery  # noqa: E402

STATE_PATH = Path(os.environ.get(
    "ECLIPSE_APPLY_STATE", Path(__file__).with_name("state.json")))
SUBMISSION_MODE = "DRY-RUN"  # v0.1: payloads are built + recorded, NEVER sent

STOPWORDS = set("""the a an and or of to in for with on at by from as is are was
were be been you your we our they their this that will who what have has had
not but all any can had her was one our out day get has him his how man new
now old see two way work team years experience including etc about into
using used use build building built develop developed development software
engineer engineering role job requirements qualifications plus strong""".split())


class ToolError(Exception):
    pass


def now() -> int:
    return int(time.time())


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------- state
def empty_state() -> dict:
    return {"profile": None, "policy": None, "paused": False,
            "jobs": {}, "applications": {}, "receipts": []}


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return empty_state()


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1))


# ------------------------------------------------------------------ receipts
def emit_receipt(state: dict, rtype: str, detail: dict) -> dict:
    """Append-only, hash-chained. Every receipt states the submission mode."""
    prev = state["receipts"][-1]["hash"] if state["receipts"] else "GENESIS"
    entry = {"seq": len(state["receipts"]) + 1, "ts": now(), "type": rtype,
             "mode": SUBMISSION_MODE, "prev_hash": prev, "detail": detail}
    entry["hash"] = sha(prev + canon({k: v for k, v in entry.items()
                                      if k != "hash"}))
    state["receipts"].append(entry)
    return entry


def verify_receipt_chain(receipts) -> bool:
    prev = "GENESIS"
    for e in receipts:
        if e.get("prev_hash") != prev:
            return False
        expect = sha(prev + canon({k: v for k, v in e.items() if k != "hash"}))
        if e.get("hash") != expect:
            return False
        prev = e["hash"]
    return True


# -------------------------------------------------------------- resume ingest
def parse_resume(text: str) -> dict:
    """Structure a plain-text/markdown resume AS WRITTEN.

    The parser may structure; it never embellishes: every skill, title,
    company and bullet in the profile is a verbatim slice of the source.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    name, headline = "", ""
    contact = [ln.strip() for ln in lines
               if "@" in ln and " " not in ln.strip()]
    skills, experience, education = [], [], []
    section, current = None, None
    for ln in lines:
        s = ln.strip()
        if s.startswith("# ") and not name:
            name = s[2:].strip()
            continue
        if s.startswith("## "):
            section = s[3:].strip().lower()
            current = None
            continue
        if s.startswith("### "):
            head = s[4:].strip()
            m = re.match(r"^(.*?)\s*[—–-]\s*(.*?)\s*\((.*?)\)\s*$", head)
            if m:
                current = {"title": m.group(1).strip(),
                           "company": m.group(2).strip(),
                           "dates": m.group(3).strip(), "bullets": []}
            else:
                current = {"title": head, "company": "", "dates": "",
                           "bullets": []}
            experience.append(current)
            continue
        if not s:
            continue
        if section == "skills" or (section is None and not experience
                                  and "," in s and not headline and "@" not in s
                                  and s.lower() not in (name.lower(),)):
            if section == "skills":
                items = re.split(r"[,\n]", s.lstrip("- ").strip())
                skills.extend(i.strip() for i in items if i.strip())
            continue
        if section == "experience" and current is not None:
            if s.startswith("- ") or s.startswith("* "):
                current["bullets"].append(s[2:].strip())
            continue
        if section == "education":
            if s.startswith("- ") or s.startswith("* "):
                education.append(s[2:].strip())
            continue
        if section is None and not headline and s != name and "@" not in s \
                and "SYNTHETIC" not in s and len(s) <= 80:
            headline = s
    source_hash = sha(text)
    return {"name": name, "headline": headline,
            "contact": contact[0] if contact else "",
            "skills": skills, "experience": experience,
            "education": education, "raw_text": text,
            "source_hash": source_hash,
            "version": "sha256:" + source_hash[:12], "ingested_at": now()}


def public_profile(profile: dict) -> dict:
    return {k: v for k, v in profile.items() if k != "raw_text"}


# -------------------------------------------------------------------- policy
def make_policy(args: dict) -> dict:
    pol = {
        "target_roles": [r.strip() for r in args.get("target_roles", []) if r.strip()],
        "salary_floor": args.get("salary_floor"),
        "locations": [l.strip() for l in args.get("locations", []) if l.strip()],
        "remote_ok": bool(args.get("remote_ok", True)),
        "allowlist": [c.strip().lower() for c in args.get("allowlist", []) if c.strip()],
        "blocklist": [c.strip().lower() for c in args.get("blocklist", []) if c.strip()],
        "daily_cap": int(args.get("daily_cap", 5)),
        "approval_threshold": int(args.get("approval_threshold", 60)),
        "granted_at": now(),
        "expires_at": (now() + int(args["expires_in_days"]) * 86400)
                      if args.get("expires_in_days") else None,
    }
    pol["snapshot_id"] = "pol_" + sha(canon(pol))[:12]
    return pol


def submissions_today(state: dict) -> int:
    return sum(1 for a in state["applications"].values()
               if a.get("submitted_day") == today())


def check_policy(state: dict, job: dict, match: dict = None):
    """Policy FIRST. Returns (allowed: bool, reasons: [str]).

    Every refusal names the clause that refused. No silent drops.
    """
    reasons = []
    if state.get("paused"):
        reasons.append("PAUSED: kill switch engaged — no applications "
                       "are prepared or submitted while paused")
        return False, reasons
    pol = state.get("policy")
    if not pol:
        return False, ["NO POLICY: no written grant exists — the agent may "
                       "not apply without a policy"]
    if pol.get("expires_at") and now() > pol["expires_at"]:
        reasons.append("POLICY EXPIRED: the grant's expiry has passed; "
                       "the owner must re-grant")
    company = (job.get("company") or "").lower()
    if company in pol["blocklist"]:
        reasons.append(f"BLOCKLIST: {job.get('company')} is on the policy "
                       f"blocklist")
    if pol["allowlist"] and company not in pol["allowlist"]:
        reasons.append(f"ALLOWLIST: {job.get('company')} is not on the "
                       f"policy allowlist")
    floor = pol.get("salary_floor")
    if floor is not None:
        if job.get("comp_max") is not None and job["comp_max"] < floor:
            reasons.append(f"SALARY FLOOR: published comp tops at "
                           f"{job['comp_max']} < floor {floor}")
        elif job.get("comp_min") is not None and job["comp_min"] >= floor:
            pass  # range clears the floor
    loc = (job.get("location") or "").lower()
    if pol["locations"]:
        loc_ok = any(l.lower() in loc for l in pol["locations"])
        if job.get("remote") and pol.get("remote_ok"):
            loc_ok = True
        if not loc_ok:
            reasons.append(f"LOCATION: '{job.get('location')}' matches none "
                           f"of the policy locations {pol['locations']} "
                           f"(remote_ok={pol.get('remote_ok')})")
    if match is not None:
        roles_hit = match["signals"]["title_matched_roles"]
        if pol["target_roles"] and not roles_hit:
            reasons.append(f"ROLE: title '{job.get('title')}' matches none "
                           f"of the target roles {pol['target_roles']}")
        if match["score"] < pol["approval_threshold"]:
            reasons.append(f"APPROVAL THRESHOLD: match score "
                           f"{match['score']} < threshold "
                           f"{pol['approval_threshold']} — not worth the "
                           f"owner's review time")
    if submissions_today(state) >= pol["daily_cap"]:
        reasons.append(f"DAILY CAP: {pol['daily_cap']} submissions already "
                       f"made today")
    return (len(reasons) == 0), reasons


# ------------------------------------------------------------------ matching
def _keywords(text: str):
    return {w for w in re.findall(r"[a-z][a-z0-9+#.]{2,}", (text or "").lower())
            if w not in STOPWORDS}


def match_job(profile: dict, job: dict, policy: dict = None) -> dict:
    """Transparent score 0-100. Every point is explained; no black box."""
    signals, rationale = {}, []
    # Title fit: 0-40
    title_words = _keywords(job.get("title", ""))
    roles = (policy or {}).get("target_roles") or [profile.get("headline", "")]
    matched_roles = [r for r in roles
                     if _keywords(r) & title_words or
                     r.lower() in (job.get("title") or "").lower()]
    title_pts = 40 if matched_roles else (15 if title_words &
                                          _keywords(profile.get("headline", ""))
                                          else 0)
    signals["title_fit"] = title_pts
    signals["title_matched_roles"] = matched_roles
    rationale.append(f"title fit {title_pts}/40: "
                     + (f"matches target role(s) {matched_roles}"
                        if matched_roles else "no target-role overlap"))
    # Skills overlap: 0-30, proportional to profile skills the job asks for
    job_words = _keywords(job.get("title", "") + " " +
                          job.get("description_text", ""))
    hit_skills = [s for s in profile.get("skills", [])
                  if s.lower() in job_words or
                  all(tok in job_words for tok in _keywords(s))]
    denom = max(4, min(10, len(profile.get("skills", []) or [4])))
    skills_pts = round(30 * min(1.0, len(hit_skills) / denom))
    signals["skills_overlap"] = skills_pts
    signals["skills_hit"] = hit_skills
    rationale.append(f"skills overlap {skills_pts}/30: job text names "
                     f"{len(hit_skills)} profile skills {hit_skills[:6]}")
    # Comp vs floor: 0-15 (unknown comp = half credit, stated honestly)
    floor = (policy or {}).get("salary_floor")
    if floor is None:
        comp_pts, note = 15, "no salary floor in policy"
    elif job.get("comp_max") is None:
        comp_pts, note = 7, "employer did not publish comp — cannot verify vs floor"
    elif job["comp_max"] >= floor:
        comp_pts, note = 15, f"published comp up to {job['comp_max']} >= floor {floor}"
    else:
        comp_pts, note = 0, f"published comp up to {job['comp_max']} < floor {floor}"
    signals["comp"] = comp_pts
    rationale.append(f"comp {comp_pts}/15: {note}")
    # Location: 0-15
    locs = (policy or {}).get("locations") or []
    if not locs:
        loc_pts, note = 15, "no location constraint in policy"
    elif job.get("remote") and (policy or {}).get("remote_ok", True):
        loc_pts, note = 15, "remote role, policy allows remote"
    elif any(l.lower() in (job.get("location") or "").lower() for l in locs):
        loc_pts, note = 15, f"location '{job.get('location')}' in policy locations"
    else:
        loc_pts, note = 0, f"location '{job.get('location')}' outside policy locations"
    signals["location"] = loc_pts
    rationale.append(f"location {loc_pts}/15: {note}")
    return {"score": title_pts + skills_pts + comp_pts + loc_pts,
            "signals": signals, "rationale": rationale}


# ----------------------------------------------------------------- tailoring
def tailor_resume(profile: dict, job: dict) -> dict:
    """Re-order / re-emphasize REAL entries only.

    Output is assembled exclusively from verbatim source lines (bullets,
    titles, companies, dates, skills). Nothing is reworded, nothing added.
    Returns the tailored text + a machine-checkable truthfulness proof.
    """
    job_words = _keywords(job.get("title", "") + " " +
                          job.get("description_text", ""))
    hit_skills = [s for s in profile["skills"]
                  if s.lower() in job_words]
    rest_skills = [s for s in profile["skills"] if s not in hit_skills]
    ordered_skills = hit_skills + rest_skills

    def entry_score(entry):
        blob = " ".join(entry["bullets"]).lower()
        return sum(1 for w in job_words if w in blob)

    entries = sorted(profile["experience"],
                     key=lambda e: -entry_score(e))
    lines = [f"# {profile['name']}", profile.get("headline", ""),
             profile.get("contact", ""), "", "## Skills",
             ", ".join(ordered_skills), "", "## Experience", ""]
    for e in entries:
        head = f"### {e['title']} — {e['company']}"
        if e.get("dates"):
            head += f" ({e['dates']})"
        lines.append(head)
        scored = sorted(e["bullets"],
                        key=lambda b: -sum(1 for w in job_words
                                           if w in b.lower()))
        for b in scored:
            lines.append(f"- {b}")
        lines.append("")
    if profile.get("education"):
        lines.append("## Education")
        for ed in profile["education"]:
            lines.append(f"- {ed}")
    tailored = "\n".join(lines).strip() + "\n"
    diff = "\n".join(difflib.unified_diff(
        profile["raw_text"].splitlines(), tailored.splitlines(),
        fromfile="resume.source", tofile="resume.tailored", lineterm=""))
    proof = verify_truthful(profile, tailored, ordered_skills)
    return {"tailored_text": tailored, "diff": diff,
            "diff_hash": "sha256:" + sha(diff)[:16], "proof": proof,
            "skills_emphasized": hit_skills}


def verify_truthful(profile: dict, tailored_text: str,
                    tailored_skills=None) -> dict:
    """Machine-checkable: the tailored resume adds ZERO new facts.

    Checks: (1) every experience bullet is verbatim from the source;
    (2) every (title, company, dates) heading exists in the source;
    (3) the tailored skill set equals the source skill set (order may
    differ); (4) the candidate name matches. Any violation is listed.
    """
    violations = []
    src_bullets = {b for e in profile["experience"] for b in e["bullets"]}
    src_heads = {(e["title"], e["company"], e["dates"])
                 for e in profile["experience"]}
    t = parse_resume(tailored_text)
    for e in t["experience"]:
        for b in e["bullets"]:
            if b not in src_bullets:
                violations.append(f"added bullet not in source: {b!r}")
        if (e["title"], e["company"], e["dates"]) not in src_heads:
            violations.append(f"added/altered role heading: {e['title']} / "
                                f"{e['company']} / {e['dates']}")
    if tailored_skills is not None and set(tailored_skills) != set(profile["skills"]):
        violations.append("skill set differs from source")
    if set(t["skills"]) != set(profile["skills"]):
        violations.append("parsed tailored skills differ from source skills")
    if t["name"] != profile["name"]:
        violations.append("candidate name differs from source")
    return {"truthful": not violations, "violations": violations,
            "rule": "tailoring = re-order/emphasis of verbatim source "
                    "facts only; zero added facts"}


# ------------------------------------------------------- Greenhouse dry-run
def build_greenhouse_payload(state: dict, app: dict) -> dict:
    """Construct + validate the payload a Greenhouse application WOULD
    carry. DRY-RUN: this dict is recorded in the receipt and RETURNED to
    the caller. It is never transmitted — v0.1 has no transmit code path.
    """
    profile, job = state["profile"], app["job"]
    payload = {
        "endpoint_shape": "POST https://boards.greenhouse.io/v1/boards/"
                          f"{job['board']}/jobs/{job['job_id'].split(':')[-1]}",
        "fields": {
            "first_name": profile["name"].split()[0],
            "last_name": " ".join(profile["name"].split()[1:]),
            "email": profile.get("contact", ""),
            "resume_text": app["tailored_text"],
            "job_id": job["job_id"].split(":")[-1],
            "source": "Eclipse Apply (DRY-RUN — not transmitted)",
        },
        "attachments": ["resume (tailored, diff-verified)"],
    }
    problems = []
    if not payload["fields"]["email"]:
        problems.append("profile has no contact email")
    if not payload["fields"]["resume_text"].strip():
        problems.append("tailored resume is empty")
    if not app["truthfulness"]["truthful"]:
        problems.append("truthfulness proof FAILED — payload must not ship")
    payload["validation"] = {"valid": not problems, "problems": problems,
                             "mode": SUBMISSION_MODE,
                             "transmitted": False}
    return payload


# --------------------------------------------------------------------- tools
def _need_profile(state):
    if not state.get("profile"):
        raise ToolError("no resume ingested yet — call ingest_resume first")
    return state["profile"]


def t_ingest_resume(state, args):
    text = args.get("resume_text") or ""
    if args.get("resume_path"):
        text = Path(args["resume_path"]).read_text()
    if not text.strip():
        raise ToolError("resume_text (or resume_path) is required")
    profile = parse_resume(text)
    state["profile"] = profile
    emit_receipt(state, "resume_ingested",
                 {"name": profile["name"], "version": profile["version"],
                  "skills": len(profile["skills"]),
                  "roles": len(profile["experience"]),
                  "note": "structured as written; nothing embellished"})
    return {"profile": public_profile(profile),
            "note": "Profile structured verbatim from the source resume."}


def t_set_policy(state, args):
    pol = make_policy(args)
    state["policy"] = pol
    receipt_text = (
        f"Application grant: roles {pol['target_roles'] or 'any'}, salary "
        f"floor {pol['salary_floor']}, locations {pol['locations'] or 'any'} "
        f"(remote_ok={pol['remote_ok']}), allowlist "
        f"{pol['allowlist'] or 'open'}, blocklist {pol['blocklist'] or 'none'}, "
        f"max {pol['daily_cap']} submissions/day, human review for every "
        f"application, matches below score {pol['approval_threshold']} are "
        f"refused outright, expiry {pol['expires_at']}. Nothing outside "
        f"this scope is authorized. Submission mode in v0.1: DRY-RUN.")
    emit_receipt(state, "policy_granted",
                 {"policy": pol, "grant_text": receipt_text})
    return {"policy": pol, "grant_text": receipt_text}


def t_get_policy(state, args):
    return {"policy": state.get("policy"), "paused": state.get("paused", False),
            "submissions_today": submissions_today(state),
            "submission_mode": SUBMISSION_MODE}


def t_search_jobs(state, args):
    boards = args.get("boards") or []
    if not boards:
        raise ToolError("boards is required: [{source, board}, ...]")
    found, errors = [], []
    for b in boards:
        try:
            jobs = discovery.discover(b["source"], b["board"])
            for j in jobs:
                state["jobs"][j["job_id"]] = j
            found.append({"source": b["source"], "board": b["board"],
                          "jobs": len(jobs)})
        except Exception as e:  # one bad board must not sink the search
            errors.append({"source": b.get("source"), "board": b.get("board"),
                           "error": str(e)[:200]})
    query = (args.get("query") or "").lower()
    cached = list(state["jobs"].values())
    if query:
        cached = [j for j in cached
                  if query in (j["title"] + " " + j["company"]).lower()]
    limit = int(args.get("limit", 25))
    return {"discovered": found, "errors": errors,
            "jobs": [{k: v for k, v in j.items() if k != "description_text"}
                     for j in cached[:limit]],
            "total_cached": len(state["jobs"])}


def t_match_jobs(state, args):
    profile = _need_profile(state)
    pol = state.get("policy")
    results = []
    for job in state["jobs"].values():
        m = match_job(profile, job, pol)
        allowed, reasons = check_policy(state, job, m)
        results.append({"job_id": job["job_id"], "title": job["title"],
                        "company": job["company"], "location": job["location"],
                        "url": job["url"], "score": m["score"],
                        "rationale": m["rationale"],
                        "policy_eligible": allowed,
                        "policy_reasons": reasons})
    results.sort(key=lambda r: -r["score"])
    return {"matches": results[:int(args.get("limit", 25))],
            "total": len(results), "submission_mode": SUBMISSION_MODE}


def t_prepare_application(state, args):
    profile = _need_profile(state)
    job = state["jobs"].get(args.get("job_id", ""))
    if not job:
        raise ToolError(f"unknown job_id {args.get('job_id')} — run "
                        f"search_jobs first")
    m = match_job(profile, job, state.get("policy"))
    allowed, reasons = check_policy(state, job, m)
    if not allowed:
        r = emit_receipt(state, "application_refused",
                         {"job_id": job["job_id"], "title": job["title"],
                          "company": job["company"], "score": m["score"],
                          "reasons": reasons,
                          "policy_snapshot": (state.get("policy") or {})
                          .get("snapshot_id")})
        return {"status": "refused", "reasons": reasons,
                "receipt_seq": r["seq"],
                "note": "Refusals are first-class outcomes — nothing was "
                        "prepared, and the refusal itself is on the record."}
    t = tailor_resume(profile, job)
    if not t["proof"]["truthful"]:
        r = emit_receipt(state, "application_refused",
                         {"job_id": job["job_id"],
                          "reasons": ["TRUTHFULNESS: tailored resume failed "
                                      "verification — " +
                                      "; ".join(t["proof"]["violations"])]})
        return {"status": "refused",
                "reasons": ["truthfulness verification failed"],
                "receipt_seq": r["seq"]}
    app_id = "app_" + uuid.uuid4().hex[:10]
    app = {"id": app_id, "job_id": job["job_id"], "job": job,
           "status": "prepared", "history": ["prepared"],
           "match": m, "tailored_text": t["tailored_text"],
           "diff_hash": t["diff_hash"], "truthfulness": t["proof"],
           "skills_emphasized": t["skills_emphasized"],
           "resume_version": profile["version"],
           "policy_snapshot": state["policy"]["snapshot_id"],
           "created_at": now(), "submitted_day": None, "payload": None}
    state["applications"][app_id] = app
    r = emit_receipt(state, "application_prepared",
                     {"application_id": app_id, "job_id": job["job_id"],
                      "title": job["title"], "company": job["company"],
                      "score": m["score"], "rationale": m["rationale"],
                      "resume_version": profile["version"],
                      "diff_hash": t["diff_hash"],
                      "truthfulness": t["proof"],
                      "policy_snapshot": app["policy_snapshot"]})
    return {"status": "prepared", "application_id": app_id,
            "match": m, "diff_hash": t["diff_hash"],
            "truthfulness": t["proof"], "receipt_seq": r["seq"],
            "note": "Review-first: nothing is submitted until the owner "
                    "calls approve_application."}


def t_approve_application(state, args):
    app = state["applications"].get(args.get("application_id", ""))
    if not app:
        raise ToolError(f"unknown application {args.get('application_id')}")
    if app["status"] != "prepared":
        raise ToolError(f"application is '{app['status']}', not 'prepared'")
    # Re-check policy at approval time — grants can expire / pause mid-queue
    allowed, reasons = check_policy(state, app["job"], app["match"])
    if not allowed:
        r = emit_receipt(state, "application_refused",
                         {"application_id": app["id"],
                          "job_id": app["job_id"],
                          "stage": "approval", "reasons": reasons})
        return {"status": "refused", "reasons": reasons,
                "receipt_seq": r["seq"]}
    app["status"] = "approved"
    app["history"].append("approved")
    source = app["job"]["source"]
    held = source != "greenhouse"
    if held:
        # v0.1 has a submission adapter for Greenhouse only. A non-
        # Greenhouse approval is a HOLD, not a dead end: the application
        # stays in 'approved', the hold is stored on the record and in
        # the receipt, and the caller gets the same structured fields a
        # dry-run submission returns (transmitted, mode, receipt_seq).
        app["hold"] = {
            "reason": "no_submission_adapter_v0.1",
            "source": source,
            "detail": f"v0.1 dry-run submission covers Greenhouse only; "
                      f"no {source} submission adapter exists yet",
            "transmitted": False}
    r = emit_receipt(state, "application_approved",
                     {"application_id": app["id"], "job_id": app["job_id"],
                      "approved_by": "owner", "source": source,
                      "held": held,
                      "hold_reason": (app["hold"]["reason"] if held
                                      else None),
                      "transmitted": False})
    if held:
        return {"status": "approved", "application_id": app["id"],
                "held": True, "hold_reason": app["hold"]["reason"],
                "source": source, "transmitted": False,
                "submission_mode": SUBMISSION_MODE,
                "receipt_seq": r["seq"],
                "plain_language": f"APPROVED AND HELD: your application "
                        f"for {app['job']['title']} at "
                        f"{app['job']['company']} ({source}) was approved "
                        f"but NOT sent — v0.1 can only dry-run Greenhouse "
                        f"submissions, and nothing was transmitted to the "
                        f"employer.",
                "note": "Held in 'approved' state for owner review. It "
                        "will not move or transmit on its own; a "
                        f"{source} submission path is future gated work "
                        f"(live-submit stays OFF in v0.1)."}
    payload = build_greenhouse_payload(state, app)
    app["payload"] = payload
    if not payload["validation"]["valid"]:
        raise ToolError("payload validation failed: " +
                        "; ".join(payload["validation"]["problems"]))
    app["status"] = "submitted_dry_run"
    app["history"].append("submitted_dry_run")
    app["submitted_day"] = today()
    r = emit_receipt(state, "application_submitted_dry_run",
                     {"application_id": app["id"], "job_id": app["job_id"],
                      "title": app["job"]["title"],
                      "company": app["job"]["company"],
                      "resume_version": app["resume_version"],
                      "diff_hash": app["diff_hash"],
                      "policy_snapshot": app["policy_snapshot"],
                      "payload": payload,
                      "transmitted": False,
                      "plain_language": f"DRY-RUN: a Greenhouse application "
                      f"for {app['job']['title']} at {app['job']['company']} "
                      f"was built and validated but NOT sent. No employer "
                      f"received anything."})
    return {"status": "submitted_dry_run", "application_id": app["id"],
            "receipt_seq": r["seq"], "payload_validation": payload["validation"],
            "transmitted": False,
            "note": "DRY-RUN — payload constructed, validated and recorded. "
                    "Nothing was transmitted to the employer."}


def t_reject_application(state, args):
    app = state["applications"].get(args.get("application_id", ""))
    if not app:
        raise ToolError(f"unknown application {args.get('application_id')}")
    if app["status"] not in ("prepared", "approved"):
        raise ToolError(f"cannot reject an application in state "
                        f"'{app['status']}'")
    app["status"] = "rejected"
    app["history"].append("rejected")
    r = emit_receipt(state, "application_rejected",
                     {"application_id": app["id"], "job_id": app["job_id"],
                      "reason": args.get("reason", "owner rejected")})
    return {"status": "rejected", "application_id": app["id"],
            "receipt_seq": r["seq"]}


def t_list_receipts(state, args):
    ok = verify_receipt_chain(state["receipts"])
    limit = int(args.get("limit", 50))
    return {"receipts": state["receipts"][-limit:],
            "total": len(state["receipts"]), "chain_valid": ok,
            "mode": SUBMISSION_MODE}


def t_get_tracker(state, args):
    apps = [{"id": a["id"], "job_id": a["job_id"], "title": a["job"]["title"],
             "company": a["job"]["company"], "status": a["status"],
             "history": a["history"], "score": a["match"]["score"],
             "diff_hash": a["diff_hash"], "created_at": a["created_at"],
             "hold": a.get("hold")}
            for a in state["applications"].values()]
    refused = sum(1 for r in state["receipts"]
                  if r["type"] == "application_refused")
    return {"applications": apps, "refusals_on_record": refused,
            "counts": {s: sum(1 for a in apps if a["status"] == s)
                       for s in ("prepared", "approved", "rejected",
                                 "submitted_dry_run", "closed")},
            "submission_mode": SUBMISSION_MODE}


def t_pause(state, args):
    state["paused"] = True
    r = emit_receipt(state, "paused",
                     {"reason": args.get("reason", "owner paused"),
                      "note": "Kill switch: no prepares, no submissions."})
    return {"paused": True, "receipt_seq": r["seq"]}


def t_resume(state, args):
    state["paused"] = False
    r = emit_receipt(state, "resumed", {"by": "owner"})
    return {"paused": False, "receipt_seq": r["seq"]}


def t_delete_my_data(state, args):
    if args.get("confirm") is not True:
        raise ToolError("delete_my_data requires confirm=true — this "
                        "purges the profile, policy, applications and the "
                        "entire receipts ledger")
    counts = {"profile": 1 if state.get("profile") else 0,
              "policy": 1 if state.get("policy") else 0,
              "applications": len(state["applications"]),
              "receipts": len(state["receipts"]),
              "cached_jobs": len(state["jobs"])}
    fresh = empty_state()
    state.clear()
    state.update(fresh)
    return {"purged": counts, "verified_empty": state == empty_state(),
            "note": "All personal data deleted from the local state file. "
                    "The ledger is gone by design — deletion beats audit "
                    "when the owner says delete."}


TOOLS = {
    "ingest_resume": (t_ingest_resume,
                      "Parse a plain-text or markdown resume into the stored candidate profile, structured verbatim from the source — the parser structures, it never embellishes, rewords, or adds facts. Use this first: match_jobs and prepare_application cannot run until a profile exists. Use resume_path to load from a local file or resume_text to pass the text directly (resume_path takes precedence). Expected format: a '# Name' heading, a '## Skills' section with comma-separated skills, and '## Experience' entries headed '### Title — Company (dates)' with '- ' bullets; an '## Education' section is optional. Mutating and persistent: replaces any existing profile in the local state file and appends a hash-chained 'resume_ingested' receipt. The raw resume text is stored locally with the profile; nothing is transmitted anywhere. Returns {profile, note} — the profile excludes the raw text but carries name, headline, contact, skills, experience, education, and a version string derived from the source hash.",
                      {"resume_text": {"type": "string", "description": "Full resume text in plain text/markdown, in the heading format described. Use resume_path instead to load from disk."},
                       "resume_path": {"type": "string", "description": "Local file path to read the resume from; when given it takes precedence over resume_text."}}, []),
    "set_policy": (t_set_policy,
                   "Record the owner's written application policy — the grant that bounds everything this server may prepare. Use before match_jobs or prepare_application: with no policy, every preparation is refused with a NO POLICY reason. Mutating and persistent: replaces any existing policy, stamps granted_at and a content-derived snapshot_id, and appends a hash-chained 'policy_granted' receipt containing the human-readable grant text. All fields are optional and narrow or widen the grant: target_roles (job titles must match one), salary_floor (jobs whose published comp tops out below it are refused; unpublished comp cannot be verified), locations plus remote_ok, allowlist (when non-empty, only these companies are eligible) and blocklist (never eligible, wins over allowlist), daily_cap (max dry-run submissions per day, default 5), approval_threshold (match score below which jobs are refused as not worth review, default 60), expires_in_days (grant expiry; omit for no expiry). Returns {policy, grant_text}. Submissions in v0.1 are DRY-RUN only regardless of policy.",
                   {"target_roles": {"type": "array", "items": {"type": "string"}, "description": "Role titles to target, e.g. ['Senior Software Engineer']. A job whose title matches none is refused when the policy is checked with a match."},
                    "salary_floor": {"type": "number", "description": "Minimum acceptable compensation as a number in the job's published currency/units. Jobs with comp_max below it are refused."},
                    "locations": {"type": "array", "items": {"type": "string"}, "description": "Acceptable location substrings, e.g. ['Seattle', 'Remote']. Empty means no location constraint."},
                    "remote_ok": {"type": "boolean", "description": "Whether remote jobs satisfy the location constraint; defaults to true."},
                    "allowlist": {"type": "array", "items": {"type": "string"}, "description": "Company names (case-insensitive). When non-empty, only these companies are eligible."},
                    "blocklist": {"type": "array", "items": {"type": "string"}, "description": "Company names (case-insensitive) that are never eligible, even if allowlisted."},
                    "daily_cap": {"type": "integer", "description": "Maximum dry-run submissions per calendar day (UTC); defaults to 5."},
                    "approval_threshold": {"type": "integer", "description": "Minimum match score (0-100) for a job to be preparable; defaults to 60."},
                    "expires_in_days": {"type": "integer", "description": "Days from now until the grant expires; omit for a grant with no expiry."}}, []),
    "get_policy": (t_get_policy,
                   "Read the current application policy, the pause state, how many dry-run submissions were made today, and the submission mode. Use to check what grant is in force before matching or preparing; use set_policy to create or replace the grant. Read-only and takes no parameters. The policy field is null when no policy has been granted yet. Returns {policy, paused, submissions_today, submission_mode}.",
                   {}, []),
    "search_jobs": (t_search_jobs,
                    "Fetch live job listings from public ATS boards and cache them locally for matching. Use to discover jobs before match_jobs; use match_jobs to score what is already cached. This is the one tool that makes network calls: it GETs the public JSON feeds of the named boards (sources: greenhouse, lever, ashby, workable) over HTTPS with no authentication. boards is required — a list of {source, board} objects naming each company's board slug. Mutating: fetched jobs are normalized and merged into the local job cache in the state file (no receipt is emitted for a search). A board that fails is reported in errors and does not sink the others. Optional query filters the cached results by case-insensitive substring of title + company; limit caps returned jobs (default 25). Returns {discovered, errors, jobs, total_cached}; returned jobs omit the full description_text.",
                    {"boards": {"type": "array", "items": {"type": "object"}, "description": "Required list of {source, board} objects; source is one of greenhouse, lever, ashby, workable and board is that company's board slug on the ATS."},
                     "query": {"type": "string", "description": "Optional substring filter over cached job titles and company names."},
                     "limit": {"type": "integer", "description": "Maximum jobs to return; defaults to 25. total_cached always reports the full cache size."}}, ["boards"]),
    "match_jobs": (t_match_jobs,
                   "Score every cached job against the stored profile and policy on a transparent 0-100 scale — title fit 0-40, skills overlap 0-30, comp vs salary floor 0-15, location 0-15 — with a written rationale for every point and a policy_eligible verdict with named refusal reasons. Use after search_jobs to rank what to prepare; use prepare_application to build the application for one chosen job. Read-only: scoring changes nothing and emits no receipt, but it requires an ingested profile (fails with a 'no resume ingested' error otherwise — call ingest_resume first). Results are sorted by score descending; limit caps the list (default 25) while total reports all scored jobs. Returns {matches, total, submission_mode}, each match carrying job_id, title, company, location, url, score, rationale, policy_eligible, and policy_reasons.",
                   {"limit": {"type": "integer", "description": "Maximum matches to return after sorting; defaults to 25."}}, []),
    "prepare_application": (t_prepare_application,
                            "Build a PREPARED application for one cached job: policy-check first, then tailor the resume and attach a machine-checkable truthfulness proof. Use for a job match_jobs shows as policy-eligible; the result still needs approve_application before anything else happens — preparing submits nothing. Tailoring only re-orders and re-emphasizes verbatim resume entries; if the tailored text adds any fact, verification fails and the application is refused. Policy failures (paused, no/expired policy, blocklist/allowlist, salary floor, location, role, score below threshold, daily cap) are NOT errors: they return {status: 'refused', reasons, receipt_seq} and the refusal is written to the hash-chained ledger as a first-class outcome. Mutating and persistent on both paths. Fails with an error for an unknown job_id (run search_jobs first) or when no resume is ingested. On success returns {status: 'prepared', application_id, match, diff_hash, truthfulness, receipt_seq, note} — the diff between source and tailored resume is verifiable from diff_hash.",
                            {"job_id": {"type": "string", "description": "Cached job id from search_jobs or match_jobs, e.g. 'greenhouse:<board>:<id>'."}}, ["job_id"]),
    "approve_application": (t_approve_application,
                            "Approve one PREPARED application — the human decision point. Use only on an application_id from prepare_application whose status is 'prepared'; use reject_application to decline instead. Behaviour branches by job source and transmits NOTHING in v0.1 either way: for Greenhouse jobs it builds and validates the exact submission payload, records a 'submitted_dry_run' receipt with the payload, and returns {status: 'submitted_dry_run', transmitted: false, payload_validation, receipt_seq}; for any other source (lever, ashby, workable) it returns a structured APPROVED-AND-HELD response {status: 'approved', held: true, hold_reason: 'no_submission_adapter_v0.1', transmitted: false, receipt_seq} and the application stays in 'approved' for owner review. Policy is re-checked at approval time — a grant that expired or a pause engaged since preparation turns the approval into a recorded refusal {status: 'refused', reasons, receipt_seq}. Approving a non-prepared application is an error. Mutating and persistent; every outcome is receipted.",
                            {"application_id": {"type": "string", "description": "Application id returned by prepare_application, e.g. 'app_xxxxxxxxxx'."}}, ["application_id"]),
    "reject_application": (t_reject_application,
                           "Reject one prepared or approved application so it never proceeds, recording the rejection in the hash-chained ledger. Use instead of approve_application when the owner declines a prepared application; there is no undo — a rejected application cannot be approved later (prepare a fresh one instead). Mutating and persistent. Rejecting an application in any other state (rejected, submitted_dry_run, closed) or an unknown id is an error. Returns {status: 'rejected', application_id, receipt_seq}.",
                           {"application_id": {"type": "string", "description": "Application id returned by prepare_application."},
                            "reason": {"type": "string", "description": "Why it was rejected, stored in the receipt; defaults to 'owner rejected'."}}, ["application_id"]),
    "list_receipts": (t_list_receipts,
                      "Read the append-only, hash-chained receipts ledger — grants, resume ingests, prepares, approvals, refusals, dry-run submissions, pause/resume — and verify the chain. Use to audit exactly what happened and in what order; use get_tracker for application states rather than raw events. Read-only. Returns the most recent receipts up to limit (default 50) plus {total, chain_valid, mode}; chain_valid false means the ledger was tampered with or corrupted. Receipts are personal data too: delete_my_data purges them.",
                      {"limit": {"type": "integer", "description": "Maximum receipts to return, taken from the end of the ledger; defaults to 50."}}, []),
    "get_tracker": (t_get_tracker,
                    "Summarize every application's current state — id, job, title, company, status (prepared, approved, rejected, submitted_dry_run, closed), history, match score, diff_hash, and any hold — plus per-status counts and the total number of refusals on record. Use for the pipeline overview; use list_receipts for the underlying event ledger and prepare_application / approve_application to move an application. Read-only, takes no parameters, and note 'submitted_dry_run' means a validated payload was recorded — in v0.1 nothing was ever transmitted to an employer. Returns {applications, refusals_on_record, counts, submission_mode}.",
                    {}, []),
    "pause": (t_pause,
              "Engage the kill switch: set the paused flag so prepare_application and approve_application refuse everything with a PAUSED reason until resume is called. Use to halt all application activity instantly; use get_policy to check the current pause state. Mutating and persistent, and the pause itself is written to the receipts ledger. Pausing does not delete anything and does not block read-only tools (search, match, tracker, receipts). Returns {paused: true, receipt_seq}.",
              {"reason": {"type": "string", "description": "Why the pause was engaged, stored in the receipt; defaults to 'owner paused'."}}, []),
    "resume": (t_resume,
               "Lift a pause previously set by the pause tool so preparing and approving can proceed again. Despite the name, this tool is about the pause switch only — it has nothing to do with the resume document (that is ingest_resume). Use after pause when the owner wants activity to continue; use get_policy to check the state first. Mutating and persistent, and the resume event is written to the receipts ledger. Lifting a pause does not change the policy or revive expired grants. Takes no parameters. Returns {paused: false, receipt_seq}.",
               {}, []),
    "delete_my_data": (t_delete_my_data,
                       "Permanently purge ALL local data — profile (including the stored raw resume text), policy, applications, cached jobs, and the entire receipts ledger — from the state file. Use only on the owner's explicit instruction; use pause instead when activity should merely stop. IRREVERSIBLE: the hash-chained ledger is deleted by design, so no audit trail survives, and nothing can be recovered or re-verified afterwards. Guarded: it refuses unless confirm is exactly true. No receipt can be written for the deletion itself, since the ledger is part of what is purged. Returns {purged, verified_empty, note} with the per-category counts that were deleted.",
                       {"confirm": {"type": "boolean", "description": "Must be exactly true or the call is refused; this is the only confirmation gate before an irreversible purge."}}, ["confirm"]),
}


def tool_list():
    return [{"name": name, "description": desc,
             "inputSchema": {"type": "object", "properties": props,
                             "required": req}}
            for name, (_, desc, props, req) in TOOLS.items()]


def handle(req: dict):
    method, rid = req.get("method", ""), req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req.get("params", {}).get(
                "protocolVersion", "2025-03-26"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "eclipse-apply", "version": "0.1.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tool_list()}}
    if method == "tools/call":
        name = req["params"]["name"]
        args = req["params"].get("arguments", {})
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32602, "message": f"unknown tool {name}"}}
        state = load_state()
        try:
            out = TOOLS[name][0](state, args)
            save_state(state)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(out, indent=1)}]}}
        except ToolError as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps({"error": str(e)})}],
                "isError": True}}
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid, "error": {
            "code": -32601, "message": f"method not found: {method}"}}
    return None  # notification


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except Exception as e:  # never crash the transport
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

# Eclipse Apply MCP · © 2026 Cyan Eclipse Technologies LLC · cetech.ai
