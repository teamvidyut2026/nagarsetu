"""FastAPI app: citizen intake, command-center APIs, demo controls, static dashboard."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import kernel
from .config import ROOT, city, load_dotenv, policy
from .seed import seed
from .service import Service
from .store import Store

STATIC = Path(__file__).parent / "static"


class ReportIn(BaseModel):
    text: str = Field(min_length=5, max_length=2000)
    lat: float
    lng: float
    reporter: str = ""
    phone: str = ""
    photo: str | None = None


class ActionIn(BaseModel):
    action: str
    actor: str
    payload: dict = {}


class PreviewIn(BaseModel):
    text: str = Field(min_length=5, max_length=2000)
    lat: float = 22.7196
    lng: float = 75.8577
    photo: str | None = None


class TickIn(BaseModel):
    hours: float = Field(gt=0, le=240)


API_ACTORS = {"officer", "crew", "citizen", "ai"}  # 'system' is internal only


def create_app(db_path: str | None = None, auto_seed: bool = True) -> FastAPI:
    load_dotenv()
    store = Store(db_path or os.environ.get("NAGARSETU_DB", str(ROOT / "data" / "nagarsetu.db")))
    svc = Service(store)
    if auto_seed and store.count() == 0:
        seed(svc)

    app = FastAPI(title="NagarSetu", version="1.0",
                  description="AI civic-operations agent for Indore: the AI proposes, the kernel decides.")
    app.state.svc = svc

    @app.exception_handler(kernel.Denied)
    async def denied(_, exc: kernel.Denied):
        return JSONResponse(status_code=409, content={"denied": True, "detail": str(exc)})

    @app.get("/api/meta")
    def meta():
        pol = policy()
        return {"mode": svc.triage.mode, "now": store.now(),
                "categories": pol["categories"], "departments": pol["departments"],
                "hazards": pol["hazards"], "wards": city()["wards"], "policy": pol,
                "crews": svc.crews}

    @app.post("/api/reports")
    def create_report(r: ReportIn):
        try:
            return svc.report(r.text, r.lat, r.lng, r.reporter, r.phone, r.photo)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/triage/preview")
    def preview(r: PreviewIn):
        """What would happen to this complaint? Nothing is saved."""
        proposal = svc.triage.classify(r.text, r.photo)
        decision = kernel.decide_intake(proposal, r.text, r.lat, r.lng)
        return {"proposal": proposal, "decision": decision}

    @app.get("/api/tickets")
    def tickets(status: str | None = None, ward: str | None = None, category: str | None = None,
                limit: int = 300):
        return svc.list(status, ward, category, min(limit, 1000))

    @app.get("/api/tickets/{tid}")
    def ticket(tid: str):
        try:
            return svc.view(tid)
        except KeyError:
            raise HTTPException(404, "Ticket not found")

    @app.get("/api/tickets/{tid}/photo")
    def ticket_photo(tid: str):
        return {"photo": svc.photo(tid)}

    @app.post("/api/tickets/{tid}/actions")
    def act(tid: str, a: ActionIn):
        if a.actor not in API_ACTORS:
            raise HTTPException(400, "Unknown actor")
        try:
            return svc.act(tid, a.action, a.actor, a.payload)
        except KeyError:
            raise HTTPException(404, "Ticket not found")

    @app.get("/api/stats")
    def stats():
        return svc.stats()

    @app.get("/api/export.csv")
    def export_csv():
        return PlainTextResponse(svc.export_csv(), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="nagarsetu_open_data.csv"'})

    @app.get("/api/brief")
    def brief():
        return svc.brief()

    @app.get("/api/events")
    def events(limit: int = 40):
        return store.events(limit=min(limit, 200))

    @app.post("/api/demo/tick")
    def tick(t: TickIn):
        return svc.tick(t.hours)

    @app.post("/api/demo/seed")
    def reseed():
        return seed(svc)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
