"""Pack Proving Ground recordings for the hosted replay build.

Reads data/proving-ground/tapes/*.json and scorecard.json, and writes
ui/public/replay/ (index.json + one file per scenario) and
ui/public/proving-ground/scorecard.json. Floats are rounded to keep files small;
nothing else changes: the replay shows exactly what was recorded.

    python scripts/pack_replay.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "proving-ground"
OUT = ROOT / "ui" / "public"
ORDER = ["slow-inventory", "bad-deploy", "flash-sale", "payment-leak", "payment-runaway"]


def tidy(v):
    if isinstance(v, float):
        return round(v, 3) if abs(v) >= 1e6 else round(v, 4)
    if isinstance(v, list):
        return [tidy(x) for x in v]
    if isinstance(v, dict):
        return {k: tidy(x) for k, x in v.items()}
    return v


def main() -> None:
    tapes = {p.stem: json.loads(p.read_text()) for p in sorted((SRC / "tapes").glob("*.json"))}
    if not tapes:
        raise SystemExit(f"no recordings in {SRC / 'tapes'}: run the Proving Ground first")
    (OUT / "replay").mkdir(parents=True, exist_ok=True)
    chapters = []
    for name in sorted(tapes, key=lambda n: ORDER.index(n) if n in ORDER else 99):
        t = tapes[name]
        out = {k: t[k] for k in ("version", "scenario", "label", "inject_ts", "end_ts", "snapshot", "messages")}
        (OUT / "replay" / f"{name}.json").write_text(json.dumps(tidy(out), separators=(",", ":")))
        r = t.get("row") or {}
        chapters.append({"scenario": name, "label": t["label"], "file": f"{name}.json",
                         "result": {k: r.get(k) for k in ("pass", "mttd_s", "mttr_s", "actions", "root")}})
    recorded = min(t["snapshot"]["taken_at"] for t in tapes.values())
    (OUT / "replay" / "index.json").write_text(json.dumps({"recorded_at": recorded, "chapters": chapters}, indent=1))
    card = SRC / "scorecard.json"
    if card.exists():
        (OUT / "proving-ground").mkdir(parents=True, exist_ok=True)
        (OUT / "proving-ground" / "scorecard.json").write_text(card.read_text())
    size = sum(p.stat().st_size for p in (OUT / "replay").glob("*.json")) / 1e6
    print(f"packed {len(chapters)} recordings ({size:.1f} MB): {', '.join(c['scenario'] for c in chapters)}")


if __name__ == "__main__":
    main()
