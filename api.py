from flask import Flask, request, jsonify
import os

app = Flask(__name__)

BAN_API_KEY = os.getenv("BAN_API_KEY")

ban_records = {}  # temp storage (we'll upgrade later)

def check_auth(req):
    auth = req.headers.get("Authorization")
    return auth == f"Bearer {BAN_API_KEY}"


@app.route("/ban-records/request", methods=["POST"])
def create_request():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    data = request.json
    ban_id = data["ban_id"]

    ban_records[ban_id] = data
    ban_records[ban_id]["status"] = "pending"

    return {"success": True}


@app.route("/ban-records/execute", methods=["POST"])
def execute_ban():
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    data = request.json
    ban_id = data["ban_id"]

    if ban_id not in ban_records:
        ban_records[ban_id] = {}

    ban_records[ban_id].update(data)
    ban_records[ban_id]["status"] = data.get("action", "approved")

    return {"success": True}


@app.route("/ban-records/<ban_id>", methods=["GET"])
def get_ban(ban_id):
    if not check_auth(request):
        return {"error": "Unauthorized"}, 401

    return jsonify(ban_records.get(ban_id, {}))


@app.route("/")
def home():
    return "Ban API Running"


app.run(host="0.0.0.0", port=5000)