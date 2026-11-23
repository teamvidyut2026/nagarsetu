"""Civic Policy Kernel: the deterministic layer that decides.

The AI proposes a category, severity and hazards. The kernel turns that proposal into
a routed ticket with an SLA, or parks it for a human, using only rules in policy.json.
It also guards every state change: the AI can never move a ticket, a crew cannot close
its own work without geotagged proof, and only the citizen (or a timeout) can close.
Every decision returns the rule ids that fired, so the audit log explains itself.
"""
from __future__ import annotations

import re

from .config import haversine_m, in_city, policy

OPEN = ("needs_review", "routed", "assigned", "in_progress")
ACTIVE = OPEN + ("resolved",)

# Independent of the AI: instruction-like text is never auto-routed, even if the
# model (or the offline classifier) did not flag it.
INSTRUCTION_LIKE = re.compile(
    r"ignore (?:all |the )?(?:previous|above|prior)|mark (?:this |it )?(?:as )?(?:resolved|closed)|"
    r"set (?:the )?(?:priority|severity|status)|system prompt|admin override|"
    r"close (?:all|every) (?:ticket|complaint)", re.I)


def _rule(rules: list, rid: str, text: str) -> None:
    rules.append({"id": rid, "text": text})


def sla_hours(category: str, severity: int, pol: dict | None = None) -> float:
    pol = pol or policy()
    base = pol["categories"].get(category, pol["categories"]["other"])["sla_hours"]
    return round(base * pol["severity_sla_factor"][str(int(severity))], 2)


def decide_intake(proposal: dict, text: str, lat: float, lng: float, pol: dict | None = None) -> dict:
    """Turn an AI proposal into a kernel decision. Pure function."""
    pol = pol or policy()
    rules: list[dict] = []
    review: list[str] = []

    category = proposal.get("category")
    if category not in pol["categories"]:
        _rule(rules, "K-CAT", f"Unknown category '{category}' mapped to 'other'.")
        category = "other"

    try:
        severity = int(proposal.get("severity", 3))
    except (TypeError, ValueError):
        severity = 3
    severity = max(1, min(5, severity))

    hazards = [h for h in proposal.get("hazards") or [] if h in pol["hazards"]]
    for h in hazards:
        floor = pol["hazards"][h]["min_severity"]
        if severity < floor:
            _rule(rules, "K-HAZ", f"Hazard '{h}' raises severity {severity} -> {floor}.")
            severity = floor

    if not in_city(lat, lng):
        review.append("Location is outside Indore city limits.")
        _rule(rules, "K-GEO", "Pin outside the city boundary: human must verify location.")

    conf = float(proposal.get("confidence", 0) or 0)
    if conf < pol["triage"]["min_confidence"]:
        review.append(f"AI confidence {conf:.2f} below {pol['triage']['min_confidence']}.")
        _rule(rules, "K-CONF", "Low-confidence classification goes to human triage.")

    if proposal.get("suspicious") and pol["triage"]["suspicious_goes_to_review"]:
        review.append("AI flagged: " + (proposal.get("suspicion_reason") or "suspicious content"))
        _rule(rules, "K-SUS", "AI-flagged content is never auto-routed.")
    if INSTRUCTION_LIKE.search(text or ""):
        if not proposal.get("suspicious"):
            review.append("Kernel found instruction-like text the AI did not flag.")
        _rule(rules, "K-INJ", "Kernel pattern check: instruction-like text held for a human.")

    dept = pol["categories"][category]["dept"]
    sla = sla_hours(category, severity, pol)
    _rule(rules, "K-ROUTE", f"{category} -> {dept}; severity {severity} gives SLA {sla}h.")

    return {
        "status": "needs_review" if review else "routed",
        "category": category,
        "severity": severity,
        "hazards": hazards,
        "dept": dept,
        "sla_hours": sla,
        "review_reason": " ".join(review) or None,
        "rules": rules,
    }


def find_duplicate(lat: float, lng: float, category: str, now: float, open_tickets: list[dict],
                   pol: dict | None = None) -> dict | None:
    """Nearest open ticket of the same category within the dedup radius and window."""
    pol = pol or policy()
    d = pol["dedup"]
    best, best_dist = None, float("inf")
    for t in open_tickets:
        if t["category"] != category or t.get("parent_id") or t["status"] not in ACTIVE:
            continue
        if now - t["created_at"] > d["window_hours"] * 3600:
            continue
        dist = haversine_m(lat, lng, t["lat"], t["lng"])
        if dist <= d["radius_m"] and dist < best_dist:
            best, best_dist = t, dist
    return best


def supporter_bump(supporters: int, severity: int, pol: dict | None = None) -> int:
    """Severity after a new supporter joins. Crossing each step adds 1 (max 5)."""
    pol = pol or policy()
    steps = pol["dedup"]["supporter_severity_steps"]
    if supporters in steps:
        return min(5, severity + 1)
    return severity


def sla_state(t: dict, now: float, pol: dict | None = None) -> dict:
    """Escalation level, at-risk flag and breach for an open ticket."""
    pol = pol or policy()
    if t["status"] not in OPEN or not t.get("sla_due"):
        return {"level": t.get("level") or 1, "at_risk": False, "breached": False, "ratio": 0.0}
    total = max(1.0, t["sla_due"] - t["created_at"])
    ratio = (now - t["created_at"]) / total
    level = 1
    for lv in pol["escalation"]["levels"]:
        if ratio >= lv["after_sla_ratio"] and lv["after_sla_ratio"] > 0:
            level = lv["level"]
    level = max(level, t.get("level") or 1)  # never de-escalate
    return {"level": level, "at_risk": pol["escalation"]["at_risk_ratio"] <= ratio < 1.0,
            "breached": ratio >= 1.0, "ratio": round(ratio, 3)}


def level_role(level: int, pol: dict | None = None) -> str:
    pol = pol or policy()
    return next((l["role"] for l in pol["escalation"]["levels"] if l["level"] == level), "Ward Officer")


def pick_crew(dept: str, zone: str, crews: list[dict], load: dict[str, int]) -> dict | None:
    """Least-loaded crew of the department in the same zone; ties broken by id."""
    pool = [c for c in crews if c["dept"] == dept and c["zone"] == zone] or \
           [c for c in crews if c["dept"] == dept]
    if not pool:
        return None
    return min(pool, key=lambda c: (load.get(c["id"], 0), c["id"]))


class Denied(Exception):
    pass


def check_transition(t: dict, action: str, actor: str, payload: dict, now: float,
                     pol: dict | None = None) -> list[dict]:
    """Raise Denied if the action is not allowed. Returns the rules that allowed it."""
    pol = pol or policy()
    rules: list[dict] = []
    if actor == "ai":
        raise Denied("K-AI: the AI layer can propose but never change a ticket's state.")
    s = t["status"]
    if action == "approve":
        if actor != "officer" or s != "needs_review":
            raise Denied("K-REV: only an officer can release a ticket from human review.")
        _rule(rules, "K-REV", "Officer released the ticket from human review.")
    elif action == "reject":
        if actor != "officer" or s != "needs_review":
            raise Denied("K-REV: only tickets in review can be rejected, by an officer.")
        if not (payload.get("reason") or "").strip():
            raise Denied("K-REJ: a rejection needs a written reason (sent to the citizen).")
        _rule(rules, "K-REJ", "Rejected with a reason shared with the citizen.")
    elif action == "assign":
        if actor not in ("officer", "system") or s != "routed":
            raise Denied("K-ASN: only routed tickets can be assigned.")
        _rule(rules, "K-ASN", "Assigned to the least-loaded crew in the zone.")
    elif action == "start":
        if actor != "crew" or s != "assigned":
            raise Denied("K-WRK: only the assigned crew can start work.")
    elif action == "resolve":
        if actor != "crew" or s not in ("assigned", "in_progress"):
            raise Denied("K-RES: only the assigned crew can mark work done.")
        p_lat, p_lng = payload.get("lat"), payload.get("lng")
        if p_lat is None or p_lng is None:
            raise Denied("K-PRF: resolution needs a geotagged proof (crew location).")
        dist = haversine_m(float(p_lat), float(p_lng), t["lat"], t["lng"])
        if dist > pol["closure"]["proof_radius_m"]:
            raise Denied(f"K-PRF: proof was captured {dist:.0f} m away; must be within "
                         f"{pol['closure']['proof_radius_m']} m of the complaint.")
        if not (payload.get("note") or "").strip():
            raise Denied("K-PRF: resolution needs a short work note.")
        _rule(rules, "K-PRF", f"Geotagged proof {dist:.0f} m from the complaint accepted.")
    elif action == "confirm":
        if actor != "citizen" or s != "resolved":
            raise Denied("K-CLS: only the citizen can confirm a resolved ticket.")
        _rule(rules, "K-CLS", "Closed on citizen confirmation.")
    elif action == "reopen":
        if actor != "citizen" or s != "resolved":
            raise Denied("K-REO: only the citizen can reopen a resolved ticket.")
        _rule(rules, "K-REO", "Citizen rejected the fix: severity +1 and escalated one level.")
    elif action == "auto_close":
        if actor != "system" or s != "resolved":
            raise Denied("K-CLS: auto-close applies only to resolved tickets.")
        hrs = pol["closure"]["auto_confirm_hours"]
        resolved_at = t.get("resolved_at")
        if resolved_at is None or now - resolved_at < hrs * 3600:
            raise Denied(f"K-CLS: citizen has {hrs}h to respond before auto-close.")
        _rule(rules, "K-CLS", f"No citizen response in {hrs}h: auto-closed.")
    else:
        raise Denied(f"Unknown action '{action}'.")
    return rules
