"""Orchestration: citizen report -> AI proposal -> kernel decision -> dedup -> crew.

Also the SLA sweep (escalation, auto-close), analytics (hotspots, predicted breaches,
ward scorecards) and the commissioner brief.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict

from . import kernel
from .config import city, crews, nearest_ward, policy, ward_by_id
from .store import Store
from .triage import Triage

HOUR = 3600.0
DAY = 86400.0


class Service:
    def __init__(self, store: Store, triage: Triage | None = None) -> None:
        self.store = store
        self.triage = triage or Triage()
        self.crews = crews()

    # --- intake ------------------------------------------------------------
    def report(self, text: str, lat: float, lng: float, reporter: str = "", phone: str = "",
               photo: str | None = None, at: float | None = None, use_ai: bool = True) -> dict:
        text = (text or "").strip()
        if len(text) < 5:
            raise ValueError("Please describe the problem in a few words.")
        if len(text) > 2000:
            text = text[:2000]
        now = at if at is not None else self.store.now()
        ward = nearest_ward(lat, lng)
        tid = self.store.next_id()

        proposal = self.triage.classify(text, photo) if use_ai else _offline(text)
        decision = kernel.decide_intake(proposal, text, lat, lng)

        ticket = {
            "id": tid, "created_at": now, "updated_at": now,
            "reporter": (reporter or "Citizen")[:60], "phone": _mask(phone),
            "text": text, "lang": proposal.get("language"),
            "lat": lat, "lng": lng, "ward_id": ward["id"], "zone": ward["zone"],
            "category": decision["category"], "severity": decision["severity"],
            "hazards": decision["hazards"],
            "summary_en": proposal.get("summary_en"), "summary_hi": proposal.get("summary_hi"),
            "ai": {k: proposal.get(k) for k in ("category", "severity", "hazards", "confidence",
                                                 "suspicious", "suspicion_reason", "source",
                                                 "location_hint", "used_photo")},
            "dept": decision["dept"], "status": decision["status"],
            "sla_due": now + decision["sla_hours"] * HOUR, "level": 1,
            "review_reason": decision["review_reason"],
            "photo": photo if photo and len(photo) < 1_500_000 else None,
        }

        parent = None
        if decision["status"] == "routed":
            open_t = self.store.tickets("status IN ('routed','assigned','in_progress','resolved') "
                                        "AND parent_id IS NULL AND category=?", (decision["category"],))
            parent = kernel.find_duplicate(lat, lng, decision["category"], now, open_t)
        if parent:
            ticket["status"] = "merged"
            ticket["parent_id"] = parent["id"]

        self.store.insert(ticket)
        self.store.log(tid, "citizen", "reported", {"text": text[:280], "ward": ward["name"]}, ts=now)
        self.store.log(tid, "ai", "proposed", ticket["ai"], ts=now)
        self.store.log(tid, "kernel", "decided", {
            "status": decision["status"], "category": decision["category"],
            "severity": decision["severity"], "dept": decision["dept"],
            "sla_hours": decision["sla_hours"], "rules": decision["rules"],
            "review_reason": decision["review_reason"]}, ts=now)

        if parent:
            supporters = (parent.get("supporters") or 0) + 1
            sev = kernel.supporter_bump(supporters, parent["severity"])
            upd = {"supporters": supporters}
            rules = [{"id": "K-DUP", "text": f"Same issue within {policy()['dedup']['radius_m']} m: "
                                             f"merged into {parent['id']} as supporter #{supporters}."}]
            if sev != parent["severity"]:
                upd["severity"] = sev
                new_sla = kernel.sla_hours(parent["category"], sev)
                upd["sla_due"] = min(parent["sla_due"], parent["created_at"] + new_sla * HOUR)
                rules.append({"id": "K-SUP", "text": f"{supporters} citizens affected: severity "
                                                     f"{parent['severity']} -> {sev}, SLA tightened."})
            self.store.update(parent["id"], **upd)
            self.store.log(tid, "kernel", "merged", {"into": parent["id"], "rules": rules}, ts=now)
            self.store.log(parent["id"], "kernel", "supporter_added",
                           {"from": tid, "supporters": supporters, "rules": rules}, ts=now)
        self._notify(tid, "merged" if parent else ("review" if decision["status"] == "needs_review" else "received"),
                     now, parent_id=parent["id"] if parent else None)
        if not parent and decision["status"] == "routed":
            self._auto_assign(tid, now)
        return self.view(tid)

    # --- citizen notifications (simulated SMS / WhatsApp outbox) ----------
    def _notify(self, tid: str, kind: str, now: float, **extra) -> None:
        t = self.store.get(tid)
        if not t:
            return
        pol = policy()
        cat = pol["categories"][t["category"]]
        hours = max(0, round((t["sla_due"] - now) / HOUR)) if t.get("sla_due") else 0
        msgs = {
            "received": (f"NagarSetu {tid}: your {cat['label'].lower()} complaint is registered and sent to "
                         f"{pol['departments'][t['dept']]}. Fix due within {hours} h.",
                         f"नगर सेतु {tid}: आपकी शिकायत ({cat['label_hi']}) दर्ज हो गई है। {hours} घंटे में समाधान होगा।"),
            "review": (f"NagarSetu {tid}: received. A ward officer will check it shortly.",
                       f"नगर सेतु {tid}: शिकायत मिली। वार्ड अधिकारी जल्द जांच करेंगे।"),
            "merged": (f"NagarSetu {tid}: this issue is already reported as {extra.get('parent_id')}. "
                       f"Your report adds priority to it.",
                       f"नगर सेतु {tid}: यह समस्या पहले से {extra.get('parent_id')} में दर्ज है। आपकी शिकायत से प्राथमिकता बढ़ी।"),
            "assigned": (f"NagarSetu {tid}: crew {t.get('crew_id')} is assigned.",
                         f"नगर सेतु {tid}: टीम {t.get('crew_id')} को काम सौंपा गया।"),
            "resolved": (f"NagarSetu {tid}: marked fixed. Reply YES if fixed or NO to reopen "
                         f"(auto-closes in {pol['closure']['auto_confirm_hours']} h).",
                         f"नगर सेतु {tid}: काम पूरा बताया गया। ठीक है तो YES, नहीं तो NO भेजें।"),
            "closed": (f"NagarSetu {tid}: closed. Thank you for helping Indore.",
                       f"नगर सेतु {tid}: शिकायत बंद। इंदौर की मदद के लिए धन्यवाद।"),
            "reopened": (f"NagarSetu {tid}: reopened and escalated to {kernel.level_role(t.get('level') or 1)}.",
                         f"नगर सेतु {tid}: शिकायत फिर से खोली गई और ऊपर भेजी गई।"),
            "rejected": (f"NagarSetu {tid}: not accepted. Reason: {t.get('review_reason')}",
                         f"नगर सेतु {tid}: शिकायत अस्वीकार। कारण: {t.get('review_reason')}"),
        }
        if kind not in msgs:
            return
        en, hi = msgs[kind]
        channel = "SMS" if t.get("phone") else "in-app"
        self.store.log(tid, "system", "notify", {"channel": channel, "to": t.get("phone") or "tracking page",
                                                 "en": en, "hi": hi, "kind": kind}, ts=now)

    def _crew_load(self) -> dict[str, int]:
        rows = self.store.tickets("status IN ('assigned','in_progress')")
        return Counter(r["crew_id"] for r in rows if r.get("crew_id"))

    def _auto_assign(self, tid: str, now: float) -> None:
        t = self.store.get(tid)
        crew = kernel.pick_crew(t["dept"], t["zone"], self.crews, self._crew_load())
        if crew:
            self.act(tid, "assign", "system", {"crew_id": crew["id"]}, at=now)

    # --- lifecycle ---------------------------------------------------------
    def act(self, tid: str, action: str, actor: str, payload: dict | None = None,
            at: float | None = None) -> dict:
        payload = payload or {}
        t = self.store.get(tid)
        if not t:
            raise KeyError(tid)
        now = at if at is not None else self.store.now()
        rules = kernel.check_transition(t, action, actor, payload, now)
        upd: dict = {}
        if action == "approve":
            cat = payload.get("category") or t["category"]
            sev = int(payload.get("severity") or t["severity"])
            if cat not in policy()["categories"]:
                raise kernel.Denied("K-CAT: unknown category.")
            sla = kernel.sla_hours(cat, sev)
            upd = {"status": "routed", "category": cat, "severity": sev,
                   "dept": policy()["categories"][cat]["dept"], "review_reason": None,
                   "created_at": t["created_at"], "sla_due": now + sla * HOUR}
        elif action == "reject":
            upd = {"status": "rejected", "review_reason": payload["reason"].strip()[:300], "closed_at": now}
        elif action == "assign":
            upd = {"status": "assigned", "crew_id": payload.get("crew_id")}
        elif action == "start":
            upd = {"status": "in_progress"}
        elif action == "resolve":
            upd = {"status": "resolved", "resolved_at": now,
                   "proof": {"lat": payload["lat"], "lng": payload["lng"],
                             "note": payload["note"].strip()[:300],
                             "photo": bool(payload.get("photo"))}}
        elif action in ("confirm", "auto_close"):
            upd = {"status": "closed", "closed_at": now}
            if payload.get("rating"):
                upd["rating"] = max(1, min(5, int(payload["rating"])))
        elif action == "reopen":
            sev = min(5, t["severity"] + policy()["closure"]["reopen_severity_bump"])
            sla = kernel.sla_hours(t["category"], sev)
            upd = {"status": "assigned", "severity": sev, "level": min(3, (t["level"] or 1) + 1),
                   "resolved_at": None, "proof": None, "sla_due": now + sla * HOUR}
        upd["updated_at"] = now
        self.store.update(tid, **upd)
        self.store.log(tid, actor, action, {**{k: v for k, v in payload.items() if k != "photo"},
                                            "rules": rules}, ts=now)
        if action == "approve":
            self._auto_assign(tid, now)
        note = {"assign": "assigned", "resolve": "resolved", "confirm": "closed", "auto_close": "closed",
                "reopen": "reopened", "reject": "rejected"}.get(action)
        if note:
            self._notify(tid, note, now)
        return self.view(tid)

    # --- open data ---------------------------------------------------------
    def export_csv(self) -> str:
        """Public open-data export. No names, phones, free text or photos."""
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "created_at_utc", "ward", "zone", "category", "severity", "department", "status",
                    "sla_hours", "resolved_at_utc", "resolved_within_sla", "supporters", "duplicate_of",
                    "escalation_level", "lat_3dp", "lng_3dp"])
        from datetime import datetime, timezone

        def iso(ts):
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""
        for t in self.store.tickets(order="created_at"):
            w.writerow([t["id"], iso(t["created_at"]), (ward_by_id(t["ward_id"]) or {}).get("name"), t["zone"],
                        t["category"], t["severity"], t["dept"], t["status"],
                        round((t["sla_due"] - t["created_at"]) / HOUR, 1) if t.get("sla_due") else "",
                        iso(t.get("resolved_at")),
                        "" if not t.get("resolved_at") else int(t["resolved_at"] <= t["sla_due"]),
                        t.get("supporters") or 0, t.get("parent_id") or "", t.get("level") or 1,
                        round(t["lat"], 3), round(t["lng"], 3)])
        return buf.getvalue()

    def sweep(self, now: float | None = None) -> dict:
        """Escalate open tickets by SLA ratio and auto-close resolved ones past the window."""
        now = now if now is not None else self.store.now()
        escalated, closed = [], []
        for t in self.store.tickets("status IN ('needs_review','routed','assigned','in_progress')"):
            st = kernel.sla_state(t, now)
            if st["level"] > (t["level"] or 1):
                self.store.update(t["id"], level=st["level"])
                role = kernel.level_role(st["level"])
                self.store.log(t["id"], "kernel", "escalated", {
                    "level": st["level"], "to": role,
                    "rules": [{"id": "K-ESC", "text": f"SLA {st['ratio']:.0%} used: escalated to {role}."}]},
                    ts=now)
                escalated.append(t["id"])
            if bool(st["at_risk"]) != bool(t["at_risk"]):
                self.store.update(t["id"], at_risk=int(st["at_risk"]))
        for t in self.store.tickets("status='resolved'"):
            try:
                self.act(t["id"], "auto_close", "system", {}, at=now)
                closed.append(t["id"])
            except kernel.Denied:
                pass
        return {"escalated": escalated, "auto_closed": closed}

    def tick(self, hours: float) -> dict:
        self.store.advance_clock(hours)
        return {"now": self.store.now(), **self.sweep()}

    # --- views -------------------------------------------------------------
    def view(self, tid: str, with_events: bool = True) -> dict:
        t = self.store.get(tid)
        if not t:
            raise KeyError(tid)
        return self._decorate(t, self.store.now(), with_events)

    def _decorate(self, t: dict, now: float, with_events: bool = False) -> dict:
        pol = policy()
        w = ward_by_id(t["ward_id"]) or {}
        st = kernel.sla_state(t, now)
        out = {**t, "ward": w.get("name"), "category_label": pol["categories"][t["category"]]["label"],
               "category_label_hi": pol["categories"][t["category"]]["label_hi"],
               "dept_name": pol["departments"].get(t["dept"], t["dept"]),
               "sla": st, "level_role": kernel.level_role(max(st["level"], t["level"] or 1)),
               "hours_left": round((t["sla_due"] - now) / HOUR, 1) if t.get("sla_due") else None,
               "age_hours": round((now - t["created_at"]) / HOUR, 1)}
        out["has_photo"] = bool(t.get("photo"))
        out.pop("photo", None)
        if with_events:
            out["events"] = self.store.events(t["id"])
            out["children"] = [c["id"] for c in self.store.tickets("parent_id=?", (t["id"],))]
        return out

    def list(self, status: str | None = None, ward: str | None = None, category: str | None = None,
             limit: int = 300) -> list[dict]:
        where, params = ["1=1"], []
        if status == "open":
            where.append("status IN ('needs_review','routed','assigned','in_progress')")
        elif status:
            where.append("status=?")
            params.append(status)
        if ward:
            where.append("ward_id=?")
            params.append(ward)
        if category:
            where.append("category=?")
            params.append(category)
        now = self.store.now()
        return [self._decorate(t, now) for t in
                self.store.tickets(" AND ".join(where), tuple(params), limit=limit)]

    def photo(self, tid: str) -> str | None:
        t = self.store.get(tid)
        return t.get("photo") if t else None

    # --- analytics ---------------------------------------------------------
    def stats(self) -> dict:
        pol, now = policy(), self.store.now()
        all_t = self.store.tickets()
        roots = [t for t in all_t if t["status"] != "merged"]
        open_t = [t for t in roots if t["status"] in kernel.OPEN]
        states = {t["id"]: kernel.sla_state(t, now) for t in open_t}
        done = [t for t in roots if t["status"] in ("resolved", "closed") and t.get("resolved_at")]
        on_time = [t for t in done if t["resolved_at"] <= t["sla_due"]]
        res_hours = [(t["resolved_at"] - t["created_at"]) / HOUR for t in done]
        ratings = [t["rating"] for t in roots if t.get("rating")]

        kpis = {
            "total_reports": len(all_t), "tickets": len(roots), "open": len(open_t),
            "merged_duplicates": len(all_t) - len(roots),
            "needs_review": sum(t["status"] == "needs_review" for t in roots),
            "breached": sum(s["breached"] for s in states.values()),
            "at_risk": sum(s["at_risk"] for s in states.values()),
            "resolved": len(done),
            "sla_compliance_pct": round(100 * len(on_time) / len(done), 1) if done else 0.0,
            "median_resolution_h": round(statistics.median(res_hours), 1) if res_hours else 0.0,
            "citizen_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
            "reopened": sum(1 for e in self._events_of_kind("reopen")),
        }

        depts = []
        for code, name in pol["departments"].items():
            mine = [t for t in open_t if t["dept"] == code]
            d_done = [t for t in done if t["dept"] == code]
            depts.append({"code": code, "name": name, "open": len(mine),
                          "breached": sum(states[t["id"]]["breached"] for t in mine),
                          "at_risk": sum(states[t["id"]]["at_risk"] for t in mine),
                          "sla_pct": round(100 * sum(t["resolved_at"] <= t["sla_due"] for t in d_done)
                                           / len(d_done), 1) if d_done else None})
        depts.sort(key=lambda d: (-d["breached"], -d["open"]))

        wards = []
        for w in city()["wards"]:
            mine = [t for t in roots if t["ward_id"] == w["id"]]
            w_open = [t for t in mine if t["status"] in kernel.OPEN]
            w_done = [t for t in mine if t["status"] in ("resolved", "closed") and t.get("resolved_at")]
            score = 100.0
            if mine:
                compliance = (sum(t["resolved_at"] <= t["sla_due"] for t in w_done) / len(w_done)) if w_done else 0.5
                breach_share = sum(states[t["id"]]["breached"] for t in w_open) / max(1, len(mine))
                score = round(max(0, min(100, 100 * compliance - 60 * breach_share)), 0)
            wards.append({**w, "open": len(w_open), "total": len(mine),
                          "breached": sum(states[t["id"]]["breached"] for t in w_open),
                          "score": score})

        days = []
        for i in range(13, -1, -1):
            start = now - (i + 1) * DAY
            end = now - i * DAY
            days.append({"offset": -i,
                         "reported": sum(start <= t["created_at"] < end for t in all_t),
                         "resolved": sum(1 for t in done if start <= t["resolved_at"] < end)})

        by_cat = Counter(t["category"] for t in open_t)
        return {
            "now": now, "kpis": kpis, "departments": depts, "wards": wards, "trend": days,
            "open_by_category": [{"category": c, "label": pol["categories"][c]["label"], "count": n}
                                 for c, n in by_cat.most_common()],
            "hotspots": self.hotspots(all_t, now),
            "predicted_breaches": self.predicted_breaches(open_t, now)[:12],
            "mode": self.triage.mode,
        }

    def _events_of_kind(self, kind: str) -> list[dict]:
        with self.store.lock:
            rows = self.store.db.execute("SELECT id FROM events WHERE kind=?", (kind,)).fetchall()
        return list(rows)

    def hotspots(self, all_t: list[dict], now: float) -> list[dict]:
        pol = policy()
        cur: dict = defaultdict(int)
        prev: dict = defaultdict(int)
        for t in all_t:
            if t["status"] == "rejected":
                continue
            age = now - t["created_at"]
            key = (t["ward_id"], t["category"])
            if age <= 7 * DAY:
                cur[key] += 1
            elif age <= 14 * DAY:
                prev[key] += 1
        out = []
        for (wid, cat), n in cur.items():
            if n < 3:
                continue
            p = prev.get((wid, cat), 0)
            growth = (n - p) / max(1, p)
            w = ward_by_id(wid)
            out.append({"ward_id": wid, "ward": w["name"], "zone": w["zone"], "lat": w["lat"],
                        "lng": w["lng"], "category": cat,
                        "category_label": pol["categories"][cat]["label"],
                        "last_7d": n, "prev_7d": p, "growth": round(growth, 2),
                        "growth_label": f"up {round(growth * 100)}% week on week" if p and growth > 0
                        else ("new this week" if not p else f"down {abs(round(growth * 100))}%"),
                        "score": round(n * (1 + max(0, growth)), 2)})
        out.sort(key=lambda h: -h["score"])
        return out[:8]

    def predicted_breaches(self, open_t: list[dict], now: float) -> list[dict]:
        """Queue-aware forecast: when will each crew reach each ticket?"""
        handle = city()["avg_handle_hours"]
        queues: dict = defaultdict(list)
        for t in open_t:
            queues[t.get("crew_id") or f"UNASSIGNED-{t['dept']}"].append(t)
        out = []
        for crew, ts in queues.items():
            ts.sort(key=lambda t: (-t["severity"], t["sla_due"]))
            eta = now
            for t in ts:
                eta += handle.get(t["dept"], 6) * HOUR
                if t["status"] == "needs_review":
                    eta += 4 * HOUR
                if eta > t["sla_due"]:
                    out.append({"id": t["id"], "ward": (ward_by_id(t["ward_id"]) or {}).get("name"),
                                "category": t["category"], "severity": t["severity"],
                                "crew": t.get("crew_id"), "dept": t["dept"],
                                "late_by_h": round((eta - t["sla_due"]) / HOUR, 1),
                                "already_breached": now > t["sla_due"]})
        out.sort(key=lambda r: (r["already_breached"], -r["late_by_h"]))
        return [r for r in out if not r["already_breached"]] + [r for r in out if r["already_breached"]]

    def brief(self) -> dict:
        s = self.stats()
        payload = {"kpis": s["kpis"], "hotspots": s["hotspots"][:5],
                   "departments": [d for d in s["departments"] if d["open"]][:6],
                   "predicted_breaches": len(s["predicted_breaches"])}
        text, source = self.triage.brief(payload)
        return {"text": text, "source": source, "generated_at": s["now"]}


def _mask(phone: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return f"******{digits[-4:]}" if len(digits) >= 4 else ""


def _offline(text: str) -> dict:
    from .triage import rules_classify
    out = rules_classify(text)
    out["used_photo"] = False
    return out
