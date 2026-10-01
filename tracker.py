"""Local DFW-to-Japan fare tracking. Never books or purchases flights."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import sys
import uuid
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("flight-tracker")
HEADERS = ["record_id", "checked_at_utc", "tracking_day_chicago", "origin", "destination",
           "departure", "return", "adults", "currency", "total_fare", "per_person",
           "starting_per_person", "daily_low_per_person", "drop_percent", "status",
           "offer_id", "operating_carriers", "expires_at"]


class ServiceError(RuntimeError):
    """Safe error text: credentials and authenticated URLs are never included."""


def api(session, service, method, url, **kwargs):
    try:
        response = session.request(method, url, timeout=90, **kwargs)
    except requests.RequestException:
        raise ServiceError(f"{service}: network request failed") from None
    if not response.ok:
        # Only Duffel's documented code fields; never log response text or URL.
        codes = []
        if service == "Duffel":
            try:
                codes = [str(e.get("code", "unknown")) for e in response.json().get("errors", [])]
            except (ValueError, AttributeError):
                pass
        safe_codes = [c for c in codes if c.replace("_", "").isalnum()]
        raise ServiceError(f"{service}: HTTP {response.status_code}" +
                           (f" ({', '.join(safe_codes)})" if safe_codes else ""))
    try:
        return response.json()
    except ValueError:
        raise ServiceError(f"{service}: invalid JSON response") from None


def tracking_day(now, config):
    local = now.astimezone(ZoneInfo(config["timezone"]))
    day = local.date()
    if local.hour < config["reset_hour"]:
        day -= timedelta(days=1)
    return day.isoformat()


def read_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["origin"] == "DFW", "Origin must be DFW"
    assert config["destinations"] == ["HND", "NRT"], "Destinations must be HND and NRT"
    assert config["adults"] == 3, "Exactly three adult travelers required"
    assert config["cabin_class"] == "economy", "Economy cabin required"
    assert config["max_stops"] == 1, "Maximum one stop required"
    assert config["currency"] == "USD", "USD fares required"
    assert config["timezone"] == "America/Chicago" and config["reset_hour"] == 8
    assert len(config["date_pairs"]) == 4
    for outbound, inbound in config["date_pairs"]:
        assert date.fromisoformat(outbound) < date.fromisoformat(inbound)
    assert 0 < config["alert_drop_percent"] < 100
    assert config["alert_price_per_person"] > 0
    return config


def missing_credentials():
    names = [n for n in ("DUFFEL_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                         "GOOGLE_SHEET_ID") if not os.getenv(n, "").strip()]
    if not (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip() or
            os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()):
        names.append("GOOGLE_SERVICE_ACCOUNT_JSON (or local GOOGLE_SERVICE_ACCOUNT_FILE)")
    return names


def eligible(offer, config, now):
    if offer.get("total_currency") != config["currency"]:
        return False
    if not config["allow_test_fares"] and offer.get("live_mode") is not True:
        return False
    if len(offer.get("passengers", [])) != config["adults"]:
        return False
    slices = offer.get("slices", [])
    if len(slices) != 2:
        return False
    for item in slices:
        segments = item.get("segments", [])
        if not segments or len(segments) > config["max_stops"] + 1:
            return False
        for segment in segments:
            passengers = segment.get("passengers", [])
            if len(passengers) != config["adults"]:
                return False
            if any(p.get("cabin_class") != config["cabin_class"] for p in passengers):
                return False
    try:
        expires = datetime.fromisoformat(offer["expires_at"].replace("Z", "+00:00"))
        return expires > now and Decimal(offer["total_amount"]) > 0
    except (KeyError, ValueError, ArithmeticError, TypeError):
        return False


def search(session, config, destination, outbound, inbound, now):
    body = {"data": {"slices": [
        {"origin": config["origin"], "destination": destination, "departure_date": outbound},
        {"origin": destination, "destination": config["origin"], "departure_date": inbound}],
        "passengers": [{"type": "adult"} for _ in range(config["adults"])],
        "cabin_class": config["cabin_class"], "max_connections": config["max_stops"]}}
    headers = {"Authorization": "Bearer " + os.environ["DUFFEL_ACCESS_TOKEN"],
               "Duffel-Version": "v2", "Accept": "application/json"}
    request = api(session, "Duffel", "POST", "https://api.duffel.com/air/offer_requests",
                  headers=headers, params={"return_offers": "false", "supplier_timeout": 60000},
                  json=body)["data"]
    best = None
    params = {"offer_request_id": request["id"], "sort": "total_amount",
              "max_connections": config["max_stops"], "limit": 200}
    seen = set()
    while True:
        page = api(session, "Duffel", "GET", "https://api.duffel.com/air/offers",
                   headers=headers, params=params)
        for offer in page["data"]:
            if eligible(offer, config, now):
                if best is None or Decimal(offer["total_amount"]) < Decimal(best["total_amount"]):
                    best = offer
        cursor = page.get("meta", {}).get("after")
        if not cursor:
            break
        if cursor in seen:
            raise ServiceError("Duffel: repeated pagination cursor")
        seen.add(cursor)
        params["after"] = cursor
    return best


def open_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS daily (
      day TEXT, route TEXT, start TEXT NOT NULL, low TEXT NOT NULL,
      PRIMARY KEY(day, route));
    CREATE TABLE IF NOT EXISTS history (
      id TEXT PRIMARY KEY, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS alerts (
      id TEXT PRIMARY KEY, message TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
    """)
    return conn


def fare_decision(price, start, low, config):
    drop = (start - price) / start * 100
    reasons = []
    if price <= Decimal(str(config["alert_price_per_person"])):
        reasons.append("fare at or below $900/person")
    if drop >= Decimal(str(config["alert_drop_percent"])):
        reasons.append("20%+ drop from today's starting fare")
    return min(low, price), drop, reasons


def record(conn, config, destination, outbound, inbound, offer, now, status="no_eligible_offers"):
    day = tracking_day(now, config)
    route = f"{config['origin']}-{destination}|{outbound}|{inbound}"
    row_id = uuid.uuid4().hex
    total = price = start = low = drop = ""
    carriers = ""
    if offer:
        total = Decimal(offer["total_amount"])
        # Keep full precision for decisions; round only when displaying.
        price = total / config["adults"]
        prior = conn.execute("SELECT start, low FROM daily WHERE day=? AND route=?", (day, route)).fetchone()
        start = Decimal(prior["start"]) if prior else price
        previous_low = Decimal(prior["low"]) if prior else price
        low, drop, reasons = fare_decision(price, start, previous_low, config)
        conn.execute("INSERT OR REPLACE INTO daily VALUES(?,?,?,?)", (day, route, str(start), str(low)))
        carriers = ", ".join(sorted({s["operating_carrier"]["name"] for sl in offer["slices"] for s in sl["segments"]}))
        status = "fare_found"
        # Queue once per itinerary/day/price; a new lower qualifying fare gets a new alert.
        if reasons and (not prior or price < previous_low):
            alert_id = f"{day}|{route}|{price}"
            message = (f"Japan flight alert: {config['origin']} to {destination}\n"
                       f"{outbound} to {inbound}, 3 adults, economy, max 1 stop each way\n"
                       f"${money(price)}/person; ${money(total)} total (USD)\n"
                       f"Day starting fare: ${money(start)}/person; day low: ${money(low)}/person\n"
                       f"Drop: {money(drop)}%\nReason: {'; '.join(reasons)}\n"
                       f"Operating carriers: {carriers}\nOffer: {offer['id']}\n"
                       f"Expires: {offer['expires_at']}\nSearch quote only; no booking made.")
            conn.execute("INSERT OR IGNORE INTO alerts(id,message) VALUES(?,?)", (alert_id, message))
    values = [row_id, now.isoformat(), day, config["origin"], destination, outbound, inbound,
              config["adults"], config["currency"], money(total), money(price), money(start),
              money(low), money(drop), status, offer["id"] if offer else "", carriers,
              offer["expires_at"] if offer else ""]
    conn.execute("INSERT INTO history(id,payload) VALUES(?,?)", (row_id, json.dumps(values)))
    conn.commit()
    if offer:
        LOG.info("%s: $%s/person; daily low $%s", route, money(price), money(low))
    else:
        LOG.info("%s: %s", route, status)


def money(value):
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if isinstance(value, Decimal) else value


def deliver_telegram(conn, session):
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        return
    for row in conn.execute("SELECT * FROM alerts WHERE delivered=0").fetchall():
        result = api(session, "Telegram", "POST",
                     "https://api.telegram.org/bot" + os.environ["TELEGRAM_BOT_TOKEN"] + "/sendMessage",
                     json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": row["message"]})
        if not result.get("ok"):
            raise ServiceError("Telegram: message was not accepted")
        conn.execute("UPDATE alerts SET delivered=1 WHERE id=?", (row["id"],))
        conn.commit()


def deliver_sheets(conn, config):
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    filename = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not os.getenv("GOOGLE_SHEET_ID") or not (raw or filename):
        return
    pending = conn.execute("SELECT * FROM history WHERE delivered=0").fetchall()
    if not pending:
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        info = json.loads(raw) if raw else json.loads(Path(filename).read_text(encoding="utf-8"))
        creds = Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        client = gspread.authorize(creds)
        client.set_timeout(60)
        sheet = client.open_by_key(os.environ["GOOGLE_SHEET_ID"])
        try:
            tab = sheet.worksheet(config["sheet_tab"])
        except gspread.WorksheetNotFound:
            tab = sheet.add_worksheet(title=config["sheet_tab"], rows=1000, cols=len(HEADERS))
        existing = set(tab.col_values(1))
        if not existing:
            tab.append_row(HEADERS, value_input_option="RAW")
        # IDs make a retry safe after a timeout or crash during a Sheets append.
        new_rows = [json.loads(row["payload"]) for row in pending if row["id"] not in existing]
        if new_rows:
            tab.append_rows(new_rows, value_input_option="RAW")
        conn.executemany("UPDATE history SET delivered=1 WHERE id=?", [(r["id"],) for r in pending])
        conn.commit()
    except Exception:
        raise ServiceError("Google Sheets: delivery failed; check credentials, API enablement and sheet sharing") from None


def run(config):
    if not os.getenv("DUFFEL_ACCESS_TOKEN"):
        LOG.warning("Search skipped: missing DUFFEL_ACCESS_TOKEN. Configuration is valid.")
        return 0
    conn = open_db(ROOT / "data" / "tracker.sqlite3")
    failed = False
    now = datetime.now(timezone.utc)
    with requests.Session() as session:
        for destination in config["destinations"]:
            for outbound, inbound in config["date_pairs"]:
                now = datetime.now(timezone.utc)
                if date.fromisoformat(outbound) <= now.astimezone(ZoneInfo(config["timezone"])).date():
                    record(conn, config, destination, outbound, inbound, None, now, "departure_passed")
                    continue
                try:
                    offer = search(session, config, destination, outbound, inbound, now)
                    now = datetime.now(timezone.utc)
                    if offer and not eligible(offer, config, now):
                        offer = None
                    record(conn, config, destination, outbound, inbound, offer, now)
                except ServiceError as exc:
                    failed = True
                    LOG.error("%s %s/%s: %s", destination, outbound, inbound, exc)
                    record(conn, config, destination, outbound, inbound, None, now, str(exc))
        # Deliver independently; failed delivery stays queued for the next run.
        for callback in (lambda: deliver_sheets(conn, config), lambda: deliver_telegram(conn, session)):
            try:
                callback()
            except ServiceError as exc:
                failed = True
                LOG.error("%s", exc)
    best = conn.execute("SELECT MIN(CAST(low AS REAL)) FROM daily WHERE day=?", (tracking_day(now, config),)).fetchone()[0]
    LOG.info("Today's overall low: %s", f"${best:.2f}/person" if best is not None else "no eligible fares yet")
    conn.close()
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true", help="Offline configuration and credentials check; no searches")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    handlers = [logging.StreamHandler(sys.stdout)] if sys.stdout else []
    if not args.validate:
        (ROOT / "logs").mkdir(exist_ok=True)
        handlers.append(RotatingFileHandler(ROOT / "logs" / "tracker.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    try:
        config = read_config(ROOT / "config.json")
        LOG.info("Configuration valid: DFW -> HND/NRT, 3 adults, 8 round-trip searches. Reset 08:00 America/Chicago.")
        missing = missing_credentials()
        if missing:
            LOG.warning("Missing credentials: %s", "; ".join(missing))
        if args.validate:
            summary = os.getenv("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a", encoding="utf-8") as handle:
                    handle.write("Configuration validation passed. Local execution only.\n\n")
                    handle.write("Missing credentials: " + (", ".join(missing) if missing else "none") + "\n")
            return 0
        # Prevent overlapping local invocations; OS releases the lock after a crash.
        (ROOT / "data").mkdir(exist_ok=True)
        with open(ROOT / "data" / "run.lock", "a+b") as lock:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    LOG.info("Another tracker run is active; skipping.")
                    return 0
            else:
                import fcntl
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    LOG.info("Another tracker run is active; skipping.")
                    return 0
            return run(config)
    except (AssertionError, ValueError, KeyError, OSError, sqlite3.Error) as exc:
        # Configuration errors are visible without printing potentially secret data.
        LOG.error("Local configuration or storage error (%s). Check config.json, .env, and directory permissions.", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
