from flask import Flask, request, jsonify
import os
from datetime import datetime, timezone

app = Flask(__name__)

BAN_API_KEY = os.getenv("BAN_API_KEY")

# temp in-memory storage for now
# later you can move this to sqlite/postgres
ban_records = {}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def check_auth(req):
    auth = req.headers.get("Authorization")
    return auth == f"Bearer {BAN_API_KEY}"


@app.route("/")
def home():
    return "Ban API Running"


@app.route("/ban-records/request", methods=["POST"])
def create_request():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    data = request.json or {}
    ban_id = data.get("ban_id")
    if not ban_id:
        return {"error": "ban_id is required"}, 400

    data["status"] = data.get("status", "pending")
    data["created_at"] = data.get("created_at", now_iso())
    data["updated_at"] = now_iso()
    data["processed_by_game"] = data.get("processed_by_game", False)
    data["processed_at"] = data.get("processed_at")
    data["api_source"] = "discord_bot"

    ban_records[ban_id] = data
    return {"success": True, "ban_id": ban_id}


@app.route("/ban-records/execute", methods=["POST"])
def execute_ban():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    data = request.json or {}
    ban_id = data.get("ban_id")
    action = data.get("action")

    if not ban_id:
        return {"error": "ban_id is required"}, 400

    existing = ban_records.get(ban_id, {})
    existing.update(data)

    if action == "approve":
        existing["status"] = "approved"
    elif action == "deny":
        existing["status"] = "denied"
    elif existing.get("status") is None:
        existing["status"] = "approved"

    existing["updated_at"] = now_iso()
    existing["processed_by_game"] = existing.get("processed_by_game", False)
    existing["processed_at"] = existing.get("processed_at")

    ban_records[ban_id] = existing
    return {"success": True, "ban_id": ban_id, "status": existing["status"]}


@app.route("/ban-records/<ban_id>", methods=["GET"])
def get_ban(ban_id):
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    data = ban_records.get(ban_id)
    if not data:
        return {"error": "Not found"}, 404

    return jsonify(data)


@app.route("/ban-records/pending", methods=["GET"])
def get_pending_bans():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    results = []
    for ban_id, record in ban_records.items():
        if (
            record.get("platform") == "roblox"
            and record.get("status") == "approved"
            and not record.get("processed_by_game", False)
        ):
            results.append(record)

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
    record["processed_by_game"] = True
    record["processed_at"] = now_iso()
    record["game_success"] = bool(success)
    record["game_message"] = data.get("message", "")
    record["game_place_id"] = data.get("place_id")
    record["game_job_id"] = data.get("job_id")
    record["updated_at"] = now_iso()

    if success:
        record["status"] = "completed"
    else:
        record["status"] = "game_failed"

    ban_records[ban_id] = record
    return {"success": True, "ban_id": ban_id, "status": record["status"]}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
