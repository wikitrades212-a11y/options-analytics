"""
Two-phase Robinhood login.

Phase 1: Sends login → gets workflow ID → saves device token.
Phase 2: Reads saved device token → retries login → saves pickle.

Usage:
  python save_login.py           # normal flow (Phase 1 or Phase 2)
  python save_login.py --reset   # discard stale workflow and start fresh
"""
import time, sys, requests, pickle, os, json, argparse
from robin_stocks.robinhood.authentication import generate_device_token
from robin_stocks.robinhood.helper import update_session, set_login_state

LOGIN_URL   = "https://api.robinhood.com/oauth2/token/"
CLIENT_ID   = "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS"
STATE_FILE  = "/tmp/rh_login_state.json"
PICKLE_PATH = os.path.expanduser("~/.tokens/robinhood.pickle")

STALE_SECONDS = 300   # workflow older than 5 min is considered stale
POLL_INTERVAL = 10    # seconds between status checks
POLL_TIMEOUT  = 180   # stop polling after 3 minutes


# ── Helpers ───────────────────────────────────────────────────────────────────

def login_request(username, password, device_token, mfa_code=None):
    payload = {
        "client_id": CLIENT_ID, "expires_in": 86400,
        "grant_type": "password", "password": password,
        "scope": "internal", "username": username,
        "challenge_type": "sms", "device_token": device_token,
    }
    if mfa_code:
        payload["mfa_code"] = mfa_code
    return requests.post(LOGIN_URL, data=payload).json()


def respond_to_challenge(challenge_id, code):
    return requests.post(
        f"https://api.robinhood.com/challenge/{challenge_id}/respond/",
        data={"response": code}
    ).json()


def save_state(device_token, workflow_id):
    with open(STATE_FILE, "w") as f:
        json.dump({
            "device_token": device_token,
            "workflow_id":  workflow_id,
            "started_at":   time.time(),
        }, f)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None


def clear_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)


def save_pickle_and_exit(data, device_token):
    """Write pickle, update session, print success, exit."""
    os.makedirs(os.path.dirname(PICKLE_PATH), exist_ok=True)
    with open(PICKLE_PATH, "wb") as f:
        pickle.dump({
            "token_type":    data["token_type"],
            "access_token":  data["access_token"],
            "refresh_token": data["refresh_token"],
            "device_token":  device_token,
        }, f)
    update_session("Authorization", f"{data['token_type']} {data['access_token']}")
    set_login_state(True)
    clear_state()
    print(f"\n  Pickle written → {PICKLE_PATH}")
    print("✓ LOGIN SUCCESSFUL — backend is now authenticated!\n")
    sys.exit(0)


# ── Polling ───────────────────────────────────────────────────────────────────

def poll_for_approval(username, password, device_token):
    """Retry login_request every POLL_INTERVAL seconds until approved or timeout."""
    print(f"\nPolling every {POLL_INTERVAL}s (timeout {POLL_TIMEOUT}s) — approve in the Robinhood app now...\n")
    deadline = time.time() + POLL_TIMEOUT
    elapsed  = 0
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        data = login_request(username, password, device_token)

        if "access_token" in data:
            return data  # approved

        if "verification_workflow" in data:
            wid    = data["verification_workflow"]["id"]
            status = data["verification_workflow"]["workflow_status"]
            print(f"  [{elapsed:3d}s]  status={status}  id={wid[:8]}...")
        else:
            print(f"  [{elapsed:3d}s]  unexpected: {data}")

    return None  # timed out


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Robinhood login helper")
    parser.add_argument("--reset", "-r", action="store_true",
                        help="Discard stale state and start a fresh login challenge")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    username = os.getenv("RH_USERNAME", "")
    password = os.getenv("RH_PASSWORD", "")

    # ── Force reset ───────────────────────────────────────────────────────────
    if args.reset:
        clear_state()
        print("[reset] Stale state discarded — starting a fresh login challenge.\n")

    state = load_state()

    # ── Phase 2: saved state exists ───────────────────────────────────────────
    if state:
        device_token      = state["device_token"]
        saved_workflow_id = state.get("workflow_id")
        age               = time.time() - state.get("started_at", 0)

        print(f"Phase 2 — retrying with saved device token (state is {int(age)}s old)")
        data = login_request(username, password, device_token)

        if "access_token" in data:
            save_pickle_and_exit(data, device_token)

        elif "verification_workflow" in data:
            wid    = data["verification_workflow"]["id"]
            status = data["verification_workflow"]["workflow_status"]

            # Identify whether this is the same stale workflow or a new one
            if wid == saved_workflow_id:
                print(f"[reusing OLD workflow]  id={wid}  status={status}  age={int(age)}s")
                if age > STALE_SECONDS:
                    print(f"\nThis workflow has been pending for {int(age)}s and looks stale.")
                    print("Tip: run  python save_login.py --reset  to get a fresh challenge.")
                    print("     Or approve the existing request in the Robinhood app and poll below.\n")
            else:
                print(f"[NEW workflow]          id={wid}  status={status}")
                save_state(device_token, wid)

            # Either way — offer to poll
            choice = input("Poll here while you approve? [Y/n]: ").strip().lower()
            if choice in ("", "y", "yes"):
                result = poll_for_approval(username, password, device_token)
                if result:
                    save_pickle_and_exit(result, device_token)
                else:
                    print("\nTimed out — approval not received.")
                    print("If the workflow is stuck, run: python save_login.py --reset")
                    sys.exit(1)
            else:
                print("\nRun again after approving:  python save_login.py")
                print("If it stays stuck, run:     python save_login.py --reset")
                sys.exit(0)

        elif "challenge" in data:
            cid  = data["challenge"]["id"]
            code = input("Enter the Robinhood SMS/email code: ").strip()
            respond_to_challenge(cid, code)
            r = requests.post(LOGIN_URL, data={
                "client_id": CLIENT_ID, "expires_in": 86400,
                "grant_type": "password", "password": password,
                "scope": "internal", "username": username,
                "challenge_type": "sms", "device_token": device_token,
            }, headers={"X-ROBINHOOD-CHALLENGE-RESPONSE-ID": cid})
            data = r.json()
            if "access_token" in data:
                save_pickle_and_exit(data, device_token)
            else:
                print(f"Challenge failed: {data}")
                sys.exit(1)

        elif "mfa_required" in data:
            code = input("Enter MFA code: ").strip()
            data = login_request(username, password, device_token, mfa_code=code)
            if "access_token" in data:
                save_pickle_and_exit(data, device_token)
            else:
                print(f"MFA failed: {data}")
                sys.exit(1)

        else:
            print(f"Unexpected response: {data}")
            sys.exit(1)

    # ── Phase 1: no saved state → fresh login ────────────────────────────────
    else:
        device_token = generate_device_token()
        print(f"Phase 1 — starting fresh login as {username}...")
        data = login_request(username, password, device_token)

        if "access_token" in data:
            save_pickle_and_exit(data, device_token)

        elif "verification_workflow" in data:
            wid    = data["verification_workflow"]["id"]
            status = data["verification_workflow"]["workflow_status"]

            save_state(device_token, wid)
            print("[NEW workflow]")
            print("\n" + "═" * 60)
            print("  ACTION REQUIRED ON YOUR PHONE")
            print("═" * 60)
            print(f"  Workflow ID : {wid}")
            print(f"  Status      : {status}")
            print()
            print("  1. Open the Robinhood app")
            print("  2. Account → Security → Login Requests")
            print("  3. Approve the NEWEST pending entry")
            print()

            choice = input("Poll here while you approve? [Y/n]: ").strip().lower()
            if choice in ("", "y", "yes"):
                result = poll_for_approval(username, password, device_token)
                if result:
                    save_pickle_and_exit(result, device_token)
                else:
                    print("\nTimed out. After approving, run: python save_login.py")
                    sys.exit(1)
            else:
                print("  After approving, run:  python save_login.py")
                print("═" * 60)
                sys.exit(0)

        else:
            print(f"Unexpected response: {data}")
            sys.exit(1)


if __name__ == "__main__":
    main()
