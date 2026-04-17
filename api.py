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
    return {"success": True, "ban_id": ban_id, "status": record["status"]}


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
    return {"success": True, "ban_id": ban_id, "status": record["status"]}


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
    return jsonify({"count": len(results), "records": results})


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
    return {"success": True, "ban_id": ban_id, "status": record["status"]}
