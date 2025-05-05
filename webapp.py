from flask import Flask, request, send_from_directory, jsonify, abort
from bson import ObjectId
from db import db
import os
import logging

app = Flask(__name__, static_folder="static", template_folder="templates")

@app.route('/vote')
def vote():
    # Serves the static HTML
    return send_from_directory('templates', 'vote.html')

@app.route('/api/session/<session_id>')
def session_data(session_id):
    # Validate and convert to ObjectId
    try:
        oid = ObjectId(session_id)
    except Exception:
        abort(400, "Invalid session_id")

    sess = db.criteria_sessions.find_one({'_id': oid})
    if not sess:
        abort(404, "Session not found")

    return jsonify({
        "session_id": session_id,
        "fields":     sess.get("criteria_list", [])
    })

@app.route('/tinder')
def tinder():
    """
    Serve the swipeable card interface.
    Expects ?session_id=<criteria_session_id> in the URL.
    """
    session_id = request.args.get('session_id')
    if not session_id:
        abort(400, "Missing session_id")
    try:
        oid = ObjectId(session_id)
    except:
        abort(400, "Invalid session_id")

    # fetch the already-computed suggestions for this session
    tinder_sess = db.tinder_sessions.find_one({'session_id': oid})
    if not tinder_sess or tinder_sess.get('status') != 'voting':
        abort(404, "No active Tinder session")
    cities = tinder_sess['suggestions']  # e.g. ['Paris','Tokyo',…]
    return send_from_directory('templates', 'tinder.html')

@app.route('/api/tinder/<session_id>')
def tinder_data(session_id):
    try:
        oid = ObjectId(session_id)
    except:
        abort(400, "Bad session_id")
    tinder_sess = db.tinder_sessions.find_one({'session_id': oid})
    logging.info(f"tinder_sess: {tinder_sess}")
    if not tinder_sess:
        abort(404)
    return jsonify({
        'session_id': session_id,
        'cities': tinder_sess['suggestions']
    })

if __name__ == '__main__':
    port = int(os.getenv("WEBAPP_PORT", 5001))
    app.run(host="0.0.0.0", port=port)
