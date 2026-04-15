"""
Nexus Health Monitor v2 — Railway Cloud Service
================================================
Full instantaneous detection + diagnostic + self-healing system.

Detection flow:
  1. Each agent pushes heartbeat every 30s → /heartbeat endpoint
  2. Missed heartbeat within 60s → IMMEDIATE alert (not 5-min poll)
  3. Diagnostic classifier runs → identifies error site + failure type
  4. Tiered heal action selected based on diagnosis
  5. Recovery confirmed to Health Group when service restores

Poll-based checks run every 5 min as a safety net in addition to heartbeats.

Endpoints:
  POST /heartbeat          — Agents push heartbeat here every 30s
  GET  /health             — This service's own health (Railway healthcheck)
  GET  /status             — Full current status of all watched services
  POST /test               — Trigger a manual check cycle (auth required)

Author: OMNI 🌐
Date: 2026-04-08 v2
"""

import os
import time
import threading
import datetime
import requests
import pytz
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Nexus Health Monitor v2", version="2.0.0")

# ── Config ────────────────────────────────────────────────────────────────────

TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN",   "")
HEALTH_GROUP   = os.getenv("HEALTH_GROUP_ID", "-5184172590")
AHMED_DM       = os.getenv("AHMED_CHAT_ID",   "8573754783")
NEXUS_SECRET   = os.getenv("NEXUS_SECRET",    "")
RAILWAY_TOKEN  = os.getenv("RAILWAY_TOKEN",   "")

HEARTBEAT_INTERVAL_SEC = 30    # agents push every 30s
HEARTBEAT_TIMEOUT_SEC  = 75    # missed if no ping in 75s (2.5x interval, buffer for latency)
POLL_INTERVAL_SEC      = 300   # backup poll every 5 min
FAILURE_THRESHOLD      = 2     # consecutive poll failures before alert (if heartbeat missed, alert is immediate)

ET_TZ        = pytz.timezone("America/New_York")
MARKET_OPEN  = datetime.time(9, 25)
MARKET_CLOSE = datetime.time(16, 5)

# ── Services ──────────────────────────────────────────────────────────────────

SERVICES = {
    "Axiom": {
        "display":    "Axiom 🔷",
        "url":        os.getenv("AXIOM_URL",  "https://axiom-production-334c.up.railway.app") + "/health",
        "railway_id": os.getenv("AXIOM_RAILWAY_SERVICE_ID", ""),
        "tier":       3,
    },
    "Alpha": {
        "display":    "Alpha 🔵",
        "url":        os.getenv("ALPHA_URL",  "https://worker-production-2060.up.railway.app") + "/health",
        "railway_id": os.getenv("ALPHA_RAILWAY_SERVICE_ID", ""),
        "tier":       3,
    },
    "Prime": {
        "display":    "Prime 🟢",
        "url":        os.getenv("PRIME_URL",  "https://nexus-prime-bot-production.up.railway.app") + "/health",
        "railway_id": os.getenv("PRIME_RAILWAY_SERVICE_ID", ""),
        "tier":       3,
    },
}

# ── State ─────────────────────────────────────────────────────────────────────

_state: dict = {
    name: {
        "status":           "UNKNOWN",
        "latency_ms":       0,
        "failure_count":    0,
        "alert_sent":       False,
        "last_heartbeat":   None,   # ISO timestamp of last received heartbeat
        "heartbeat_missed": False,
        "last_up":          None,
        "last_check":       None,
        "last_data":        {},
        "failure_type":     None,   # classified failure type
        "heal_attempts":    0,
    }
    for name in SERVICES
}

_monitor_start   = datetime.datetime.utcnow()
_last_pulse_hour = -1
_state_lock      = threading.Lock()

# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg(chat_id: str, text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[MONITOR] TG failed → {chat_id}: {e}")
        return False

def _alert(text: str, tier: int = 2):
    _tg(HEALTH_GROUP, text)
    if tier >= 3:
        _tg(AHMED_DM, text)

# ── Diagnostic Classifier ─────────────────────────────────────────────────────

FAILURE_TYPES = {
    "CONNECTION_REFUSED": {
        "label":  "Connection Refused",
        "emoji":  "🔌",
        "cause":  "Service process crashed or port not open",
        "action": "REDEPLOY",
        "detail": "The service is not accepting connections. Process likely crashed.",
    },
    "TIMEOUT": {
        "label":  "Timeout",
        "emoji":  "⏱️",
        "cause":  "Service overloaded, frozen, or out of memory",
        "action": "REDEPLOY",
        "detail": "Service is not responding within 8s. Possible OOM or deadlock.",
    },
    "HTTP_500": {
        "label":  "Internal Server Error",
        "emoji":  "💥",
        "cause":  "Application error in health endpoint",
        "action": "REDEPLOY",
        "detail": "Service is up but returning 500. Application-level crash.",
    },
    "HTTP_503": {
        "label":  "Service Unavailable",
        "emoji":  "🚫",
        "cause":  "Service starting up or overloaded",
        "action": "WAIT_THEN_CHECK",
        "detail": "Service returning 503. May be restarting — wait 60s before redeploying.",
    },
    "HTTP_OTHER": {
        "label":  "Unexpected HTTP Error",
        "emoji":  "⚠️",
        "cause":  "Unexpected HTTP status from health endpoint",
        "action": "REDEPLOY",
        "detail": "Unexpected HTTP response. Service behavior is abnormal.",
    },
    "DNS_FAILURE": {
        "label":  "DNS Resolution Failure",
        "emoji":  "🌐",
        "cause":  "Railway domain unreachable — possible Railway infrastructure issue",
        "action": "ALERT_ONLY",
        "detail": "Cannot resolve hostname. This may be a Railway-wide issue, not isolated to this service.",
    },
    "HEARTBEAT_MISSED": {
        "label":  "Heartbeat Missed",
        "emoji":  "💓",
        "cause":  "Agent stopped emitting heartbeats — process may be frozen or crashed",
        "action": "DIAGNOSE_THEN_HEAL",
        "detail": "No heartbeat received in 75s. Running diagnostic before healing.",
    },
    "UNKNOWN": {
        "label":  "Unknown Failure",
        "emoji":  "❓",
        "cause":  "Unclassified error",
        "action": "REDEPLOY",
        "detail": "Unable to classify failure. Attempting redeploy.",
    },
}

def _classify_failure(error_str: str, status_code: int = 0) -> str:
    """Classify a failure into a known failure type."""
    err = (error_str or "").lower()
    if "connection refused" in err or "connectionrefused" in err:
        return "CONNECTION_REFUSED"
    if "timeout" in err or "timed out" in err:
        return "TIMEOUT"
    if "name or service not known" in err or "nodename nor servname" in err or "dns" in err:
        return "DNS_FAILURE"
    if status_code == 500:
        return "HTTP_500"
    if status_code == 503:
        return "HTTP_503"
    if status_code and status_code not in (200, 0):
        return "HTTP_OTHER"
    return "UNKNOWN"

def _run_diagnostic(name: str, url: str) -> dict:
    """
    Full diagnostic pass on a failed service.
    Returns: {failure_type, error, status_code, latency_ms, heal_action, detail}
    """
    t0 = time.time()
    try:
        r = requests.get(url, timeout=8)
        latency = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            return {
                "failure_type": None,
                "status_code":  200,
                "latency_ms":   latency,
                "heal_action":  "NONE",
                "detail":       "Service is UP — false alarm or already recovered",
                "error":        None,
            }
        ft = _classify_failure("", r.status_code)
        info = FAILURE_TYPES[ft]
        return {
            "failure_type": ft,
            "status_code":  r.status_code,
            "latency_ms":   latency,
            "heal_action":  info["action"],
            "detail":       info["detail"],
            "error":        f"HTTP {r.status_code}",
        }
    except requests.exceptions.ConnectionError as e:
        ft = _classify_failure(str(e))
        info = FAILURE_TYPES[ft]
        return {
            "failure_type": ft,
            "status_code":  0,
            "latency_ms":   int((time.time() - t0) * 1000),
            "heal_action":  info["action"],
            "detail":       info["detail"],
            "error":        str(e)[:120],
        }
    except requests.exceptions.Timeout:
        info = FAILURE_TYPES["TIMEOUT"]
        return {
            "failure_type": "TIMEOUT",
            "status_code":  0,
            "latency_ms":   8000,
            "heal_action":  info["action"],
            "detail":       info["detail"],
            "error":        "Timeout after 8s",
        }
    except Exception as e:
        return {
            "failure_type": "UNKNOWN",
            "status_code":  0,
            "latency_ms":   int((time.time() - t0) * 1000),
            "heal_action":  "REDEPLOY",
            "detail":       FAILURE_TYPES["UNKNOWN"]["detail"],
            "error":        str(e)[:120],
        }

# ── Railway redeploy ──────────────────────────────────────────────────────────

def _redeploy(name: str, service_id: str) -> bool:
    if not RAILWAY_TOKEN or not service_id:
        return False
    try:
        r = requests.post(
            "https://backboard.railway.app/graphql/v2",
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
            json={
                "query": "mutation serviceInstanceRedeploy($serviceId: String!) { serviceInstanceRedeploy(serviceId: $serviceId) }",
                "variables": {"serviceId": service_id},
            },
            timeout=15,
        )
        return r.status_code == 200 and "errors" not in r.json()
    except Exception:
        return False

# ── Heal dispatcher ───────────────────────────────────────────────────────────

def _execute_heal(name: str, config: dict, diag: dict) -> str:
    """
    Execute the appropriate heal action based on diagnostic result.
    Returns a human-readable description of what was done.
    """
    action     = diag.get("heal_action", "REDEPLOY")
    display    = config["display"]
    service_id = config.get("railway_id", "")

    with _state_lock:
        _state[name]["heal_attempts"] += 1
        attempts = _state[name]["heal_attempts"]

    if action == "ALERT_ONLY":
        return "⚠️ Alert-only (DNS/infrastructure issue) — manual check required"

    if action == "WAIT_THEN_CHECK":
        # Service returning 503 — wait 60s then re-check before redeploying
        threading.Timer(60, lambda: _deferred_check(name, config)).start()
        return "⏳ HTTP 503 detected — waiting 60s before attempting redeploy"

    if action in ("REDEPLOY", "DIAGNOSE_THEN_HEAL", "UNKNOWN"):
        if service_id and RAILWAY_TOKEN:
            ok = _redeploy(name, service_id)
            if ok:
                return f"🔄 Railway redeploy triggered (attempt #{attempts})"
            else:
                return f"⚠️ Railway redeploy failed (attempt #{attempts}) — manual restart required"
        else:
            return "⚠️ No RAILWAY_TOKEN or service ID — manual Railway restart required"

    return f"⚠️ Unknown heal action: {action}"

def _deferred_check(name: str, config: dict):
    """Re-check after 60s wait (for 503 responses)."""
    diag = _run_diagnostic(name, config["url"])
    if diag["failure_type"] is not None:
        # Still down after wait — escalate to redeploy
        heal_result = _execute_heal(name, config, {"heal_action": "REDEPLOY"})
        _tg(HEALTH_GROUP, f"⚠️ {config['display']} still DOWN after 60s wait. {heal_result}")

# ── Outage handler ────────────────────────────────────────────────────────────

def _handle_outage(name: str, config: dict, trigger: str = "POLL"):
    """
    Full outage response sequence:
    1. Run diagnostic
    2. Classify failure
    3. Send detailed alert (Health Group + Ahmed DM)
    4. Execute appropriate heal action
    5. Post heal confirmation
    """
    display = config["display"]
    url     = config["url"]

    print(f"[MONITOR] 🔴 OUTAGE: {display} | trigger={trigger} | running diagnostic...")

    # Step 1: Diagnostic
    diag = _run_diagnostic(name, url)
    ft   = diag.get("failure_type", "UNKNOWN")
    info = FAILURE_TYPES.get(ft, FAILURE_TYPES["UNKNOWN"])

    with _state_lock:
        _state[name]["failure_type"] = ft

    # Step 2: Build detailed alert
    now = _et_full()
    alert_msg = (
        f"🔴 <b>NEXUS CRITICAL — {display} OFFLINE</b>\n\n"
        f"{info['emoji']} <b>Failure Type:</b> {info['label']}\n"
        f"📍 <b>Error Site:</b> {info['cause']}\n"
        f"📋 <b>Detail:</b> {info['detail']}\n"
        f"🔧 <b>Heal Action:</b> {info['action']}\n"
        f"⚡ <b>Trigger:</b> {trigger}\n"
        f"🕐 <b>Detected:</b> {now}\n\n"
        f"<i>Raw error: {diag.get('error', 'N/A')}</i>"
    )
    _alert(alert_msg, tier=config["tier"])

    # Step 3: Execute heal
    heal_result = _execute_heal(name, config, diag)
    _tg(HEALTH_GROUP, f"🔧 <b>Healing {display}:</b> {heal_result}")

    print(f"[MONITOR] Heal dispatched for {display}: {heal_result}")

# ── Heartbeat receiver ────────────────────────────────────────────────────────

class HeartbeatPayload(BaseModel):
    service:    str             # "Axiom" | "Alpha" | "Prime"
    version:    Optional[str]  = None
    loop_active: Optional[bool] = None
    extra:      Optional[dict] = None

@app.post("/heartbeat")
def receive_heartbeat(payload: HeartbeatPayload):
    """
    Agents POST here every 30s.
    Updates last_heartbeat timestamp.
    If service was previously flagged DOWN, triggers recovery flow.
    """
    name = payload.service
    if name not in _state:
        raise HTTPException(status_code=400, detail=f"Unknown service: {name}")

    now_iso = datetime.datetime.utcnow().isoformat()
    with _state_lock:
        s = _state[name]
        prev_missed = s["heartbeat_missed"]
        s["last_heartbeat"]   = now_iso
        s["heartbeat_missed"] = False

        # If service was flagged as down due to missed heartbeat — recover
        if prev_missed and s["status"] == "DOWN":
            s["status"]        = "UP"
            s["failure_count"] = 0
            was_alerted        = s["alert_sent"]
            s["alert_sent"]    = False
            s["failure_type"]  = None
            s["heal_attempts"] = 0
        else:
            was_alerted = False
            if s["status"] == "UNKNOWN":
                s["status"] = "UP"

    if was_alerted:
        display = SERVICES[name]["display"]
        _tg(HEALTH_GROUP, f"🟢 <b>{display} RESTORED</b>\nHeartbeat resumed at {_et_now()}. Pipeline active.")
        _tg(AHMED_DM,     f"✅ {display} is back online — heartbeat restored at {_et_now()}.")

    return {"status": "ok", "received_at": now_iso}

# ── Heartbeat watchdog ────────────────────────────────────────────────────────

def _heartbeat_watchdog():
    """
    Runs every 30s. Checks for missed heartbeats.
    If no heartbeat in 75s → immediate outage response.
    Much faster than 5-min poll cycle.
    """
    print("[MONITOR] 💓 Heartbeat watchdog started")
    while True:
        time.sleep(30)
        now = datetime.datetime.utcnow()
        for name, config in SERVICES.items():
            with _state_lock:
                s         = _state[name]
                last_hb   = s["last_heartbeat"]
                already   = s["heartbeat_missed"]
                alerted   = s["alert_sent"]

            if last_hb is None:
                # Never received a heartbeat — skip until we get first one
                continue

            last_dt = datetime.datetime.fromisoformat(last_hb)
            age_sec = (now - last_dt).total_seconds()

            if age_sec > HEARTBEAT_TIMEOUT_SEC and not already:
                display = config["display"]
                print(f"[MONITOR] 💔 Heartbeat missed: {display} (last: {age_sec:.0f}s ago)")

                with _state_lock:
                    _state[name]["heartbeat_missed"] = True
                    _state[name]["status"]           = "DOWN"
                    if not alerted:
                        _state[name]["alert_sent"] = True

                if not alerted:
                    _handle_outage(name, config, trigger=f"HEARTBEAT_MISSED ({age_sec:.0f}s ago)")

# ── Poll-based backup checker ─────────────────────────────────────────────────

def _check_service(name: str, config: dict) -> dict:
    url = config["url"]
    t0  = time.time()
    try:
        r       = requests.get(url, timeout=8)
        latency = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            try:    data = r.json()
            except: data = {}
            return {"status": "UP", "latency_ms": latency, "data": data, "error": None}
        return {"status": "DOWN", "latency_ms": latency, "data": {}, "error": f"HTTP {r.status_code}"}
    except requests.exceptions.ConnectionError as e:
        return {"status": "DOWN", "latency_ms": 0, "data": {}, "error": f"Connection refused"}
    except requests.exceptions.Timeout:
        return {"status": "DOWN", "latency_ms": 8000, "data": {}, "error": "Timeout (8s)"}
    except Exception as e:
        return {"status": "DOWN", "latency_ms": 0, "data": {}, "error": str(e)[:80]}

def _poll_cycle():
    """Backup 5-min poll. Catches services that never sent heartbeats."""
    for name, config in SERVICES.items():
        result = _check_service(name, config)

        with _state_lock:
            s    = _state[name]
            prev = s["status"]
            s["last_check"]  = _et_full()
            s["latency_ms"]  = result["latency_ms"]
            s["last_data"]   = result.get("data", {})

        if result["status"] == "DOWN":
            with _state_lock:
                _state[name]["status"]        = "DOWN"
                _state[name]["failure_count"] += 1
                count   = _state[name]["failure_count"]
                alerted = _state[name]["alert_sent"]

            print(f"[MONITOR] [POLL] ⚠️ {config['display']} DOWN #{count}")

            if count >= FAILURE_THRESHOLD and not alerted:
                with _state_lock:
                    _state[name]["alert_sent"] = True
                _handle_outage(name, config, trigger=f"POLL (#{count} consecutive)")

        else:  # UP
            with _state_lock:
                s    = _state[name]
                prev = s["status"]
                was_alerted = s["alert_sent"]

                if s["last_heartbeat"] is None:
                    # Service is up via poll but never sent heartbeat — mark UP
                    s["status"] = "UP"

                if prev == "DOWN":
                    s["status"]        = "UP"
                    s["failure_count"] = 0
                    s["alert_sent"]    = False
                    s["failure_type"]  = None
                    s["heal_attempts"] = 0

            if prev == "DOWN" and was_alerted:
                display = config["display"]
                _tg(HEALTH_GROUP, f"🟢 <b>{display} RESTORED</b>\nDetected via poll | Latency: {result['latency_ms']}ms | {_et_full()}")
                _tg(AHMED_DM,     f"✅ {display} is back online at {_et_now()}.")

            print(f"[MONITOR] [POLL] ✅ {config['display']} UP | {result['latency_ms']}ms")

# ── Hourly pulse ──────────────────────────────────────────────────────────────

def _et_now() -> str:
    return datetime.datetime.now(ET_TZ).strftime("%H:%M ET")

def _et_full() -> str:
    return datetime.datetime.now(ET_TZ).strftime("%Y-%m-%d %H:%M ET")

def _is_market_hours() -> bool:
    t = datetime.datetime.now(ET_TZ).time()
    return MARKET_OPEN <= t <= MARKET_CLOSE

def _uptime() -> str:
    d = datetime.datetime.utcnow() - _monitor_start
    h, r = divmod(int(d.total_seconds()), 3600)
    return f"{h}h {r//60}m"

def _send_pulse():
    lines = []
    for name, config in SERVICES.items():
        s    = _state[name]
        icon = "✅" if s["status"] == "UP" else ("❌" if s["status"] == "DOWN" else "❓")
        lat  = f"{s['latency_ms']}ms" if s["latency_ms"] else "—"
        hb   = "💓 live" if not s["heartbeat_missed"] and s["last_heartbeat"] else ("💔 missed" if s["heartbeat_missed"] else "⚪ no HB")
        lines.append(f"  {icon} {config['display']}: {s['status']} | {lat} | {hb}")

    all_up = all(s["status"] == "UP" for s in _state.values())
    hdr    = "🟢" if all_up else "🔴"

    msg = (
        f"{hdr} <b>NEXUS HEALTH PULSE — {_et_full()}</b>\n\n"
        + "\n".join(lines) +
        f"\n\n<b>Monitor uptime:</b> {_uptime()}\n"
        f"<b>Detection:</b> Heartbeat (60s) + Poll (5min)\n"
        f"<b>Source:</b> Railway cloud watchdog\n\n"
        f"{'✅ All systems nominal' if all_up else '⚠️ DEGRADED — check alerts above'}"
    )
    _tg(HEALTH_GROUP, msg)
    print("[MONITOR] Hourly pulse sent")

# ── Main monitor loop ─────────────────────────────────────────────────────────

def _monitor_loop():
    global _last_pulse_hour
    print(f"[MONITOR] 🟢 Nexus Health Monitor v2 started — {_et_full()}")

    # Silent startup — no noise unless something is wrong
    logger.info("Nexus Health Monitor v2 started at %s", _et_full())

    while True:
        try:
            _poll_cycle()
            if _is_market_hours():
                h = datetime.datetime.now(ET_TZ).hour
                if h != _last_pulse_hour:
                    _last_pulse_hour = h
                    # Only send hourly pulse if something is degraded — silent when all UP
                    all_healthy = all(s["status"] == "UP" for s in _state.values())
                    if not all_healthy:
                        _send_pulse()
        except Exception as e:
            print(f"[MONITOR] Loop error: {e}")
        time.sleep(POLL_INTERVAL_SEC)

# ── FastAPI startup ───────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    # Start heartbeat watchdog
    threading.Thread(target=_heartbeat_watchdog, daemon=True, name="hb-watchdog").start()
    # Start poll loop
    threading.Thread(target=_monitor_loop, daemon=True, name="poll-loop").start()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status":    "healthy",
        "service":   "nexus-health-monitor",
        "version":   "2.0.0",
        "uptime":    _uptime(),
        "watching":  list(SERVICES.keys()),
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }

@app.get("/status")
def get_status():
    with _state_lock:
        snapshot = {k: dict(v) for k, v in _state.items()}
    return {
        "services":  snapshot,
        "all_up":    all(s["status"] == "UP" for s in snapshot.values()),
        "uptime":    _uptime(),
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }

@app.post("/test")
def test_cycle(x_nexus_secret: Optional[str] = Header(None)):
    if x_nexus_secret != NEXUS_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    threading.Thread(target=_poll_cycle, daemon=True).start()
    return {"status": "check cycle triggered"}

# ── Entry ─────────────────────────────────────────────────────────────────────



# ── Resilience Module Health Checks ──────────────────────────────────────────

def check_resilience_modules(alpha_health: dict) -> dict:
    """
    Check health of all resilience framework modules via Alpha /health endpoint.
    Called as part of the extended health check cycle.
    """
    results = {}
    
    # These modules report their status through Alpha /health
    scanner     = alpha_health.get("omni_scanner", {})
    execution   = alpha_health.get("execution_paused", False)
    pending     = alpha_health.get("pending", 0)
    l11         = alpha_health.get("layer11", {})
    
    # Pending queue check
    if pending > 50:
        results["pending_queue"] = {
            "status": "CRITICAL",
            "detail": f"pending={pending} (>50 — possible explosion)"
        }
    elif pending > 25:
        results["pending_queue"] = {
            "status": "WARNING",
            "detail": f"pending={pending} elevated"
        }
    else:
        results["pending_queue"] = {"status": "OK", "detail": f"pending={pending}"}
    
    # Execution gate
    results["execution_gate"] = {
        "status": "PAUSED" if execution else "OK",
        "detail": f"execution_paused={execution}"
    }
    
    # Scanner loop
    results["scanner_loop"] = {
        "status": "OK" if scanner.get("loop_active") else "DEAD",
        "detail": f"loop_active={scanner.get('loop_active')} cycles={scanner.get('total_cycles',0)}"
    }
    
    # Layer 11 AGENT_SILENCE
    l11_failures = l11.get("active_failures", [])
    if "AGENT_SILENCE" in l11_failures:
        results["agent_silence"] = {
            "status": "WARNING",
            "detail": "Layer 11 detecting agent silence — check agent submissions"
        }
    else:
        results["agent_silence"] = {"status": "OK", "detail": "agents submitting normally"}
    
    return results


def check_dashboard(alpha_url: str, nexus_secret: str) -> Optional[dict]:
    """Call Alpha /dashboard for unified resilience status."""
    try:
        r = requests.get(
            f"{alpha_url}/dashboard",
            headers={"X-Nexus-Secret": nexus_secret},
            timeout=15,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None



def _get_resilience_summary() -> dict:
    """Quick resilience status from last Alpha health fetch."""
    try:
        alpha_url = os.getenv("ALPHA_URL", "https://worker-production-2060.up.railway.app")
        r = requests.get(f"{alpha_url}/health", timeout=8)
        if r.status_code == 200:
            h = r.json()
            return {
                "pending":          h.get("pending", 0),
                "execution_paused": h.get("execution_paused", False),
                "loop_active":      h.get("omni_scanner", {}).get("loop_active", False),
                "go_today":         h.get("omni_scanner", {}).get("go_count", 0),
                "layer11":          h.get("layer11", {}).get("state", "unknown"),
            }
    except Exception:
        pass
    return {}

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)


