from flask import Flask, request, jsonify
import os
import json
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone

app = Flask(__name__)

BAN_API_KEY = os.getenv("BAN_API_KEY")
ban_records = {}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def check_auth(req):
    auth = req.headers.get("Authorization")
    return auth == f"Bearer {BAN_API_KEY}"


def parse_expiry(record):
    duration = str(record.get("duration", "")).lower()
    created_at = record.get("created_at")

    if duration in {"perm", "permanent"}:
        return None

    if not created_at:
        return None

    try:
        created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except Exception:
        return None

    seconds_map = {
        "1h": 3600,
        "12h": 43200,
        "1d": 86400,
        "7d": 604800,
        "30d": 2592000,
    }

    seconds = seconds_map.get(duration)
    if not seconds:
        return None

    return created_dt.timestamp() + seconds


def is_record_active(record):
    status = str(record.get("status", "")).lower()

    if status in {"denied", "unbanned", "remove_pending"}:
        return False

    if status not in {"approved", "completed", "edit_pending"}:
        return False

    duration = str(record.get("duration", "")).lower()
    if duration in {"perm", "permanent"}:
        return True

    expiry_ts = parse_expiry(record)
    if expiry_ts is None:
        return False

    return datetime.now(timezone.utc).timestamp() < expiry_ts


def record_with_ban_id(ban_id, record):
    item = dict(record)
    item["ban_id"] = ban_id
    item["active"] = is_record_active(record)
    return item


def normalize_lookup_value(value):
    return str(value or "").strip().lower()


def generate_manual_ban_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"STAFF-{stamp}"


def apply_record_defaults(data, ban_id, source="bot"):
    data["ban_id"] = ban_id
    data["status"] = data.get("status", "approved")
    data["source"] = data.get("source", source)
    data["request_type"] = data.get("request_type", "manual_staff_ban" if source == "manual_staff" else data.get("request_type", "unknown"))
    data["platform"] = str(data.get("platform", "roblox")).lower()
    data["duration"] = data.get("duration", "perm")
    data["created_at"] = data.get("created_at", now_iso())
    data["updated_at"] = now_iso()
    data["processed_by_game"] = data.get("processed_by_game", False)
    data["processed_at"] = data.get("processed_at")
    data["game_success"] = data.get("game_success")
    data["game_message"] = data.get("game_message")
    data["game_place_id"] = data.get("game_place_id")
    data["game_job_id"] = data.get("game_job_id")
    data["roblox_enforced"] = data.get("roblox_enforced", False)
    data["roblox_last_action"] = data.get("roblox_last_action", "manual_staff_ban" if source == "manual_staff" else data.get("roblox_last_action"))

    # Roblox alt-account enforcement:
    # BanAsync/Open Cloud uses ExcludeAltAccounts/excludeAltAccounts.
    # False means DO NOT exclude alts, so alt accounts are included in the ban.
    data["exclude_alt_accounts"] = data.get("exclude_alt_accounts", data.get("excludeAltAccounts", False))
    data["excludeAltAccounts"] = data.get("excludeAltAccounts", data.get("exclude_alt_accounts", False))
    data["ban_alt_accounts"] = data.get("ban_alt_accounts", True)

    return data


def find_records_by_user_id(user_id, active_only=False):
    results = []
    wanted = str(user_id).strip()

    for ban_id, record in ban_records.items():
        if str(record.get("platform", "")).lower() != "roblox":
            continue

        if str(record.get("target_user_id", "")).strip() != wanted:
            continue

        if active_only and not is_record_active(record):
            continue

        results.append(record_with_ban_id(ban_id, record))

    results.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return results


def find_records_by_username(username, active_only=False):
    results = []
    wanted = normalize_lookup_value(username)

    for ban_id, record in ban_records.items():
        if str(record.get("platform", "")).lower() != "roblox":
            continue

        target_name = normalize_lookup_value(record.get("target_name"))
        if target_name != wanted:
            continue

        if active_only and not is_record_active(record):
            continue

        results.append(record_with_ban_id(ban_id, record))

    results.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return results


def first_record_or_404(records):
    if not records:
        return None
    return records[0]


# Roblox Open Cloud DataStore settings.
# These let Discord /history read the permanent in-game DataStore history instead
# of relying on Railway/API RAM.
ROBLOX_OPEN_CLOUD_API_KEY = os.getenv("ROBLOX_OPEN_CLOUD_API_KEY", "").strip()
ROBLOX_UNIVERSE_ID = os.getenv("ROBLOX_UNIVERSE_ID", "").strip()
ROBLOX_PLACE_ID = os.getenv("ROBLOX_PLACE_ID", "").strip()
ROBLOX_HISTORY_DATASTORE_NAME = os.getenv("ROBLOX_HISTORY_DATASTORE_NAME", "SOM_BanHistory_v1").strip()
ROBLOX_HISTORY_DATASTORE_SCOPE = os.getenv("ROBLOX_HISTORY_DATASTORE_SCOPE", "global").strip()


def roblox_history_key(user_id):
    return f"User_{int(user_id)}"


def roblox_open_cloud_headers():
    return {
        "x-api-key": ROBLOX_OPEN_CLOUD_API_KEY,
        "Content-Type": "application/json",
    }


def require_roblox_open_cloud_config():
    missing = []
    if not ROBLOX_OPEN_CLOUD_API_KEY:
        missing.append("ROBLOX_OPEN_CLOUD_API_KEY")
    if not ROBLOX_UNIVERSE_ID:
        missing.append("ROBLOX_UNIVERSE_ID")
    if not ROBLOX_HISTORY_DATASTORE_NAME:
        missing.append("ROBLOX_HISTORY_DATASTORE_NAME")
    if missing:
        return "Missing environment variable(s): " + ", ".join(missing)
    return None


def http_get_json_with_headers(url, headers):
    req = Request(url, method="GET", headers=headers)
    try:
        with urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            if not body:
                return {}
            return json.loads(body)
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Roblox Open Cloud HTTP {e.code}: {body}")
    except URLError as e:
        raise RuntimeError(f"Roblox Open Cloud request failed: {e}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Roblox Open Cloud returned invalid JSON: {e}")


def parse_roblox_datastore_value(raw):
    """Normalize Open Cloud DataStore get-entry responses.

    The in-game worker stores a Lua table like:
    { user_id = 123, target_name = "Name", records = [...] }

    Open Cloud can return that value directly as an object, or wrapped in a
    response field such as value/content depending on endpoint/version.
    """
    if raw is None:
        return None

    if isinstance(raw, dict):
        # v2 commonly wraps the stored value in a value field.
        for key in ("value", "content", "data"):
            if key in raw:
                value = raw.get(key)
                if isinstance(value, str):
                    try:
                        return json.loads(value)
                    except Exception:
                        return value
                return value

        # v1 may return the entry value directly.
        if "records" in raw or "user_id" in raw:
            return raw

    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return raw

    return raw


def get_roblox_datastore_history(user_id):
    """Read permanent ban history from Roblox DataStore through Open Cloud."""
    config_error = require_roblox_open_cloud_config()
    if config_error:
        raise RuntimeError(config_error)

    entry_key = roblox_history_key(user_id)
    universe = quote(str(ROBLOX_UNIVERSE_ID), safe="")
    store = quote(ROBLOX_HISTORY_DATASTORE_NAME, safe="")
    scope = quote(ROBLOX_HISTORY_DATASTORE_SCOPE or "global", safe="")
    key = quote(entry_key, safe="")

    headers = roblox_open_cloud_headers()

    # Preferred current Open Cloud v2 endpoint.
    v2_url = (
        f"https://apis.roblox.com/cloud/v2/universes/{universe}"
        f"/data-stores/{store}/scopes/{scope}/entries/{key}"
    )

    # Compatibility fallback for older DataStores v1 endpoint.
    v1_url = (
        f"https://apis.roblox.com/datastores/v1/universes/{universe}"
        f"/standard-datastores/datastore/entries/entry"
        f"?datastoreName={store}&entryKey={key}&scope={scope}"
    )

    last_error = None
    for url in (v2_url, v1_url):
        try:
            raw = http_get_json_with_headers(url, headers)
            parsed = parse_roblox_datastore_value(raw)
            if isinstance(parsed, dict):
                return parsed
            return {"user_id": int(user_id), "records": []}
        except Exception as e:
            last_error = str(e)
            # If the key is missing, treat it as no history instead of a hard failure.
            if "404" in last_error or "NOT_FOUND" in last_error.upper() or "not found" in last_error.lower():
                return {"user_id": int(user_id), "records": []}

    raise RuntimeError(last_error or "Unable to read Roblox DataStore history")


def normalize_history_records_from_datastore(user_id, data):
    if not isinstance(data, dict):
        return []

    records = data.get("records")
    if not isinstance(records, list):
        records = []

    normalized = []
    for item in records:
        if not isinstance(item, dict):
            continue
        record = dict(item)
        record["target_user_id"] = record.get("target_user_id") or data.get("user_id") or int(user_id)
        record["target_name"] = record.get("target_name") or data.get("target_name") or "Unknown"
        record["active"] = False
        normalized.append(record)

    normalized.sort(key=lambda r: r.get("processed_at") or r.get("created_at") or "", reverse=True)
    return normalized


def _restriction_matches_user(user_id, item):
    if not isinstance(item, dict):
        return False

    wanted = str(int(user_id))
    possible_values = [
        item.get("user"),
        item.get("userId"),
        item.get("user_id"),
        item.get("targetUserId"),
        item.get("target_user_id"),
        item.get("name"),
        item.get("path"),
        item.get("id"),
    ]

    restriction = item.get("gameJoinRestriction") or item.get("restriction") or item.get("userRestriction")
    if isinstance(restriction, dict):
        possible_values.extend([
            restriction.get("user"),
            restriction.get("userId"),
            restriction.get("user_id"),
            restriction.get("targetUserId"),
            restriction.get("target_user_id"),
        ])

    for value in possible_values:
        if value is None:
            continue
        text = str(value)
        if text == wanted or text.endswith("/" + wanted) or f"users/{wanted}" in text:
            return True

    return False


def _extract_restriction_items(raw):
    if not isinstance(raw, dict):
        return []

    for key in ("userRestrictions", "user_restrictions", "restrictions", "items", "results", "data"):
        value = raw.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    return []


def _list_user_restrictions(url_base, user_id):
    """List restrictions and find a user when direct GET fails.

    Roblox's docs expose both Get and List User Restrictions. Some dashboards
    and API responses are easier to find through the list endpoint, so this
    paginates a small amount and matches the user id client-side.
    """
    headers = roblox_open_cloud_headers()
    page_token = ""
    last_error = None

    for _ in range(10):
        separator = "&" if "?" in url_base else "?"
        url = f"{url_base}{separator}maxPageSize=100"
        if page_token:
            url += f"&pageToken={quote(page_token, safe='')}"

        try:
            raw = http_get_json_with_headers(url, headers)
        except Exception as e:
            last_error = str(e)
            break

        for item in _extract_restriction_items(raw):
            if _restriction_matches_user(user_id, item):
                return item

        page_token = raw.get("nextPageToken") or raw.get("next_page_token") or ""
        if not page_token:
            break

    if last_error:
        raise RuntimeError(last_error)

    return None


def roblox_get_user_restriction(user_id):
    """Return one active/manual Roblox ban from Open Cloud, if present.

    Checks universe-level restrictions first, then optional place-level
    restrictions when ROBLOX_PLACE_ID is configured, and finally falls back to
    List User Restrictions to find dashboard/manual bans.
    """
    config_error = require_roblox_open_cloud_config()
    if config_error:
        raise RuntimeError(config_error)

    universe = quote(str(ROBLOX_UNIVERSE_ID), safe="")
    restriction_id = quote(str(int(user_id)), safe="")
    headers = roblox_open_cloud_headers()

    bases = [
        f"https://apis.roblox.com/cloud/v2/universes/{universe}/user-restrictions",
    ]

    if ROBLOX_PLACE_ID:
        place = quote(str(ROBLOX_PLACE_ID), safe="")
        bases.append(
            f"https://apis.roblox.com/cloud/v2/universes/{universe}/places/{place}/user-restrictions"
        )

    errors = []
    for base in bases:
        direct_url = f"{base}/{restriction_id}"
        try:
            raw = http_get_json_with_headers(direct_url, headers)
            if raw:
                return raw
        except Exception as e:
            text = str(e)
            if "404" not in text and "NOT_FOUND" not in text.upper() and "not found" not in text.lower():
                errors.append(text)

        try:
            raw = _list_user_restrictions(base, user_id)
            if raw:
                return raw
        except Exception as e:
            errors.append(str(e))

    if errors:
        raise RuntimeError(" ; ".join(errors))

    return None


def http_request_json_with_headers(method, url, headers, payload=None):
    body = None
    req_headers = dict(headers)

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = Request(url, data=body, method=method, headers=req_headers)

    try:
        with urlopen(req, timeout=15) as resp:
            response_body = resp.read().decode("utf-8")
            if not response_body:
                return {}
            return json.loads(response_body)
    except HTTPError as e:
        response_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Roblox Open Cloud HTTP {e.code}: {response_body}")
    except URLError as e:
        raise RuntimeError(f"Roblox Open Cloud request failed: {e}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Roblox Open Cloud returned invalid JSON: {e}")


def is_roblox_rate_limited(error_text):
    text = str(error_text)
    return "HTTP 429" in text or "RESOURCE_EXHAUSTED" in text or "too many requests" in text.lower()


def roblox_unban_user_restriction(user_id):
    """Remove an active Roblox Ban API / Creator Hub user restriction.

    Roblox's Update User Restriction endpoint expects the whole
    gameJoinRestriction object to be patched atomically. Do not spam several
    fallback payload shapes; that causes 429 rate limits.
    """
    config_error = require_roblox_open_cloud_config()
    if config_error:
        raise RuntimeError(config_error)

    universe = quote(str(ROBLOX_UNIVERSE_ID), safe="")
    restriction_id = quote(str(int(user_id)), safe="")
    headers = roblox_open_cloud_headers()

    bases = [
        f"https://apis.roblox.com/cloud/v2/universes/{universe}/user-restrictions/{restriction_id}",
    ]

    if ROBLOX_PLACE_ID:
        place = quote(str(ROBLOX_PLACE_ID), safe="")
        bases.append(
            f"https://apis.roblox.com/cloud/v2/universes/{universe}/places/{place}/user-restrictions/{restriction_id}"
        )

    # Full object required. Field masks into gameJoinRestriction.active are not
    # supported by Roblox; patch the whole game_join_restriction field.
    payload = {
        "gameJoinRestriction": {
            "active": False,
            "privateReason": "Unbanned from Discord moderation panel",
            "displayReason": "Unbanned",
            "excludeAltAccounts": False,
        }
    }

    errors = []
    for base in bases:
        url = f"{base}?updateMask=game_join_restriction"
        try:
            return http_request_json_with_headers("PATCH", url, headers, payload)
        except Exception as e:
            text = str(e)

            # 429 means Roblox accepted too many recent restriction requests for
            # this same user. Stop immediately instead of trying more endpoints.
            if is_roblox_rate_limited(text):
                raise RuntimeError(
                    "Roblox Open Cloud rate-limited this user restriction request. "
                    "Wait 1-2 minutes, then click Remove Ban again. "
                    f"Details: {text}"
                )

            # If the universe-level restriction does not exist, try optional
            # place-level restriction next.
            if "404" in text or "NOT_FOUND" in text.upper() or "not found" in text.lower():
                errors.append(text)
                continue

            # 400/permission/etc. are real errors; do not keep spamming.
            raise RuntimeError(text)

    raise RuntimeError("No matching Roblox user restriction was found to remove. " + (" ; ".join(errors) if errors else ""))


def _pick_first(*values):
    for value in values:
        if value is not None and str(value).strip() != "":
            return value
    return None


def normalize_open_cloud_restriction(user_id, raw):
    """Convert Roblox Open Cloud user restriction response into bot ban format."""
    if not isinstance(raw, dict) or not raw:
        return None

    # GET can return the restriction directly, nested under gameJoinRestriction,
    # or as an item from List User Restrictions.
    restriction = (
        raw.get("gameJoinRestriction")
        or raw.get("restriction")
        or raw.get("userRestriction")
        or raw.get("user_restriction")
        or raw
    )
    if not isinstance(restriction, dict):
        restriction = raw

    active = _pick_first(
        restriction.get("active"),
        raw.get("active"),
        raw.get("isActive"),
        raw.get("currentlyActive"),
    )

    if isinstance(active, str):
        text_active = active.strip().lower()
        if text_active in {"false", "0", "no", "inactive", "expired", "unbanned"}:
            active = False
        elif text_active in {"true", "1", "yes", "active", "banned"}:
            active = True
        else:
            active = None

    # Roblox Open Cloud/List User Restrictions can return an existing active
    # restriction without a clean boolean active field. If we found the user's
    # restriction object at all, treat it as active unless it explicitly says
    # inactive/expired/unbanned.
    if active is None:
        status_text = str(_pick_first(raw.get("status"), restriction.get("status"), raw.get("state"), restriction.get("state"), "")).strip().lower()
        if status_text in {"inactive", "expired", "unbanned", "removed", "revoked"}:
            active = False
        else:
            active = True

    display_reason = _pick_first(
        restriction.get("displayReason"),
        restriction.get("display_reason"),
        raw.get("displayReason"),
        raw.get("display_reason"),
    )
    private_reason = _pick_first(
        restriction.get("privateReason"),
        restriction.get("private_reason"),
        raw.get("privateReason"),
        raw.get("private_reason"),
    )

    duration = _pick_first(
        restriction.get("duration"),
        restriction.get("durationSeconds"),
        restriction.get("duration_seconds"),
        raw.get("duration"),
        raw.get("durationSeconds"),
        raw.get("duration_seconds"),
        "Unknown",
    )

    created_at = _pick_first(
        raw.get("createTime"),
        raw.get("createdTime"),
        raw.get("created_at"),
        raw.get("create_time"),
        restriction.get("createTime"),
        restriction.get("createdTime"),
        raw.get("updateTime"),
        raw.get("updated_at"),
        now_iso(),
    )

    updated_at = _pick_first(
        raw.get("updateTime"),
        raw.get("updatedTime"),
        raw.get("updated_at"),
        raw.get("update_time"),
        restriction.get("updateTime"),
        restriction.get("updatedTime"),
        created_at,
    )

    status = "completed" if active is True else "unbanned"

    ban_id = _pick_first(
        raw.get("ban_id"),
        raw.get("id"),
        raw.get("name"),
        raw.get("path"),
        f"ROBLOX-{int(user_id)}",
    )

    return {
        "ban_id": str(ban_id),
        "status": status,
        "request_type": "roblox_manual_or_dashboard_ban",
        "platform": "roblox",
        "target_name": raw.get("target_name") or raw.get("username") or "Roblox User",
        "target_user_id": int(user_id),
        "duration": duration,
        "reason": private_reason or display_reason or "No reason provided.",
        "proof": None,
        "offender_info": None,
        "request_url": None,
        "adonis_command": "Roblox Creator Hub / Open Cloud ban",
        "approved_by_name": _pick_first(raw.get("moderator"), raw.get("actor"), raw.get("createdBy"), "Roblox / Manual Ban"),
        "created_at": created_at,
        "updated_at": updated_at,
        "active": active is True,
        "roblox_enforced": True,
        "source": "roblox_open_cloud",
        "exclude_alt_accounts": False,
        "excludeAltAccounts": False,
        "ban_alt_accounts": True,
        "raw_open_cloud": raw,
    }


def lookup_open_cloud_ban_record_by_user_id(user_id):
    raw = roblox_get_user_restriction(user_id)
    if not raw:
        return None
    return normalize_open_cloud_restriction(user_id, raw)


@app.route("/", methods=["GET"])
def home():
    return "Ban API Running", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "records": len(ban_records),
        "open_cloud_configured": not bool(require_roblox_open_cloud_config()),
        "universe_id_configured": bool(ROBLOX_UNIVERSE_ID),
        "place_id_configured": bool(ROBLOX_PLACE_ID),
        "time": now_iso()
    }), 200


@app.route("/ban-status/<int:user_id>", methods=["GET"])
def ban_status(user_id):
    active_record = None

    for ban_id, record in ban_records.items():
        try:
            target_user_id = int(record.get("target_user_id", 0))
        except Exception:
            target_user_id = 0

        if target_user_id != user_id:
            continue

        if is_record_active(record):
            item = dict(record)
            item["ban_id"] = ban_id
            active_record = item

    if not active_record:
        return jsonify({
            "active": False,
            "user_id": user_id
        }), 200

    return jsonify({
        "active": True,
        "user_id": user_id,
        "ban_id": active_record.get("ban_id"),
        "duration": active_record.get("duration"),
        "status": active_record.get("status"),
        "reason": active_record.get("reason"),
        "display_reason": f"Automated Ban - {active_record.get('ban_id')}",
        "target_name": active_record.get("target_name"),
        "apply_to_universe": True,
        "exclude_alt_accounts": active_record.get("exclude_alt_accounts", False),
        "excludeAltAccounts": active_record.get("excludeAltAccounts", False),
        "ban_alt_accounts": active_record.get("ban_alt_accounts", True),
    }), 200


@app.route("/active-bans", methods=["GET"])
def active_bans():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    results = []
    for ban_id, record in ban_records.items():
        if str(record.get("platform", "")).lower() != "roblox":
            continue

        if is_record_active(record):
            item = dict(record)
            item["ban_id"] = ban_id
            results.append(item)

    results.sort(key=lambda r: r.get("created_at", ""))
    return jsonify({
        "count": len(results),
        "records": results
    }), 200


@app.route("/ban-records/source/<source>", methods=["GET"])
def records_by_source(source):
    """List records by source, for example source=bot or source=manual_staff."""
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    wanted = normalize_lookup_value(source)
    active_only = str(request.args.get("active_only", "false")).lower() in {"1", "true", "yes"}

    results = []
    for ban_id, record in ban_records.items():
        if normalize_lookup_value(record.get("source", "bot")) != wanted:
            continue
        if active_only and not is_record_active(record):
            continue
        results.append(record_with_ban_id(ban_id, record))

    results.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return jsonify({"count": len(results), "records": results}), 200


@app.route("/ban-records/request", methods=["POST"])
def create_request():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    data = request.json or {}
    ban_id = data.get("ban_id")
    if not ban_id:
        return {"error": "ban_id is required"}, 400

    data["status"] = data.get("status", "pending")
    data = apply_record_defaults(data, ban_id, source="bot")

    ban_records[ban_id] = data
    return {"success": True, "ban_id": ban_id, "status": data["status"]}, 200


@app.route("/ban-records/execute", methods=["POST"])
def execute_ban():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    data = request.json or {}
    ban_id = data.get("ban_id")
    action = data.get("action")

    if not ban_id:
        return {"error": "ban_id is required"}, 400

    record = ban_records.get(ban_id)
    if not record:
        record = {
            "ban_id": ban_id,
            "created_at": data.get("created_at", now_iso()),
        }

    record.update(data)
    record["ban_id"] = ban_id
    record["updated_at"] = now_iso()
    record["exclude_alt_accounts"] = data.get("exclude_alt_accounts", data.get("excludeAltAccounts", False))
    record["excludeAltAccounts"] = data.get("excludeAltAccounts", data.get("exclude_alt_accounts", False))
    record["ban_alt_accounts"] = data.get("ban_alt_accounts", True)
    record["processed_by_game"] = False
    record["processed_at"] = None
    record["game_success"] = None
    record["game_message"] = None
    record["game_place_id"] = None
    record["game_job_id"] = None

    if action == "approve":
        record["status"] = "approved"
        record["roblox_last_action"] = "approve"
    elif action == "deny":
        record["status"] = "denied"
        record["roblox_last_action"] = "deny"
    elif record.get("status") is None:
        record["status"] = "approved"
        record["roblox_last_action"] = "approve"

    ban_records[ban_id] = record
    return {"success": True, "ban_id": ban_id, "status": record["status"]}, 200


@app.route("/ban-records/manual", methods=["POST"])
def create_manual_staff_ban():
    """Store a regular staff-issued ban so bot/API lookups can find it too.

    Use this when a staff member bans someone outside the SOM bot flow.
    Required: target_user_id or target_name.
    Optional: ban_id, target_name, duration, reason, proof, staff_discord_id, staff_name.
    If ban_id is not provided, the API creates a STAFF-* ID.
    """
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    data = request.json or {}
    if not data.get("target_user_id") and not data.get("target_name"):
        return {"error": "target_user_id or target_name is required"}, 400

    ban_id = data.get("ban_id") or generate_manual_ban_id()

    data["status"] = data.get("status", "approved")
    data["request_type"] = data.get("request_type", "manual_staff_ban")
    data["source"] = data.get("source", "manual_staff")
    data["approved_by_discord_id"] = data.get("approved_by_discord_id") or data.get("staff_discord_id")
    data["approved_by_name"] = data.get("approved_by_name") or data.get("staff_name")
    data["adonis_command"] = data.get("adonis_command", "Manual staff ban")
    data["exclude_alt_accounts"] = data.get("exclude_alt_accounts", data.get("excludeAltAccounts", False))
    data["excludeAltAccounts"] = data.get("excludeAltAccounts", data.get("exclude_alt_accounts", False))
    data["ban_alt_accounts"] = data.get("ban_alt_accounts", True)
    data = apply_record_defaults(data, ban_id, source="manual_staff")

    ban_records[ban_id] = data
    return jsonify({
        "success": True,
        "ban_id": ban_id,
        "status": data["status"],
        "source": data["source"],
    }), 200


@app.route("/ban-records/staff", methods=["POST"])
def create_manual_staff_ban_alias():
    """Alias for /ban-records/manual."""
    return create_manual_staff_ban()


@app.route("/ban-records/roblox/<int:user_id>", methods=["GET"])
def get_ban_by_roblox_user_id(user_id):
    """Return the newest Roblox ban record for a Roblox user ID.

    This is the route the Discord bot tries first for username appeal lookup
    after resolving a Roblox username into a Roblox numeric user ID.
    """
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    active_only = str(request.args.get("active_only", "false")).lower() in {"1", "true", "yes"}
    records = find_records_by_user_id(user_id, active_only=active_only)
    record = first_record_or_404(records)

    if not record:
        try:
            record = lookup_open_cloud_ban_record_by_user_id(user_id)
        except Exception as e:
            return jsonify({
                "error": "Failed to check Roblox Open Cloud User Restrictions",
                "details": str(e),
                "hint": "Check ROBLOX_OPEN_CLOUD_API_KEY, ROBLOX_UNIVERSE_ID, optional ROBLOX_PLACE_ID, and make sure the key has user-restrictions read access for this universe.",
                "records": []
            }), 502

    if not record:
        return {"error": "Not found", "records": []}, 404

    return jsonify(record), 200


@app.route("/ban-records/user/<int:user_id>", methods=["GET"])
def get_ban_by_user_id_alias(user_id):
    """Alias for /ban-records/roblox/<user_id> for bot compatibility."""
    return get_ban_by_roblox_user_id(user_id)


@app.route("/ban-records/search", methods=["GET"])
def search_ban_records():
    """Search ban records by Roblox target_user_id or target_name.

    Supported query params:
    - platform=roblox
    - target_user_id=<roblox user id>
    - target_name=<roblox username>
    - active_only=true/false
    """
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    platform = normalize_lookup_value(request.args.get("platform", "roblox"))
    if platform and platform != "roblox":
        return jsonify({"count": 0, "records": []}), 200

    active_only = str(request.args.get("active_only", "false")).lower() in {"1", "true", "yes"}
    target_user_id = request.args.get("target_user_id")
    target_name = request.args.get("target_name")

    if target_user_id:
        records = find_records_by_user_id(target_user_id, active_only=active_only)
        if not records:
            try:
                oc_record = lookup_open_cloud_ban_record_by_user_id(target_user_id)
                if oc_record and (not active_only or oc_record.get("active")):
                    records = [oc_record]
            except Exception as e:
                return jsonify({
                    "count": 0,
                    "records": [],
                    "open_cloud_error": str(e),
                    "hint": "Check ROBLOX_OPEN_CLOUD_API_KEY, ROBLOX_UNIVERSE_ID, optional ROBLOX_PLACE_ID, and user-restrictions read access."
                }), 502
    elif target_name:
        records = find_records_by_username(target_name, active_only=active_only)
    else:
        return {"error": "target_user_id or target_name is required"}, 400

    return jsonify({
        "count": len(records),
        "records": records
    }), 200


@app.route("/debug/roblox-restriction/<int:user_id>", methods=["GET"])
def debug_roblox_restriction(user_id):
    """Debug raw Roblox Open Cloud restriction lookup for one user."""
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    try:
        raw = roblox_get_user_restriction(user_id)
        normalized = normalize_open_cloud_restriction(user_id, raw) if raw else None
        return jsonify({
            "user_id": user_id,
            "found": raw is not None,
            "raw": raw,
            "normalized": normalized,
        }), 200
    except Exception as e:
        return jsonify({
            "user_id": user_id,
            "error": str(e),
            "hint": "Check Open Cloud key permissions and whether the ban is universe-level or place-level.",
        }), 502


@app.route("/ban-records/active/roblox/<int:user_id>", methods=["GET"])
@app.route("/ban-records/current/roblox/<int:user_id>", methods=["GET"])
@app.route("/ban-records/active/<int:user_id>", methods=["GET"])
@app.route("/ban-records/current/<int:user_id>", methods=["GET"])
@app.route("/ban-records/restriction/<int:user_id>", methods=["GET"])
@app.route("/ban-records/restrictions/<int:user_id>", methods=["GET"])
@app.route("/ban-records/roblox/<int:user_id>/active", methods=["GET"])
@app.route("/ban-records/roblox/<int:user_id>/current", methods=["GET"])
@app.route("/ban-records/user/<int:user_id>/active", methods=["GET"])
@app.route("/ban-records/user/<int:user_id>/current", methods=["GET"])
def get_active_ban_aliases(user_id):
    """Aliases for Discord bot active/current ban lookup."""
    return get_ban_by_roblox_user_id(user_id)


@app.route("/ban-records/by-name/<username>", methods=["GET"])
def get_ban_by_username(username):
    """Return the newest Roblox ban record for an exact Roblox username."""
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    active_only = str(request.args.get("active_only", "false")).lower() in {"1", "true", "yes"}
    records = find_records_by_username(username, active_only=active_only)
    record = first_record_or_404(records)

    if not record:
        return {"error": "Not found", "records": []}, 404

    return jsonify(record), 200


@app.route("/ban-records/history/roblox/<int:user_id>", methods=["GET"])
def get_ban_history_from_roblox_datastore(user_id):
    """Return permanent ban history saved by the Roblox in-game DataStore worker."""
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    try:
        data = get_roblox_datastore_history(user_id)
        records = normalize_history_records_from_datastore(user_id, data)
    except Exception as e:
        return jsonify({
            "error": "Failed to read Roblox DataStore history",
            "details": str(e),
            "records": []
        }), 500

    return jsonify({
        "count": len(records),
        "user_id": user_id,
        "target_name": data.get("target_name") if isinstance(data, dict) else None,
        "source": "roblox_datastore",
        "datastore": ROBLOX_HISTORY_DATASTORE_NAME,
        "records": records,
    }), 200


@app.route("/ban-records/history/<int:user_id>", methods=["GET"])
def get_ban_history_alias(user_id):
    """Alias used by the Discord bot."""
    return get_ban_history_from_roblox_datastore(user_id)


@app.route("/ban-records/roblox/<int:user_id>/history", methods=["GET"])
def get_ban_history_roblox_suffix_alias(user_id):
    """Alias used by the Discord bot."""
    return get_ban_history_from_roblox_datastore(user_id)


@app.route("/ban-records/user/<int:user_id>/history", methods=["GET"])
def get_ban_history_user_suffix_alias(user_id):
    """Alias used by the Discord bot."""
    return get_ban_history_from_roblox_datastore(user_id)


@app.route("/ban-records/<ban_id>", methods=["GET"])
def get_ban(ban_id):
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    data = ban_records.get(ban_id)
    if not data:
        return {"error": "Not found"}, 404

    result = dict(data)
    result["ban_id"] = ban_id
    return jsonify(result), 200


@app.route("/ban-records/update", methods=["POST"])
def update_ban():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    data = request.json or {}
    ban_id = data.get("ban_id")
    if not ban_id:
        return {"error": "ban_id is required"}, 400

    if ban_id not in ban_records:
        return {"error": "Not found"}, 404

    record = ban_records[ban_id]

    editable_fields = {
        "duration",
        "reason",
        "proof",
        "target_name",
        "target_user_id",
        "exclude_alt_accounts",
        "excludeAltAccounts",
        "ban_alt_accounts",
    }

    for key in editable_fields:
        if key in data:
            record[key] = data[key]

    record["ban_id"] = ban_id
    record["updated_at"] = now_iso()
    record["edited_by_discord_id"] = data.get("edited_by_discord_id")
    record["edited_by_name"] = data.get("edited_by_name")
    record["status"] = "edit_pending"
    record["processed_by_game"] = False
    record["processed_at"] = None
    record["game_success"] = None
    record["game_message"] = None
    record["game_place_id"] = None
    record["game_job_id"] = None
    record["roblox_last_action"] = "edit"

    ban_records[ban_id] = record
    return {"success": True, "ban_id": ban_id, "status": record["status"]}, 200


@app.route("/ban-records/remove", methods=["POST"])
def remove_ban():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    data = request.json or {}
    ban_id = data.get("ban_id")
    target_user_id = data.get("target_user_id") or data.get("user_id") or data.get("roblox_user_id")

    if not ban_id and not target_user_id:
        return {"error": "ban_id or target_user_id is required"}, 400

    # Normal bot-created bans: mark the existing record for the in-game worker.
    if ban_id and ban_id in ban_records:
        record = ban_records[ban_id]
        record["ban_id"] = ban_id
        record["status"] = "remove_pending"
        record["processed_by_game"] = False
        record["processed_at"] = None
        record["game_success"] = None
        record["game_message"] = None
        record["game_place_id"] = None
        record["game_job_id"] = None
        record["removed_by_discord_id"] = data.get("removed_by_discord_id")
        record["removed_by_name"] = data.get("removed_by_name")
        record["updated_at"] = now_iso()
        record["roblox_last_action"] = "remove"

        ban_records[ban_id] = record
        return {"success": True, "ban_id": ban_id, "status": record["status"]}, 200

    # Manual Creator Hub / Open Cloud bans do not exist in this API's RAM.
    # Remove them directly by Roblox user ID.
    if target_user_id:
        try:
            roblox_unban_user_restriction(int(target_user_id))
        except Exception as e:
            details = str(e)
            hint = (
                "Roblox rate-limited this user restriction. Wait 1-2 minutes and try again."
                if is_roblox_rate_limited(details)
                else "Roblox rejected the unban PATCH. Make sure this deployed API version does not send duration=0 and that the key has user-restrictions write access."
                if "Invalid Duration value" in details
                else "Check ROBLOX_OPEN_CLOUD_API_KEY, ROBLOX_UNIVERSE_ID, optional ROBLOX_PLACE_ID, and user-restrictions write access."
            )
            return jsonify({
                "error": "Failed to remove Roblox Open Cloud user restriction",
                "details": details,
                "hint": hint
            }), 502

        manual_id = ban_id or f"ROBLOX-{int(target_user_id)}"
        ban_records[manual_id] = apply_record_defaults({
            "ban_id": manual_id,
            "status": "unbanned",
            "request_type": "manual_or_open_cloud_unban",
            "platform": "roblox",
            "target_user_id": str(target_user_id),
            "target_name": data.get("target_name"),
            "removed_by_discord_id": data.get("removed_by_discord_id"),
            "removed_by_name": data.get("removed_by_name"),
            "roblox_enforced": False,
            "roblox_last_action": "remove",
            "processed_by_game": True,
            "processed_at": now_iso(),
            "game_success": True,
            "game_message": "Removed directly through Roblox Open Cloud",
        }, manual_id, source="roblox_open_cloud")

        return jsonify({
            "success": True,
            "ban_id": manual_id,
            "status": "unbanned",
            "target_user_id": str(target_user_id),
            "source": "roblox_open_cloud",
        }), 200

    return {"error": "Not found"}, 404


@app.route("/ban-records/unban", methods=["POST"])
@app.route("/ban-records/delete", methods=["POST"])
@app.route("/ban-records/revoke", methods=["POST"])
def remove_ban_alias():
    return remove_ban()


@app.route("/ban-records/roblox/<int:user_id>/remove", methods=["POST"])
@app.route("/ban-records/roblox/<int:user_id>/unban", methods=["POST"])
@app.route("/ban-records/user/<int:user_id>/remove", methods=["POST"])
@app.route("/ban-records/user/<int:user_id>/unban", methods=["POST"])
def remove_ban_by_user_id_alias(user_id):
    data = request.json or {}
    data["target_user_id"] = str(user_id)

    # Patch request.json usage by temporarily replacing request cached json
    # is messy in Flask, so call the underlying logic through a copied payload.
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    try:
        roblox_unban_user_restriction(int(user_id))
    except Exception as e:
        return jsonify({
            "error": "Failed to remove Roblox Open Cloud user restriction",
            "details": str(e),
            "hint": "Check ROBLOX_OPEN_CLOUD_API_KEY, ROBLOX_UNIVERSE_ID, optional ROBLOX_PLACE_ID, and user-restrictions write access."
        }), 502

    manual_id = data.get("ban_id") or f"ROBLOX-{int(user_id)}"
    ban_records[manual_id] = apply_record_defaults({
        "ban_id": manual_id,
        "status": "unbanned",
        "request_type": "manual_or_open_cloud_unban",
        "platform": "roblox",
        "target_user_id": str(user_id),
        "target_name": data.get("target_name"),
        "removed_by_discord_id": data.get("removed_by_discord_id"),
        "removed_by_name": data.get("removed_by_name"),
        "roblox_enforced": False,
        "roblox_last_action": "remove",
        "processed_by_game": True,
        "processed_at": now_iso(),
        "game_success": True,
        "game_message": "Removed directly through Roblox Open Cloud",
    }, manual_id, source="roblox_open_cloud")

    return jsonify({
        "success": True,
        "ban_id": manual_id,
        "status": "unbanned",
        "target_user_id": str(user_id),
        "source": "roblox_open_cloud",
    }), 200


@app.route("/ban-records/game-pending", methods=["GET"])
def get_game_pending():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    results = []
    for ban_id, record in ban_records.items():
        if str(record.get("platform", "")).lower() != "roblox":
            continue

        if record.get("status") in {"approved", "edit_pending", "remove_pending"} and not record.get("processed_by_game", False):
            item = dict(record)
            item["ban_id"] = ban_id
            results.append(item)

    results.sort(key=lambda r: r.get("created_at", ""))
    return jsonify({"count": len(results), "records": results}), 200


@app.route("/ban-records/complete", methods=["POST"])
def complete_ban():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    data = request.json or {}
    ban_id = data.get("ban_id")
    success = data.get("success")

    if not ban_id:
        return {"error": "ban_id is required"}, 400

    if ban_id not in ban_records:
        return {"error": "Not found"}, 404

    record = ban_records[ban_id]
    action = data.get("action")

    record["ban_id"] = ban_id
    record["processed_by_game"] = True
    record["processed_at"] = now_iso()
    record["game_success"] = bool(success)
    record["game_message"] = data.get("message", "")
    record["game_place_id"] = data.get("place_id")
    record["game_job_id"] = data.get("job_id")
    record["updated_at"] = now_iso()
    record["roblox_enforced"] = bool(success)
    record["roblox_last_action"] = action

    if success:
        if action == "remove":
            record["status"] = "unbanned"
        elif action == "edit":
            record["status"] = "completed"
        else:
            record["status"] = "completed"
    else:
        record["status"] = "game_failed"

    ban_records[ban_id] = record
    return {"success": True, "ban_id": ban_id, "status": record["status"]}, 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
