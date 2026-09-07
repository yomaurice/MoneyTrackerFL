import sys
import traceback

from flask import Flask, request, jsonify, g
from flask_cors import CORS
import datetime
from dateutil.relativedelta import relativedelta
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from models import (
    db,
    User,
    Transaction,
    Category,
    RefreshToken,
    REFRESH_TOKEN_DAYS,
)
import os
from dotenv import load_dotenv
import logging
import jwt
import uuid
from functools import wraps
import resend
import re
load_dotenv()
# Configured before anything else so startup warnings are not emitted through
# logging's handler of last resort.
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

app = Flask(__name__)

# app.config['SECRET_KEY'] = 'your-secret-key'
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")
if app.secret_key == "dev-secret-key" or len(app.secret_key) < 32:
    # Every access and refresh token is signed with this. A short or default
    # key means anyone can mint a token for any user, so a deployment must set
    # SECRET_KEY to at least 32 random bytes. Warned rather than fatal so an
    # already-running deployment is not taken down by an upgrade; make it fatal
    # once the environment is confirmed.
    logging.error(
        "SECRET_KEY is missing, default, or shorter than 32 bytes -- auth "
        "tokens are forgeable. Set SECRET_KEY in the environment."
    )
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ["DATABASE_URL"]
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
_is_local = os.environ.get("DATABASE_URL", "").startswith("postgresql://postgres@localhost") or "localhost" in os.environ.get("DATABASE_URL", "")
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "connect_args": {
        "sslmode": "disable" if _is_local else "require"
    },
    "pool_pre_ping": True,
    "pool_recycle": 300,
}


db.init_app(app)
migrate = Migrate(app, db)

CORS(
    app,
    supports_credentials=True,
    origins=[
        "https://money-tracker1.vercel.app",
        "https://moneytrackerfl.onrender.com",
        re.compile(r"^https:\/\/.*\.vercel\.app$"),
        "http://localhost:3000"
    ],
)

# Schema is owned by Alembic (see backend/migrations). db.create_all() used to
# live here, which is why new columns never reached the deployed database.
# The Procfile now runs `flask db upgrade` before gunicorn starts.

# Imported after the app and db exist. `login_required` and `token_required`
# now live in auth.py; existing routes below stay in this module deliberately,
# to keep the blast radius of new work small.
from auth import login_required, decode_token  # noqa: E402
from routes.tokens import tokens_bp  # noqa: E402
from routes.transactions_bulk import transactions_bulk_bp  # noqa: E402

app.register_blueprint(tokens_bp)
app.register_blueprint(transactions_bulk_bp)

resend.api_key = os.getenv("RESEND_API_KEY")

def send_reset_email(to_email, reset_link):
    try:
        params = {
            "from": "MoneyTracker <onboarding@resend.dev>",
            "to": [to_email],
            "subject": "Reset your password",
            "html": f"""<p>You requested a password reset.</p>
                    <p>Click the link below to reset your password:</p>
                    <p><a href="{reset_link}">Reset Password</a></p>
                    <p>If you did not request this, ignore this email.</p>"""
        }

        email = resend.Emails.send(params)
        logging.info("Email sent: %s", email)
        return True
    except Exception as e:
        logging.error("Resend error: %s", e)
        return False

def generate_access_token(user_id):
    payload = {
        'user_id': user_id,
        'type': 'access',
        'exp': datetime.datetime.utcnow() + datetime.timedelta(minutes=15)
    }
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')


def generate_refresh_token(user_id):
    payload = {
        'user_id': user_id,
        'type': 'refresh',
        'jti': uuid.uuid4().hex,
        'exp': datetime.datetime.utcnow() + datetime.timedelta(
            days=REFRESH_TOKEN_DAYS
        )
    }
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')


ACCESS_TOKEN_SECONDS = 15 * 60
REFRESH_TOKEN_SECONDS = REFRESH_TOKEN_DAYS * 24 * 60 * 60

# How long after a refresh token is rotated a replay of it is still treated as
# a race rather than as theft. See the reuse branch in /api/refresh.
REFRESH_GRACE_SECONDS = 60

# SameSite=Lax rather than None: every production API call is first-party via
# the frontend's rewrite proxy, so Lax is not restricted by third-party cookie
# policies, and it still survives the top-level navigation that opening the app
# from a notification performs.
_COOKIE_FLAGS = dict(httponly=True, secure=True, samesite='Lax', path='/')


def set_auth_cookies(resp, access_token=None, refresh_token=None):
    """Single place where auth cookie attributes are defined.

    They previously lived inline in three routes and drifted apart, which is
    the kind of thing that quietly breaks a session.
    """
    if access_token is not None:
        resp.set_cookie(
            'access_token', access_token,
            max_age=ACCESS_TOKEN_SECONDS, **_COOKIE_FLAGS
        )
    if refresh_token is not None:
        resp.set_cookie(
            'refresh_token', refresh_token,
            max_age=REFRESH_TOKEN_SECONDS, **_COOKIE_FLAGS
        )
    return resp


def clear_auth_cookies(resp):
    resp.delete_cookie('access_token', **_COOKIE_FLAGS)
    resp.delete_cookie('refresh_token', **_COOKIE_FLAGS)
    return resp


def issue_refresh_token(user_id):
    """Mint a refresh token and record its hash. Caller commits."""
    token = generate_refresh_token(user_id)
    now = datetime.datetime.utcnow()
    record = RefreshToken(
        user_id=user_id,
        token_hash=RefreshToken.hash_token(token),
        issued_at=now,
        expires_at=now + datetime.timedelta(days=REFRESH_TOKEN_DAYS),
        user_agent=(request.headers.get('User-Agent') or '')[:300] or None,
    )
    db.session.add(record)
    return token, record


def revoke_refresh_family(user_id):
    """Revoke every live refresh token for a user. Caller commits."""
    RefreshToken.query.filter(
        RefreshToken.user_id == user_id,
        RefreshToken.revoked_at.is_(None),
    ).update(
        {'revoked_at': datetime.datetime.utcnow()},
        synchronize_session=False,
    )


def prune_refresh_tokens(user_id):
    """Drop rows that can no longer authenticate anything. Caller commits.

    Rotation means a row per refresh, and a rotated row is not an expired one,
    so without this the table grows for the full 90-day window. Spent rows are
    kept for a week first: that is the forensic window in which a replay is
    still reported as reuse rather than merely as an unknown token.
    """
    now = datetime.datetime.utcnow()
    RefreshToken.query.filter(
        RefreshToken.user_id == user_id,
        db.or_(
            RefreshToken.expires_at < now - datetime.timedelta(days=30),
            db.and_(
                RefreshToken.issued_at < now - datetime.timedelta(days=7),
                db.or_(
                    RefreshToken.rotated_to.isnot(None),
                    RefreshToken.revoked_at.isnot(None),
                ),
            ),
        ),
    ).delete(synchronize_session=False)


@app.route('/api/categories', methods=['GET'])
@login_required
def get_all_categories():
    categories = Category.query.filter_by(user_id=g.user_id).all()
    return jsonify([
        {"name": c.name, "type": c.type}
        for c in categories
    ])


@app.route('/api/categories/<type>', methods=['GET'])
@login_required
def get_categories(type):
    categories = Category.query.filter_by(
        type=type,
        user_id=g.user_id
    ).all()
    return jsonify([cat.name for cat in categories])


@app.route('/api/transactions', methods=['POST'])
@login_required
def add_transaction():
    user_id = g.user_id
    data = request.json
    start_date = datetime.datetime.strptime(data.get('date'), '%Y-%m-%d')
    recurrence_months = int(data.get('recurrence_months', 1)) if data.get('is_recurring') else 1

    transaction_ids = []

    for i in range(recurrence_months):
        transaction_date = start_date + relativedelta(months=i)
        tx = Transaction(
            type=data['type'],
            category=data['category'],
            amount=float(data['amount']),
            description=data.get('description', ''),
            date=transaction_date.date(),
            user_id=user_id,
            currency=data.get('currency', 'ILS'),
            exchange_rate=float(data.get('exchange_rate', 1.0))
        )
        db.session.add(tx)
        db.session.flush()
        transaction_ids.append(tx.id)

    db.session.commit()

    return jsonify({
        'ids': transaction_ids,
        'message': f'{recurrence_months} transaction(s) added successfully'
    }), 201


@app.route('/api/analytics', methods=['GET'])
@login_required
def get_analytics():
    user_id = g.user_id
    period = request.args.get('period', 'monthly')
    categories = request.args.get('categories', '')
    category = request.args.get('category', '')

    if period == 'monthly':
        date_format = '%Y-%m'
    elif period == 'yearly':
        date_format = '%Y'

    query = Transaction.query.filter_by(user_id=user_id)

    if period == 'monthly' and categories and categories.lower() != 'all':
        category_list = categories.split(',')
        query = query.filter(Transaction.category.in_(category_list))
    elif period == 'yearly' and category and category.lower() != 'all':
        query = query.filter(Transaction.category == category)

    transactions = query.all()

    summary = {}
    category_breakdown = {}
    details = {}

    for tx in transactions:
        period_key = tx.date.strftime(date_format)

        # Summary
        if period_key not in summary:
            summary[period_key] = {'income': 0, 'expense': 0}
        summary[period_key][tx.type] += tx.amount

        # Category breakdown
        if period_key not in category_breakdown:
            category_breakdown[period_key] = {'income': {}, 'expense': {}}
        if tx.category not in category_breakdown[period_key][tx.type]:
            category_breakdown[period_key][tx.type][tx.category] = 0
        category_breakdown[period_key][tx.type][tx.category] += tx.amount

        # Details
        if period_key not in details:
            details[period_key] = []
        details[period_key].append({
            'id': tx.id,
            'type': tx.type,
            'category': tx.category,
            'amount': tx.amount,
            'currency': tx.currency or 'ILS',
            'exchange_rate': tx.exchange_rate or 1.0,
            'description': tx.description,
            'date': tx.date.strftime('%Y-%m-%d')
        })

    return jsonify({
        'summary': summary,
        'categoryBreakdown': category_breakdown,
        'details': details
    })


@app.route('/api/transactions', methods=['GET'])
@login_required
def get_transactions():
    user_id = g.user_id
    transactions = (
        Transaction.query
        .filter_by(user_id=user_id)
        .order_by(
            Transaction.date.desc(),
            Transaction.created_at.desc(),
            Transaction.id.desc(),
        )
        .limit(100)
        .all()
    )
    return jsonify([{
        'id': tx.id,
        'type': tx.type,
        'category': tx.category,
        'amount': tx.amount,
        'currency': tx.currency or 'ILS',
        'exchange_rate': tx.exchange_rate or 1.0,
        'description': tx.description,
        'date': tx.date.strftime('%Y-%m-%d'),
        'created_at': tx.created_at.strftime('%Y-%m-%d %H:%M:%S')
    } for tx in transactions])


@app.route('/api/transactions/<int:transaction_id>', methods=['PUT'])
@login_required
def update_transaction(transaction_id):
    user_id = g.user_id
    data = request.json
    tx = Transaction.query.filter_by(id=transaction_id, user_id=user_id).first()
    if not tx:
        return jsonify({'error': 'Transaction not found'}), 404

    tx.type = data['type']
    tx.category = data['category']
    tx.amount = float(data['amount'])
    tx.description = data.get('description', '')
    tx.date = datetime.datetime.strptime(data['date'], '%Y-%m-%d').date()
    tx.user_id = user_id
    tx.currency = data.get('currency', tx.currency or 'ILS')
    tx.exchange_rate = float(data.get('exchange_rate', tx.exchange_rate or 1.0))

    db.session.commit()
    return jsonify({'message': 'Transaction updated successfully'})

@app.route('/api/transactions/<int:transaction_id>', methods=['DELETE'])
@login_required
def delete_transaction(transaction_id):
    user_id = g.user_id
    tx = Transaction.query.filter_by(id=transaction_id, user_id=user_id).first()
    if not tx:
        return jsonify({'error': 'Transaction not found'}), 404
    db.session.delete(tx)
    db.session.commit()
    return jsonify({'message': 'Transaction deleted successfully'})


@app.route('/api/categories', methods=['POST'])
@login_required
def add_category():
    user_id = g.user_id
    data = request.json
    name = data.get('name')
    type_ = data.get('type')

    if not name or not type_:
        return jsonify({'error': 'Missing data'}), 400

    existing = Category.query.filter_by(name=name, type=type_, user_id=user_id).first()
    if not existing:
        db.session.add(Category(name=name, type=type_, user_id=user_id))
        db.session.commit()

    categories = Category.query.filter_by(type=type_, user_id=user_id).all()
    return jsonify([cat.name for cat in categories])



@app.route('/api/category/delete/<name>', methods=['DELETE'])
@login_required
def delete_category(name):
    user_id = g.user_id
    category = Category.query.filter_by(name=name, user_id=user_id).first()
    if category:
        db.session.delete(category)
        db.session.commit()
    return jsonify({'message': 'Category deleted'})

@app.route("/years", methods=["GET"])
def get_years_with_data():
    years = (
        db.session.query(db.extract('year', Transaction.date).label('year'))
        .group_by('year')
        .order_by('year')
        .all()
    )
    # format: [(2023,), (2024,), ...] → just extract the int
    return {"years": [int(y[0]) for y in years]}

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.get_json(silent=True)

    username = data.get('username')
    password = data.get('password')
    email = data.get('email')

    if not username or not password or not email:
        return jsonify({'message': 'Username, email and password required'}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({'message': 'Username already exists'}), 400

    user = User(username=username, email=email)
    user.set_password(password)

    db.session.add(user)
    db.session.commit()

    return jsonify({'message': 'User created'}), 201


@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True)

    if not data:
        return jsonify({'message': 'Invalid JSON body'}), 400

    username = data.get('username')
    password = data.get('password')

    user = User.query.filter_by(username=username).first()

    if not user or not user.check_password(password):
        return jsonify({'message': 'Invalid credentials'}), 401

    access_token = generate_access_token(user.id)
    refresh_token, _ = issue_refresh_token(user.id)
    prune_refresh_tokens(user.id)
    db.session.commit()

    resp = jsonify({'message': 'Login successful'})
    set_auth_cookies(resp, access_token, refresh_token)

    return resp, 200


@app.route('/api/logout', methods=['POST'])
def logout():
    # Revoke only the token presented here, so signing out on the phone does
    # not end the session on the desktop.
    token = request.cookies.get('refresh_token')
    if token:
        record = RefreshToken.query.filter_by(
            token_hash=RefreshToken.hash_token(token)
        ).first()
        if record and record.revoked_at is None:
            record.revoked_at = datetime.datetime.utcnow()
            db.session.commit()

    resp = jsonify({'message': 'Logged out'})
    clear_auth_cookies(resp)
    return resp


@app.route('/api/request_password_reset', methods=['POST'])
def request_password_reset():
    data = request.get_json()
    username = data.get("username")

    if not username:
        return jsonify({"message": "Username required"}), 400

    user = User.query.filter_by(username=username).first()

    if not user:
        # Tell frontend "user does not exist"
        return jsonify({"message": "User not found"}), 404

    # User exists → create token
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=15)},
        app.config["SECRET_KEY"],
        algorithm="HS256"
    )

    FRONTEND_URL = os.getenv("FRONTEND_URL")
    reset_link = f"{FRONTEND_URL}/reset-password?token={token}"

    send_reset_email(user.email, reset_link)

    return jsonify({"message": "Email sent"}), 200


@app.route('/api/reset_password', methods=['POST'])
def reset_password():
    data = request.get_json()
    token = data.get("token")
    new_password = data.get("password")

    if not token or not new_password:
        return jsonify({"message": "Missing token or password"}), 400

    try:
        payload = jwt.decode(
            token,
            app.config['SECRET_KEY'],
            algorithms=['HS256']
        )
        user_id = payload["user_id"]

    except jwt.ExpiredSignatureError:
        return jsonify({"message": "Token expired"}), 400
    except Exception:
        return jsonify({"message": "Invalid token"}), 400

    user = User.query.get(user_id)
    user.set_password(new_password)
    db.session.commit()

    return jsonify({"message": "Password changed"}), 200


@app.route('/api/refresh', methods=['POST'])
def refresh():
    token = request.cookies.get('refresh_token')

    if not token:
        return jsonify({'message': 'No refresh token'}), 401

    try:
        payload = jwt.decode(
            token,
            app.config['SECRET_KEY'],
            algorithms=['HS256']
        )
        if payload.get('type') != 'refresh':
            return jsonify({'message': 'Invalid token type'}), 401

        user_id = payload['user_id']

    except jwt.ExpiredSignatureError:
        return jsonify({'message': 'Refresh token expired'}), 401
    except jwt.InvalidTokenError:
        return jsonify({'message': 'Invalid refresh token'}), 401

    record = RefreshToken.query.filter_by(
        token_hash=RefreshToken.hash_token(token)
    ).first()

    if record is None:
        # A correctly signed token with no row is a session that was issued
        # before rotation shipped. Adopt it once rather than logging everyone
        # out on deploy. Only pre-rotation tokens qualify -- they have no
        # `jti` -- so this path closes by itself once the old 14-day tokens
        # expire, and an unknown token that *does* carry a jti is rejected.
        if payload.get('jti') is not None:
            resp = jsonify({'message': 'Unknown refresh token'})
            clear_auth_cookies(resp)
            return resp, 401
    elif record.rotated_to is not None:
        successor = RefreshToken.query.filter_by(
            token_hash=record.rotated_to
        ).first()
        age = datetime.datetime.utcnow() - (
            successor.issued_at if successor else record.issued_at
        )

        if (
            successor is not None
            and successor.is_active
            and age <= datetime.timedelta(seconds=REFRESH_GRACE_SECONDS)
        ):
            # Benign replay, not theft. Two tabs opening at once, or a retry
            # after a timeout, both present the same cookie: the first rotates
            # it, the second arrives moments later still holding it. Punishing
            # that would log the user out for using two tabs.
            #
            # The browser already holds the successor cookie from the winning
            # request, so hand back only a fresh access token and leave the
            # refresh cookie alone. The window is short and the successor must
            # still be live, so a genuinely stolen token gets no useful reach.
            resp = jsonify({'message': 'refreshed'})
            set_auth_cookies(resp, generate_access_token(user_id))
            return resp

        # Presented long after it was exchanged: a copy is circulating and the
        # whole family is no longer trustworthy.
        logging.warning('Refresh token reuse for user %s', user_id)
        revoke_refresh_family(user_id)
        db.session.commit()
        resp = jsonify({'message': 'Refresh token reuse detected'})
        clear_auth_cookies(resp)
        return resp, 401
    elif record.revoked_at is not None:
        resp = jsonify({'message': 'Refresh token revoked'})
        clear_auth_cookies(resp)
        return resp, 401
    elif record.expires_at <= datetime.datetime.utcnow():
        resp = jsonify({'message': 'Refresh token expired'})
        clear_auth_cookies(resp)
        return resp, 401

    if not db.session.get(User, user_id):
        resp = jsonify({'message': 'Unknown user'})
        clear_auth_cookies(resp)
        return resp, 401

    new_refresh, _ = issue_refresh_token(user_id)
    if record is not None:
        record.rotated_to = RefreshToken.hash_token(new_refresh)
    db.session.commit()

    new_access = generate_access_token(user_id)

    resp = jsonify({'message': 'refreshed'})
    # The refresh cookie is reissued too, so the 90-day window slides forward
    # on every use: an app opened regularly never asks for a password again.
    set_auth_cookies(resp, new_access, new_refresh)
    return resp

@app.route('/api/check_username', methods=['GET'])
def check_username():
    username = request.args.get('username', '').strip()

    if not username:
        return jsonify({'available': False, 'message': 'Missing username'}), 400

    taken = User.query.filter_by(username=username).first() is not None

    return jsonify({'available': not taken})

@app.route('/api/me', methods=['GET'])
@login_required
def me():
    user = User.query.get(g.user_id)

    if not user:
        return jsonify({'message': 'User not found'}), 404

    return jsonify({
        'id': user.id,
        'username': user.username
    })


@app.route('/api/exchange-rate', methods=['GET'])
@login_required
def get_exchange_rate():
    import requests as req
    from_currency = request.args.get('from', 'USD').upper()
    to_currency = request.args.get('to', 'ILS').upper()
    if from_currency == to_currency:
        return jsonify({'rate': 1.0})
    try:
        r = req.get(f'https://open.er-api.com/v6/latest/{from_currency}', timeout=5)
        data = r.json()
        rate = data['rates'].get(to_currency)
        if rate is None:
            return jsonify({'error': 'Currency not found'}), 400
        return jsonify({'rate': rate})
    except Exception as e:
        logging.error('Exchange rate fetch failed: %s', e)
        return jsonify({'error': 'Failed to fetch exchange rate'}), 502


# endpoint of to keep backend alive and reactive
@app.route("/api/health")
def health():
    return {"status": "ok"}, 200

@app.errorhandler(Exception)
def handle_exception(e):
    logging.error(traceback.format_exc())
    return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)

