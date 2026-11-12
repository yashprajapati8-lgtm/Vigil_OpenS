"""Email alerts: how VIGIL reaches a person.

VIGIL runs on a server nobody sits at. The console is where you act; the email
is how you know to open it. So VIGIL emails only when a person must act, or must
know what VIGIL did on its own:

    acting               VIGIL is fixing something on its own (what it found, what it does)
    awaiting_approval    a fix is waiting for your click while customers fail
    escalated            VIGIL cannot or may not fix it: what it tried, what you should do
    resolved             fixed and verified: how long, how many customers were hurt
    telemetry_degraded   VIGIL cannot see your services, so silence means nothing
    baseline_drift       a service's normal keeps rising: accept it or look into it

Each one goes at most once per incident (or per service, for drift), at most
alert_max_per_hour in total. A spike VIGIL is still confirming, a surge it
handles, every small event: no email, or people learn to ignore them.

Off until VIGIL has an account to send from (VIGIL_SMTP_* in .env) and someone
to send to (VIGIL_ALERT_TO in .env, or added in the console by an operator). Emails are built and sent
on one background thread with a timeout: a slow mail server never slows VIGIL.
The SMTP password is never logged.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import queue
import re
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import Settings
from .incidents import fmt_duration
from .store import Store

log = logging.getLogger("vigil.notify")

EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']{2,}$")
MAX_RECIPIENTS = 10


class RecipientError(ValueError):
    """An address the console can't add or remove; the message says why, in plain words."""


def recipient_id(address: str) -> str:
    """A short, stable id the console uses to remove an address it only shows masked."""
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()[:10]

ACTION_TEXT = {"restart": "restart {t}", "rollback": "roll back {t}", "scale_out": "add a {t} replica", "wait": "wait"}


def mask(address: str) -> str:
    """y***@gmail.com: enough to recognise, not enough to harvest."""
    name, _, domain = address.partition("@")
    return f"{name[:1]}***@{domain}" if domain else "***"


def action_text(decision: dict | None) -> str:
    if not decision or not decision.get("action"):
        return "decide what to do"
    return ACTION_TEXT.get(decision["action"], decision["action"] + " {t}").format(t=decision.get("target") or "")


def chosen_saves(decision: dict) -> float | None:
    """How many customer actions the chosen fix saves: it lives on the chosen option."""
    for o in decision.get("options") or []:
        if o.get("action") == decision.get("action") and o.get("target") == decision.get("target"):
            return o.get("saves")
    return None


class Email:
    """One email, plain text and HTML."""

    def __init__(self, subject: str, headline: str, facts: list[tuple[str, str]],
                 steps: list[tuple[str, str]], link: str | None, footer: str):
        self.subject = subject
        self.headline = headline
        self.facts = facts
        self.steps = steps
        self.link = link
        self.footer = footer

    def text(self) -> str:
        lines = [self.headline, ""]
        lines += [f"{k}: {v}" for k, v in self.facts]
        if self.steps:
            lines += ["", "What VIGIL did, step by step:"]
            lines += [f"  {t:>7}  {s}" for t, s in self.steps]
        if self.link:
            lines += ["", f"Open it in the console: {self.link}"]
        lines += ["", self.footer]
        return "\n".join(lines)

    def html(self) -> str:
        e = html.escape
        facts = "".join(
            f'<tr><td style="padding:3px 14px 3px 0;color:#6b625c;white-space:nowrap;vertical-align:top">{e(k)}</td>'
            f'<td style="padding:3px 0;color:#1d1916">{e(v)}</td></tr>' for k, v in self.facts)
        steps = "".join(
            f'<tr><td style="padding:5px 12px 5px 0;color:#0f8a7e;font-variant-numeric:tabular-nums;white-space:nowrap;'
            f'vertical-align:top">{e(t)}</td><td style="padding:5px 0;color:#2b2522;line-height:1.45">{e(s)}</td></tr>'
            for t, s in self.steps)
        button = (f'<p style="margin:22px 0 6px"><a href="{e(self.link)}" style="display:inline-block;padding:11px 18px;'
                  f'border-radius:10px;background:#0f8a7e;color:#ffffff;text-decoration:none;font-weight:600">'
                  f'Open in the VIGIL console</a></p>') if self.link else ""
        return (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
            '<body style="margin:0;background:#f4f1ee;-webkit-text-size-adjust:100%;text-size-adjust:100%">'
            '<div style="max-width:560px;margin:0 auto;padding:22px 18px;font:15px/1.5 -apple-system,Segoe UI,Roboto,'
            'Helvetica,Arial,sans-serif;color:#1d1916">'
            '<p style="margin:0 0 4px;font-size:13px;letter-spacing:.04em;color:#0f8a7e;font-weight:700">VIGIL</p>'
            f'<h1 style="margin:0 0 14px;font-size:20px;line-height:1.3">{e(self.headline)}</h1>'
            f'<table style="border-collapse:collapse;font-size:14px">{facts}</table>'
            + (f'<h2 style="margin:22px 0 6px;font-size:15px">What VIGIL did, step by step</h2>'
               f'<table style="border-collapse:collapse;font-size:14px">{steps}</table>' if steps else "")
            + button
            + f'<p style="margin:18px 0 0;font-size:12.5px;color:#6b625c">{e(self.footer)}</p>'
            '</div></body></html>')


class Notifier:
    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        # Who receives alerts: VIGIL_ALERT_TO from .env, plus addresses added in the
        # console (operator only), saved in the data folder so they survive a restart.
        self._path = settings.data_dir / "alert_recipients.json"
        self._saved: list[str] = self._load()
        self.sent = 0
        self.last_error: str | None = None
        self.last_sent_at: float | None = None
        self._seen: set[tuple[str, str]] = set()
        self._recent: list[float] = []
        self._q: queue.Queue = queue.Queue(maxsize=100)
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- who receives alerts

    def _load(self) -> list[str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable %s: %s", self._path.name, exc)
            return []
        out = []
        for a in data.get("recipients", []) if isinstance(data, dict) else []:
            if isinstance(a, str) and EMAIL_RE.match(a):
                out.append(a)
        return out

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"recipients": self._saved}, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)        # all or nothing: a crash never leaves half a file

    @property
    def recipients(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for a in [*self.s.alert_to, *self._saved]:
            if a.lower() not in seen:
                seen.add(a.lower())
                out.append(a)
        return out

    @property
    def sender_ready(self) -> bool:
        return bool(self.s.smtp_host)

    @property
    def enabled(self) -> bool:
        return self.sender_ready and bool(self.recipients)

    def add_recipient(self, address: str) -> None:
        address = (address or "").strip()
        if len(address) > 254 or not EMAIL_RE.match(address):
            raise RecipientError("that doesn't look like an email address")
        with self._lock:
            if address.lower() in {a.lower() for a in self.recipients}:
                raise RecipientError("that address already gets alerts")
            if len(self.recipients) >= MAX_RECIPIENTS:
                raise RecipientError(f"at most {MAX_RECIPIENTS} addresses")
            self._saved.append(address)
            self._save()
        log.info("alert recipient added: %s", mask(address))

    def remove_recipient(self, rid: str) -> None:
        with self._lock:
            for a in self.s.alert_to:
                if recipient_id(a) == rid:
                    raise RecipientError("that address is set in .env (VIGIL_ALERT_TO): remove it there")
            for a in self._saved:
                if recipient_id(a) == rid:
                    self._saved.remove(a)
                    self._save()
                    log.info("alert recipient removed: %s", mask(a))
                    return
        raise RecipientError("no such address")

    # ---- what the engine sees

    def status(self) -> dict:
        env = {a.lower() for a in self.s.alert_to}
        view = [{"id": recipient_id(a), "masked": mask(a), "source": ".env" if a.lower() in env else "console"}
                for a in self.recipients]
        base = {"sender": self.sender_ready, "recipients": view}
        if not self.sender_ready:
            return {**base, "email": "off",
                    "why": "VIGIL has no account to send from: set VIGIL_SMTP_HOST, VIGIL_SMTP_USER and "
                           "VIGIL_SMTP_PASSWORD in .env"}
        if not view:
            return {**base, "email": "off", "why": "add an email address to send alerts to"}
        return {**base, "email": "on", "from": mask(self.s.alert_from or self.s.smtp_user),
                "to": [r["masked"] for r in view], "on": list(self.s.alert_on),
                "sent": self.sent, "last_sent_at": self.last_sent_at, "last_error": self.last_error}

    def on_message(self, message: dict) -> None:
        """Bus watcher: runs on the publisher's thread, so it only decides and queues."""
        if not self.enabled or message.get("type") != "event":
            return
        ev = message.get("event") or {}
        kind = ev.get("kind")
        if kind not in self.s.alert_on:
            return
        scope = ev.get("incident_id") or (ev.get("payload") or {}).get("service") or "system"
        if kind == "baseline_drift":
            scope = f"{scope}:{','.join((ev.get('payload') or {}).get('signals', []))}"
        key = (str(scope), kind)
        with self._lock:
            if key in self._seen:
                return
            self._seen.add(key)
            now = time.time()
            self._recent = [t for t in self._recent if now - t < 3600]
            if len(self._recent) >= self.s.alert_max_per_hour:
                log.warning("email alert skipped (%d in the last hour): %s", len(self._recent), kind)
                return
            self._recent.append(now)
        try:
            self._q.put_nowait(ev)
        except queue.Full:
            log.warning("email alert queue full; dropped %s", kind)
            return
        self._ensure_worker()

    def send_test(self) -> None:
        """Send one email now (POST /api/alerts/test). Raises on failure, with the reason."""
        if not self.sender_ready:
            raise RuntimeError("email alerts are off: set VIGIL_SMTP_HOST and VIGIL_ALERT_TO in .env")
        if not self.recipients:
            raise RuntimeError("no one to send to: add an email address first")
        mail = Email(
            subject=f"[VIGIL] {self.s.site_name}: test email",
            headline="Email alerts work.",
            facts=[("Site", self.s.site_name), ("Watching", ", ".join(self.s.monitored)),
                   ("You'll hear when", "VIGIL fixes something on its own, needs your approval, escalates, "
                                        "can't see your services, or a service's normal keeps rising")],
            steps=[], link=self.s.console_url, footer=self._footer())
        self._send(mail)

    # ---- building

    def _footer(self) -> str:
        return (f"Sent by VIGIL watching {self.s.site_name}. You get at most one email per incident for each of: "
                "acting, approval, escalation, fixed.")

    def _steps(self, inc_id: str) -> list[tuple[str, str]]:
        events = self.store.events(incident_id=inc_id, limit=200)
        if not events:
            return []
        t0 = events[0]["ts"]
        return [(f"+{fmt_duration(e['ts'] - t0)}", e["summary"]) for e in events]

    def build(self, ev: dict) -> Email | None:
        kind = ev["kind"]
        units = self.s.units
        site = self.s.site_name
        inc_id = ev.get("incident_id")
        inc = self.store.get_incident(inc_id) if inc_id else None
        data = (inc or {}).get("data", {})
        diag = data.get("diagnosis") or {}
        decision = data.get("decision") or {}
        link = f"{self.s.console_url.rstrip('/')}/incidents/{inc_id}" if inc_id else self.s.console_url
        facts: list[tuple[str, str]] = []
        if inc:
            facts.append(("Incident", f"{inc_id} ({inc.get('severity', '?')}): {inc.get('title', '')}".strip()))
        if diag.get("root"):
            facts.append(("Root cause", f"{diag['root']} ({diag.get('category', '?').replace('_', ' ')}), "
                                        f"confidence {diag.get('confidence', '?')}"))
        if decision.get("action"):
            saves = chosen_saves(decision)
            facts.append(("Fix", action_text(decision) + (f": saves ~{int(saves):,} {units} in the next "
                                                          f"{decision.get('horizon_min', 5):.0f} min" if saves else "")))
        steps = self._steps(inc_id) if inc_id else []

        if kind == "acting":
            subject = f"[VIGIL] {site}: fixing {inc_id} on its own ({action_text(decision)})"
            headline = f"VIGIL found the cause and is fixing it: {action_text(decision)}."
            facts.append(("You need to", "nothing yet. VIGIL will email again when it's verified, or if it needs you."))
        elif kind == "awaiting_approval":
            subject = f"[VIGIL] {site}: approval needed for {inc_id} ({action_text(decision)})"
            headline = f"VIGIL wants to {action_text(decision)}, and is waiting for you."
            facts.append(("You need to", "open the console, unlock operator mode, approve or reject."))
        elif kind == "escalated":
            subject = f"[VIGIL] {site}: {inc_id} needs you"
            headline = "VIGIL stopped and handed this to a human."
            facts.append(("Why", ev.get("summary", "")))
            facts.append(("You need to", "open the console: the decision shows every option VIGIL weighed."))
        elif kind == "resolved":
            metrics = data.get("metrics") or {}
            mttr = metrics.get("mttr_s")
            lost = metrics.get("failed_checkouts", data.get("failed_checkouts"))
            subject = f"[VIGIL] {site}: fixed {inc_id}" + (f" in {fmt_duration(mttr)}" if mttr else "")
            headline = f"Fixed and verified{f' in {fmt_duration(mttr)}' if mttr else ''}."
            if lost is not None:
                facts.append(("Customers affected", f"{int(round(lost)):,} {units} failed before recovery"))
            facts.append(("You need to", "nothing. The incident page has the full record and a postmortem."))
        elif kind == "telemetry_degraded":
            subject = f"[VIGIL] {site}: VIGIL can't see your services"
            headline = "VIGIL can't read your metrics right now, so it can't watch or fix anything."
            facts = [("Problem", ev.get("summary", "")),
                     ("You need to", "check that Prometheus is running and can reach your services.")]
            steps, link = [], self.s.console_url
        elif kind == "baseline_drift":
            svc = (ev.get("payload") or {}).get("service", "a service")
            subject = f"[VIGIL] {site}: normal keeps rising on {svc}"
            headline = f"{svc} is slowly getting worse, too slowly to count as an outage."
            facts = [("What VIGIL sees", ev.get("summary", "")),
                     ("You need to", "if it's expected (a new, heavier version), accept the new normal in the "
                                     "console; if not, look into it.")]
            steps, link = [], self.s.console_url
        else:
            return None
        return Email(subject, headline, facts, steps, link, self._footer())

    # ---- sending

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name="vigil-email", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        while True:
            ev = self._q.get()
            try:
                mail = self.build(ev)
                if mail is not None:
                    self._send(mail)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("email alert failed (%s): %s", ev.get("kind"), self.last_error)
            finally:
                self._q.task_done()

    def _send(self, mail: Email) -> None:
        msg = EmailMessage()
        msg["Subject"] = mail.subject
        msg["From"] = self.s.alert_from or self.s.smtp_user
        msg["To"] = ", ".join(self.recipients)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="vigil.local")
        msg.set_content(mail.text())
        msg.add_alternative(mail.html(), subtype="html")
        security = self.s.smtp_security.lower()
        context = ssl.create_default_context()
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, timeout=10, context=context)
        else:
            smtp = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=10)
        try:
            if security == "starttls":
                smtp.starttls(context=context)
            if self.s.smtp_user:
                smtp.login(self.s.smtp_user, self.s.smtp_password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        self.sent += 1
        self.last_sent_at = time.time()
        self.last_error = None
        log.info("email alert sent: %s", mail.subject)
