"""Set up VIGIL for YOUR site: answers a few questions, writes deploy/vigil.yaml,
deploy/prometheus.yml and deploy/.env, and prints the labels to put on your containers.

    python scripts/vigil_init.py                    # asks, with sensible defaults
    python scripts/vigil_init.py --answers a.json   # no questions (CI, or to redo it)
    python scripts/vigil_init.py --force            # overwrite existing files

Needs only Python 3.9+. What it writes is checked with the engine's own vigil.yaml
validator before anything is saved, so a typo never reaches a running VIGIL.
The admin token goes into deploy/.env and is never printed.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
sys.path.insert(0, str(ROOT / "engine"))

ACTIONS = ("restart", "rollback", "scale_out")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def ask(prompt: str, default: str | None = None) -> str:
    shown = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{shown}: ").strip()
        if value or default is not None:
            return value or (default or "")
        print("  (needed)")


def ask_list(prompt: str, default: str = "") -> list[str]:
    raw = ask(prompt + " (comma separated, blank for none)", default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def interview() -> dict:
    print("VIGIL setup. Press Enter to keep the [default].\n")
    a: dict = {"site": ask("Site name", "My site")}
    print("\nWhere do customers arrive? (the service they talk to first, e.g. your web app)")
    a["entry"] = ask("  Its name", "web")
    a["entry_address"] = ask("  Its address on the Docker network (host:port)", f"{a['entry']}:8000")
    a["entry_about"] = ask("  One line about it (optional)", "")
    print("\nWhat do customers come to do? e.g. 'result lookup', 'payment', 'checkout'")
    a["unit"] = ask("  One of them", "request")
    a["units"] = ask("  Many of them", a["unit"] + "s")
    a["metric"] = ask("  Counter your app exports for it (vigil-instrument: vigil.customer(...))",
                      f"{slug(a['unit'])}_requests_total")
    a["duration_metric"] = ask("  Its latency histogram (blank for none)", f"{slug(a['unit'])}_duration_seconds")
    a["ok"] = ask_list("  Outcomes that mean it worked", "ok")
    a["failed"] = ask_list("  Outcomes that mean the customer was let down", "failed")
    a["entry_calls"] = ask_list(f"  Services {a['entry']} calls")
    a["services"] = []
    queue = list(a["entry_calls"])
    seen: set[str] = set()
    print("\nNow each service VIGIL should watch.")
    while True:
        name = queue.pop(0) if queue else ask("  Another service to watch? (name, blank when done)", "")
        if not name:
            break
        if name in seen:
            continue
        seen.add(name)
        print(f"\n  {name}")
        svc = {"name": name,
               "address": ask("    Address on the Docker network (host:port)", f"{name}:8000"),
               "about": ask("    One line about it (optional)", ""),
               "calls": ask_list("    Services it calls"),
               "allow": ask_list(f"    Fixes VIGIL may use: {', '.join(ACTIONS)}", "restart")}
        a["services"].append(svc)
        queue += [c for c in svc["calls"] if c not in seen and c not in queue]
    a["network"] = ask("\nYour app's Docker network (docker network ls)", "myapp_default")
    a["autonomy"] = ask("How much may VIGIL do alone? L0 watch, L1 propose and you click, L2 act on safe fixes, L3 act on all",
                        "L1")
    return a


# ---- writing

def q(s: str) -> str:
    """A YAML scalar, quoted when plain text could be misread."""
    return s if re.fullmatch(r"[A-Za-z0-9_.:/ -]+", s) and not re.fullmatch(r"(?i)yes|no|on|off|true|false|null|~|[\d.]+", s) \
        and not s.startswith(("-", " ")) and ": " not in s else json.dumps(s)


def flow(items: list[str]) -> str:
    return "[" + ", ".join(q(i) for i in items) + "]"


def site_doc(a: dict) -> dict:
    """The vigil.yaml content as data: what the engine's validator checks."""
    customers = {"entry": a["entry"], "calls": a["entry_calls"], "unit": a["unit"], "units": a["units"],
                 "metric": a["metric"], "ok": a["ok"], "failed": a["failed"]}
    if a.get("entry_about"):
        customers["about"] = a["entry_about"]
    if a.get("duration_metric"):
        customers["duration_metric"] = a["duration_metric"]
    services = {}
    for s in a["services"]:
        spec = {"calls": s["calls"], "allow": s["allow"]}
        if s.get("about"):
            spec["about"] = s["about"]
        services[s["name"]] = spec
    return {"version": 1, "site": {"name": a["site"]}, "customers": customers, "services": services,
            "autonomy": a["autonomy"]}


def site_yaml(a: dict) -> str:
    c = site_doc(a)["customers"]
    lines = [
        "# vigil.yaml for your site, written by scripts/vigil_init.py. Edit freely:",
        "# VIGIL checks it at start and names the exact key if something is wrong.",
        "version: 1",
        "",
        "site:",
        f"  name: {q(a['site'])}",
        "",
        "customers:",
        f"  entry: {q(c['entry'])}                 # where customers arrive: measured, never touched",
    ]
    if "about" in c:
        lines.append(f"  about: {q(c['about'])}")
    lines += [
        f"  calls: {flow(c['calls'])}",
        f"  unit: {q(c['unit'])}",
        f"  units: {q(c['units'])}",
        f"  metric: {c['metric']}",
        f"  ok: {flow(c['ok'])}",
        f"  failed: {flow(c['failed'])}              # any other outcome counts as neither",
    ]
    if "duration_metric" in c:
        lines.append(f"  duration_metric: {c['duration_metric']}")
    lines += ["", "# allow: restart, rollback, scale_out. [] = watch and diagnose only, a human fixes.", "services:"]
    for s in a["services"]:
        lines.append(f"  {s['name']}:")
        if s.get("about"):
            lines.append(f"    about: {q(s['about'])}")
        lines.append(f"    calls: {flow(s['calls'])}")
        lines.append(f"    allow: {flow(s['allow'])}")
    lines += ["", "# L0 watch · L1 propose, a human clicks · L2 act alone on low-risk fixes when sure · L3 act on every allowed fix",
              f"autonomy: {a['autonomy']}", ""]
    return "\n".join(lines)


def prometheus_yaml(a: dict) -> str:
    lines = [
        "# Written by scripts/vigil_init.py. One job per service: the `service` label is",
        "# added here, per target, so VIGIL groups by service however many copies run.",
        "# VIGIL thinks every 5 s, so scrape every 5 s.",
        "global:",
        "  scrape_interval: 5s",
        "  scrape_timeout: 4s",
        "  evaluation_interval: 5s",
        "",
        "scrape_configs:",
    ]
    for name, address in [(a["entry"], a["entry_address"]), *[(s["name"], s["address"]) for s in a["services"]]]:
        lines += [
            f"  - job_name: {q(name)}",
            "    static_configs:",
            f"      - targets: [{json.dumps(address)}]",
            f"        labels: {{ service: {q(name)} }}",
        ]
    return "\n".join(lines) + "\n"


def env_file(a: dict) -> str:
    return "\n".join([
        "# VIGIL beside your app. Keep this file private: it holds the operator token.",
        f"APP_NETWORK={a['network']}",
        f"VIGIL_ADMIN_TOKEN={secrets.token_urlsafe(24)}",
        "# Optional: an LLM second opinion on every diagnosis (https://console.groq.com).",
        "GROQ_API_KEY=",
        "# Optional: the console's port on this machine.",
        "VIGIL_PORT=3000",
        "",
    ])


def labels_help(a: dict) -> str:
    out = ["Add these labels to each watched service in YOUR docker-compose.yml, then recreate it",
           "(docker compose up -d). VIGIL acts only on containers labelled like this:", ""]
    for s in a["services"]:
        out += [f"  {s['name']}:", "    labels:", '      vigil.managed: "true"', f"      vigil.service: {s['name']}",
                "      vigil.version: v1            # keep equal to the app's APP_VERSION", ""]
    if any("scale_out" in s["allow"] for s in a["services"]):
        out += ["For scale_out: create a stopped spare of that service labelled vigil.role: replica,",
                "on the same network alias, scraped by Prometheus. VIGIL starts it under load.", ""]
    if any("rollback" in s["allow"] for s in a["services"]):
        out += ["For rollback: when you deploy a new version, keep the old container (stopped) with its",
                "vigil.version label. VIGIL starts it and stops the bad one.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--answers", help="a JSON file with the answers (no questions asked)")
    ap.add_argument("--out", default=str(DEPLOY), help="where to write (default: deploy/)")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    args = ap.parse_args(argv)

    a = json.loads(Path(args.answers).read_text(encoding="utf-8")) if args.answers else interview()
    a.setdefault("entry_address", f"{a['entry']}:8000")
    for s in a["services"]:
        s.setdefault("address", f"{s['name']}:8000")
        s.setdefault("calls", [])
        s.setdefault("allow", ["restart"])

    # The engine's own validator: the same rules VIGIL applies when it starts.
    from vigil.config import ConfigError, parse_site
    try:
        parse_site(site_doc(a))
    except ConfigError as exc:
        print(f"\nNot written: {exc}", file=sys.stderr)
        return 2
    if a["autonomy"] not in ("L0", "L1", "L2", "L3"):
        print("\nNot written: autonomy must be L0, L1, L2 or L3", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    files = {"vigil.yaml": site_yaml(a), "prometheus.yml": prometheus_yaml(a)}
    existing = [n for n in files if (out / n).exists()]
    if existing and not args.force:
        print(f"\nNot written: {', '.join(existing)} already in {out}. Use --force to overwrite.", file=sys.stderr)
        return 1
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8", newline="\n")
    env = out / ".env"
    if env.exists():
        print(f"Kept your existing {env} (token unchanged).")
    else:
        env.write_text(env_file(a), encoding="utf-8", newline="\n")
        print(f"Wrote {env} with a new operator token (open the file to see it).")
    print(f"Wrote {out / 'vigil.yaml'} and {out / 'prometheus.yml'}.\n")
    print(labels_help(a))
    print(f"Then: cd {out} && docker compose up --build -d, and open http://localhost:3000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
