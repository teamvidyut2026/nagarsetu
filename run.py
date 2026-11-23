"""Start NagarSetu: python run.py [--lan] [--port 8000] [--reseed]"""
from __future__ import annotations

import argparse
import os

import uvicorn

from nagarsetu.config import load_dotenv


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--lan", action="store_true", help="serve on 0.0.0.0 so phones on the LAN can report")
    p.add_argument("--port", type=int, default=int(os.environ.get("NAGARSETU_PORT", "8000")))
    p.add_argument("--reseed", action="store_true", help="wipe and regenerate the demo data")
    args = p.parse_args()

    from nagarsetu.server import create_app
    app = create_app()
    if args.reseed:
        from nagarsetu.seed import seed
        seed(app.state.svc)
    host = "0.0.0.0" if args.lan else os.environ.get("NAGARSETU_HOST", "127.0.0.1")
    print(f"NagarSetu on http://{'127.0.0.1' if host == '0.0.0.0' else host}:{args.port}  "
          f"(AI: {app.state.svc.triage.mode})")
    uvicorn.run(app, host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
