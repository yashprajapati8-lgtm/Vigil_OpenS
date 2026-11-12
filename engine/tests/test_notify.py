"""Email alerts: the right moments, once each, with the steps VIGIL took, and never
able to slow or break VIGIL. A fake SMTP server stands in for Gmail."""

import asyncio
import time

import pytest

import vigil.notify as notify
from vigil.bus import Bus
from vigil.config import Settings
from vigil.engine import Engine
from vigil.store import Store

from test_remediation import FakeLLM, FlashWorld, ScaleActuator, kinds, run_ticks, setup

MAIL = dict(smtp_host="smtp.test", smtp_port=587, smtp_user="vigil@test.dev", smtp_password="app-pass-123",
            alert_to=("oncall@test.dev",), console_url="http://vigil.test:3000")


class FakeSMTP:
    """Records what VIGIL would have sent. `fail` makes the server refuse."""
    sent: list = []
    calls: list = []
    fail: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None):
        FakeSMTP.calls.append(("connect", host, port, timeout))
        if FakeSMTP.fail:
            raise FakeSMTP.fail

    def starttls(self, context=None):
        FakeSMTP.calls.append(("starttls",))

    def login(self, user, password):
        FakeSMTP.calls.append(("login", user, password))

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.calls, FakeSMTP.fail = [], [], None
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)


def drain(eng, expect=None, timeout=5.0):
    """Wait for the background sender to finish what it has queued."""
    eng.notifier._q.join()
    if expect is not None:
        deadline = time.time() + timeout
        while len(FakeSMTP.sent) < expect and time.time() < deadline:
            time.sleep(0.01)
    return FakeSMTP.sent


def bodies(msg):
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    return text, html


def flash_sale(tmp_path, **mail):
    """The demo: a 6x flash sale; VIGIL adds a replica on its own and verifies."""
    world = FlashWorld()
    s = Settings(data_dir=tmp_path, baseline_seconds=0, diagnose_delay_s=0, verify_settle_s=0, autonomy="L2", **mail)
    store = Store(s.db_path)
    act = ScaleActuator(world)
    llm = FakeLLM(lambda pack: {"status": "ok", "root_cause_service": "payment", "category": "load_saturation",
                                "hypothesis": "6x traffic, CPU per request flat: real demand",
                                "evidence_ids": [e["id"] for e in pack["evidence"][:3]], "ruled_out": []})
    eng = Engine(s, collector=None, store=store, bus=Bus(), llm=llm, actuator=act)
    for _ in range(30):
        eng.process(world.snapshot())
    world.sale = True
    for _ in range(4):
        eng.process(world.snapshot())
    inc_id = store.list_incidents()[0]["id"]
    asyncio.run(eng.diagnose(inc_id))
    eng.process(world.snapshot())
    asyncio.run(eng.remediator.handle(inc_id))
    run_ticks(eng, world, 6)
    return eng, store, inc_id


def test_flash_sale_sends_two_emails_acting_then_fixed(tmp_path):
    eng, store, inc_id = flash_sale(tmp_path, **MAIL)
    assert store.get_incident(inc_id)["status"] == "RESOLVED"
    sent = drain(eng, expect=2)
    subjects = [m["Subject"] for m in sent]
    assert subjects == [f"[VIGIL] Demo shop: fixing {inc_id} on its own (add a payment replica)",
                        subjects[1]], subjects
    assert subjects[1].startswith(f"[VIGIL] Demo shop: fixed {inc_id} in ")

    acting_text, acting_html = bodies(sent[0])
    assert "VIGIL found the cause and is fixing it: add a payment replica." in acting_text
    assert "Root cause: payment (load saturation), confidence" in acting_text
    assert "saves ~" in acting_text and "checkouts in the next 5 min" in acting_text
    assert "What VIGIL did, step by step:" in acting_text
    for step in ("opened", "Blame walk", "Root cause", "Decision", "Acting alone"):
        assert step in acting_text, step
    assert f"http://vigil.test:3000/incidents/{inc_id}" in acting_text and "Open in the VIGIL console" in acting_html

    fixed_text, _ = bodies(sent[1])
    assert "Fixed and verified in" in fixed_text
    assert "checkouts failed before recovery" in fixed_text
    assert "resolved and verified" in fixed_text                    # the last step is in the timeline
    assert sent[1]["To"] == "oncall@test.dev" and sent[1]["From"] == "vigil@test.dev"


def test_off_unless_configured(tmp_path):
    eng, store, inc_id = flash_sale(tmp_path)
    assert eng.notifier.enabled is False
    assert drain(eng) == [] and FakeSMTP.calls == []
    assert eng.health()["alerts"]["email"] == "off"


def test_each_moment_is_emailed_once_per_incident(tmp_path):
    eng, store, inc_id = flash_sale(tmp_path, **MAIL)
    drain(eng, expect=2)
    resolved = next(e for e in store.events(incident_id=inc_id) if e["kind"] == "resolved")
    eng.bus.publish({"type": "event", "event": resolved})          # the same moment again
    assert len(drain(eng)) == 2


def test_waiting_for_a_human_says_what_to_do(tmp_path):
    world, eng, store, act, inc_id = setup(tmp_path, autonomy="L1", **MAIL)
    asyncio.run(eng.remediator.handle(inc_id))
    assert "awaiting_approval" in kinds(store, inc_id)
    sent = drain(eng, expect=1)
    assert [m["Subject"] for m in sent] == [f"[VIGIL] Demo shop: approval needed for {inc_id} (restart inventory)"]
    text, _ = bodies(sent[0])
    assert "VIGIL wants to restart inventory, and is waiting for you." in text
    assert "approve or reject" in text


def test_an_escalation_says_why(tmp_path):
    world, eng, store, act, inc_id = setup(tmp_path, fixes=False, verify_timeout_s=0, **MAIL)
    asyncio.run(eng.remediator.handle(inc_id))
    run_ticks(eng, world, 2)
    asyncio.run(eng.remediator.handle(inc_id))
    assert store.get_incident(inc_id)["status"] == "ESCALATED"
    sent = drain(eng, expect=2)
    subjects = [m["Subject"] for m in sent]
    assert subjects[0].startswith(f"[VIGIL] Demo shop: fixing {inc_id} on its own")
    assert subjects[-1] == f"[VIGIL] Demo shop: {inc_id} needs you"
    text, _ = bodies(sent[-1])
    assert "VIGIL stopped and handed this to a human." in text and "Why: " in text


def test_a_broken_mail_server_never_breaks_vigil(tmp_path):
    FakeSMTP.fail = OSError("connection refused")
    eng, store, inc_id = flash_sale(tmp_path, **MAIL)
    assert store.get_incident(inc_id)["status"] == "RESOLVED"        # VIGIL carried on
    drain(eng)
    status = eng.health()["alerts"]
    assert status["email"] == "on" and status["sent"] == 0 and "connection refused" in status["last_error"]


def test_the_password_never_appears_in_status_or_logs(tmp_path, caplog):
    FakeSMTP.fail = OSError("535 bad credentials")
    eng, store, inc_id = flash_sale(tmp_path, **MAIL)
    drain(eng)
    assert "app-pass-123" not in str(eng.health()) and "app-pass-123" not in caplog.text
    assert eng.health()["alerts"]["to"] == ["o***@test.dev"]


def test_starttls_and_login_by_default_ssl_when_asked(tmp_path):
    eng, store, inc_id = flash_sale(tmp_path, **MAIL)
    drain(eng, expect=2)
    assert ("connect", "smtp.test", 587, 10) in FakeSMTP.calls
    assert ("starttls",) in FakeSMTP.calls and ("login", "vigil@test.dev", "app-pass-123") in FakeSMTP.calls
    FakeSMTP.calls = []
    s = Settings(data_dir=tmp_path / "ssl", **{**MAIL, "smtp_port": 465, "smtp_security": "ssl"})
    notify.Notifier(s, Store(s.db_path)).send_test()
    assert ("starttls",) not in FakeSMTP.calls and ("connect", "smtp.test", 465, 10) in FakeSMTP.calls


def test_test_email_needs_config_and_says_so(tmp_path):
    s = Settings(data_dir=tmp_path)
    with pytest.raises(RuntimeError, match="VIGIL_SMTP_HOST and VIGIL_ALERT_TO"):
        notify.Notifier(s, Store(s.db_path)).send_test()
    s = Settings(data_dir=tmp_path / "on", **MAIL)
    notify.Notifier(s, Store(s.db_path)).send_test()
    assert FakeSMTP.sent[-1]["Subject"] == "[VIGIL] Demo shop: test email"


def test_hourly_cap(tmp_path):
    s = Settings(data_dir=tmp_path, alert_max_per_hour=3, **MAIL)
    n = notify.Notifier(s, Store(s.db_path))
    for i in range(6):
        n.on_message({"type": "event", "event": {"kind": "telemetry_degraded", "incident_id": None,
                                                 "payload": {"service": f"s{i}"}, "summary": "Prometheus unreachable"}})
    n._q.join()
    assert len(FakeSMTP.sent) == 3
    assert FakeSMTP.sent[0]["Subject"] == "[VIGIL] Demo shop: VIGIL can't see your services"


def test_site_words_reach_the_email(tmp_path):
    s = Settings(data_dir=tmp_path, site_name="University results portal", unit="result lookup",
                 units="result lookups", **MAIL)
    n = notify.Notifier(s, Store(s.db_path))
    n.send_test()
    assert FakeSMTP.sent[-1]["Subject"] == "[VIGIL] University results portal: test email"


# ---- recipients added in the console

SENDER = {k: v for k, v in MAIL.items() if k != "alert_to"}      # an account to send from, nobody to send to


def test_sender_without_recipient_says_what_is_missing(tmp_path):
    s = Settings(data_dir=tmp_path, **SENDER)
    n = notify.Notifier(s, Store(s.db_path))
    st = n.status()
    assert (st["email"], st["sender"], st["recipients"]) == ("off", True, [])
    assert "add an email address" in st["why"]
    with pytest.raises(RuntimeError, match="no one to send to"):
        n.send_test()
    st = notify.Notifier(Settings(data_dir=tmp_path / "x"), Store(Settings(data_dir=tmp_path / "x").db_path)).status()
    assert st["sender"] is False and "VIGIL_SMTP_USER" in st["why"]


def test_console_recipients_get_alerts_and_survive_a_restart(tmp_path):
    s = Settings(data_dir=tmp_path, **SENDER)
    n = notify.Notifier(s, Store(s.db_path))
    n.add_recipient("  judge@college.edu ")
    n.add_recipient("me@gmail.com")
    assert n.enabled and n.status()["to"] == ["j***@college.edu", "m***@gmail.com"]
    n.send_test()
    assert FakeSMTP.sent[-1]["To"] == "judge@college.edu, me@gmail.com"
    again = notify.Notifier(s, Store(s.db_path))                  # VIGIL restarted
    assert again.recipients == ["judge@college.edu", "me@gmail.com"]
    again.remove_recipient(notify.recipient_id("judge@college.edu"))
    assert notify.Notifier(s, Store(s.db_path)).recipients == ["me@gmail.com"]


@pytest.mark.parametrize("bad, why", [
    ("not-an-email", "doesn't look like an email"),
    ("a@b", "doesn't look like an email"),
    ("x@y.com, z@w.com", "doesn't look like an email"),       # one address at a time: no header tricks
    ("me@gmail.com", "already gets alerts"),
    ("ME@gmail.com", "already gets alerts"),
])
def test_bad_or_duplicate_addresses_are_refused(tmp_path, bad, why):
    s = Settings(data_dir=tmp_path, **SENDER)
    n = notify.Notifier(s, Store(s.db_path))
    n.add_recipient("me@gmail.com")
    with pytest.raises(notify.RecipientError, match=why):
        n.add_recipient(bad)


def test_at_most_ten_and_env_addresses_stay(tmp_path):
    s = Settings(data_dir=tmp_path, **MAIL)                       # oncall@test.dev comes from .env
    n = notify.Notifier(s, Store(s.db_path))
    for i in range(9):
        n.add_recipient(f"p{i}@test.dev")
    with pytest.raises(notify.RecipientError, match="at most 10"):
        n.add_recipient("one-too-many@test.dev")
    with pytest.raises(notify.RecipientError, match="set in .env"):
        n.remove_recipient(notify.recipient_id("oncall@test.dev"))
    assert n.status()["recipients"][0] == {"id": notify.recipient_id("oncall@test.dev"),
                                            "masked": "o***@test.dev", "source": ".env"}
    with pytest.raises(notify.RecipientError, match="no such address"):
        n.remove_recipient("nope")


def test_a_damaged_recipients_file_never_stops_vigil(tmp_path):
    (tmp_path / "alert_recipients.json").write_text("{not json")
    s = Settings(data_dir=tmp_path, **SENDER)
    assert notify.Notifier(s, Store(s.db_path)).recipients == []
