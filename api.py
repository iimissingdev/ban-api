from flask import Flask, request, jsonify
import os
from datetime import datetime, timezone

app = Flask(__name__)
print("API file loaded successfully")

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


@app.route("/", methods=["GET"])
def home():
    return "Ban API Running", 200


@app.route("/health", methods=["GET"])
def health():
    print("Health route hit")
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


@app.route("/ban-records/request", methods=["POST"])
def create_request():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    data = request.json or {}
    ban_id = data.get("ban_id")
    if not ban_id:
        return {"error": "ban_id is required"}, 400

    data["ban_id"] = ban_id
    data["status"] = data.get("status", "pending")
    data["created_at"] = data.get("created_at", now_iso())
    data["updated_at"] = now_iso()
    data["processed_by_game"] = data.get("processed_by_game", False)
    data["processed_at"] = data.get("processed_at")
    data["game_success"] = data.get("game_success")
    data["game_message"] = data.get("game_message")
    data["game_place_id"] = data.get("game_place_id")
    data["game_job_id"] = data.get("game_job_id")

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
    record["processed_by_game"] = record.get("processed_by_game", False)
    record["processed_at"] = record.get("processed_at")
    record["game_success"] = record.get("game_success")
    record["game_message"] = record.get("game_message")
    record["game_place_id"] = record.get("game_place_id")
    record["game_job_id"] = record.get("game_job_id")

    if action == "approve":
        record["status"] = "approved"
        record["processed_by_game"] = False
        record["processed_at"] = None
        record["game_success"] = None
        record["game_message"] = None
        record["game_place_id"] = None
        record["game_job_id"] = None
    elif action == "deny":
        record["status"] = "denied"
    elif record.get("status") is None:
        record["status"] = "approved"

    ban_records[ban_id] = record
    return {"success": True, "ban_id": ban_id, "status": record["status"]}, 200


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

    ban_records[ban_id] = record
    return {"success": True, "ban_id": ban_id, "status": record["status"]}, 200


@app.route("/ban-records/game-pending", methods=["GET"])
def get_game_pending():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    results = []
    for ban_id, record in ban_records.items():
        if record.get("platform") != "roblox":
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
