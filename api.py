from flask import Flask, request, jsonify
import os
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



@app.route("/", methods=["GET"])
def home():
    return "Ban API Running", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "records": len(ban_records),
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
    elif target_name:
        records = find_records_by_username(target_name, active_only=active_only)
    else:
        return {"error": "target_user_id or target_name is required"}, 400

    return jsonify({
        "count": len(records),
        "records": records
    }), 200


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
    if not ban_id:
        return {"error": "ban_id is required"}, 400

    if ban_id not in ban_records:
        return {"error": "Not found"}, 404

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
