import os, csv, io
from datetime import timezone
from flask import Flask, redirect, url_for, session, send_file, request, abort, jsonify, Response
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
import google.auth.transport.requests
import requests as req_lib
from google.cloud import firestore

app = Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']

CLIENT_ID      = os.environ['GOOGLE_CLIENT_ID']
CLIENT_SECRET  = os.environ['GOOGLE_CLIENT_SECRET']
ALLOWED_DOMAIN = 'scality.com'
BASE_URL = os.environ.get('BASE_URL', 'https://ring-formation-1098349828563.europe-west1.run.app')

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '0'

db = firestore.Client()

def make_flow():
    return Flow.from_client_config(
        {"web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [BASE_URL + "/callback"],
        }},
        scopes=["openid", "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile"],
        redirect_uri=BASE_URL + "/callback",
    )

# ── auth ──────────────────────────────────────────────────────────────────────

@app.route('/login')
def login():
    flow = make_flow()
    auth_url, state = flow.authorization_url(prompt='select_account', access_type='online')
    session['state'] = state
    return redirect(auth_url)

@app.route('/callback')
def callback():
    flow = make_flow()
    flow.fetch_token(authorization_response=request.url.replace('http://', 'https://'))
    credentials = flow.credentials
    token_request = google.auth.transport.requests.Request(session=req_lib.session())
    info = id_token.verify_oauth2_token(
        credentials.id_token, token_request, CLIENT_ID, clock_skew_in_seconds=10
    )
    email = info.get('email', '')
    if not email.endswith('@' + ALLOWED_DOMAIN):
        abort(403, f'Accès réservé aux comptes @{ALLOWED_DOMAIN}')
    session['email'] = email
    session['name']  = info.get('name', email.split('@')[0])
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

# ── pages ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'email' not in session:
        return redirect(url_for('login'))
    return send_file('ring-formation.html')

@app.route('/ring-release-notes.html')
def release_notes():
    if 'email' not in session:
        return redirect(url_for('login'))
    return send_file('ring-release-notes.html')

# ── comments API ──────────────────────────────────────────────────────────────

@app.route('/api/comments', methods=['GET'])
def get_comments():
    if 'email' not in session:
        abort(401)
    module = request.args.get('module', 'general')
    docs = db.collection('comments').where('module', '==', module).stream()
    result = []
    for d in docs:
        data = d.to_dict()
        ts = data.get('ts')
        result.append({
            'id':     d.id,
            'module': data.get('module'),
            'text':   data.get('text'),
            'email':  data.get('email'),
            'name':   data.get('name'),
            'ts':     ts.astimezone(timezone.utc).strftime('%d/%m/%Y %H:%M') if ts else '',
            '_sort':  ts.timestamp() if ts else 0,
        })
    result.sort(key=lambda x: x.pop('_sort'))
    return jsonify(result)

@app.route('/api/comments', methods=['POST'])
def post_comment():
    if 'email' not in session:
        abort(401)
    data = request.get_json(force=True)
    text = (data.get('text') or '').strip()
    if not text:
        abort(400, 'Commentaire vide')
    db.collection('comments').add({
        'module': data.get('module', 'general'),
        'text':   text,
        'email':  session['email'],
        'name':   session.get('name', session['email'].split('@')[0]),
        'ts':     firestore.SERVER_TIMESTAMP,
    })
    return jsonify({'ok': True})

@app.route('/api/comments/<doc_id>', methods=['DELETE'])
def delete_comment(doc_id):
    if 'email' not in session:
        abort(401)
    ref = db.collection('comments').document(doc_id)
    doc = ref.get()
    if not doc.exists:
        abort(404)
    if doc.to_dict().get('email') != session['email']:
        abort(403, 'Vous ne pouvez supprimer que vos propres commentaires')
    ref.delete()
    return jsonify({'ok': True})

@app.route('/api/comments/export')
def export_comments():
    if 'email' not in session:
        abort(401)
    docs = db.collection('comments').stream()
    rows = []
    for d in docs:
        data = d.to_dict()
        ts = data.get('ts')
        rows.append({
            'id':     d.id,
            'module': data.get('module', ''),
            'name':   data.get('name', ''),
            'email':  data.get('email', ''),
            'date':   ts.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if ts else '',
            'text':   data.get('text', '').replace('\n', ' '),
        })
    rows.sort(key=lambda x: (x['module'], x['date']))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=['module','name','email','date','text','id'])
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        '﻿' + buf.getvalue(),  # BOM pour Excel
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="ring-formation-reviews.csv"'}
    )

@app.route('/api/me')
def me():
    if 'email' not in session:
        abort(401)
    return jsonify({'email': session['email'], 'name': session.get('name', '')})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
