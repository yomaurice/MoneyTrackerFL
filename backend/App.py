import sys
import traceback

from flask import Flask, request, jsonify
from flask_cors import CORS
import datetime
from dateutil.relativedelta import relativedelta
from flask_sqlalchemy import SQLAlchemy
from models import db, User, Transaction, Category
import os
from dotenv import load_dotenv
import logging
import jwt
from functools import wraps
import resend
import re
RESET_TOKEN_EXPIRE_MIN = 20



load_dotenv()
app = Flask(__name__)

# app.config['SECRET_KEY'] = 'your-secret-key'
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ["DATABASE_URL"]
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "connect_args": {
        "sslmode": "require"
    }
}

print("DATABASE_URL:", os.environ.get("DATABASE_URL"))

db.init_app(app)
# CORS(app, supports_credentials=True, origins=["http://localhost:3000"])
# CORS(app, supports_credentials=True, resources={r"/api/*": {"origins": "http://localhost:3000"}}, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
CORS(
    app,
    supports_credentials=True,
    origins=[
        "https://money-tracker1.vercel.app",
        "https://moneytrackerfl.onrender.com",
        "https://trackex.store",
        re.compile(r"https://.*\.vercel\.app"),
        "http://localhost:3000",
    ],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    expose_headers=["Content-Type", "Authorization"],
)

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

with app.app_context():
    db.create_all()

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
        print("Email sent:", email)
        return True
    except Exception as e:
        print("Resend ERROR:", e)
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
        'exp': datetime.datetime.utcnow() + datetime.timedelta(days=14)
    }
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')


def decode_token(token, expected_type):
    try:
        payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
        if payload.get('type') != expected_type:
            return None
        return payload['user_id']
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def generate_token(user_id):
    payload = {
        'user_id': user_id,
        'exp': datetime.datetime.utcnow() + datetime.timedelta(days=1)
    }
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')

# def decode_token(token):
#     try:
#         payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
#         return payload['user_id']
#     except jwt.ExpiredSignatureError:
#         return None
#     except jwt.InvalidTokenError:
#         return None

from flask import g

@app.route("/")
def index():
    return "OK", 200

@app.after_request
def apply_cors(response):
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('access_token')
        if not token:
            return jsonify({'message': 'Unauthorized'}), 401

        user_id = decode_token(token, 'access')
        if not user_id:
            return jsonify({'message': 'Access token expired'}), 401

        g.user_id = user_id
        return f(*args, **kwargs)
    return decorated


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
            user_id=user_id
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
    transactions = Transaction.query.filter_by(user_id=user_id).order_by(Transaction.date.desc(), Transaction.created_at.desc()).limit(100).all()
    return jsonify([{
        'id': tx.id,
        'type': tx.type,
        'category': tx.category,
        'amount': tx.amount,
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

    if User.query.filter_by(email=email).first():
        return jsonify({'message': 'Email already exists'}), 400

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
    refresh_token = generate_refresh_token(user.id)

    resp = jsonify({'message': 'Login successful'})
    resp.set_cookie(
        'access_token',
        access_token,
        httponly=True,
        secure=True,
        samesite='None',  # IMPORTANT for Vercel ↔ Render
        max_age=15 * 60,
        path='/'
    )
    resp.set_cookie(
        'refresh_token',
        refresh_token,
        httponly=True,
        secure=True,
        samesite='None',
        max_age=14 * 24 * 60 * 60,
        path='/'
    )

    return resp, 200

@app.route('/api/logout', methods=['POST'])
def logout():
    resp = jsonify({'message': 'Logged out'})
    resp.delete_cookie('access_token')
    resp.delete_cookie('refresh_token')
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

    new_access = generate_access_token(user_id)

    resp = jsonify({'message': 'refreshed'})
    resp.set_cookie(
        'access_token',
        new_access,
        httponly=True,
        secure=True,
        samesite='None',
        path='/',
        max_age=15 * 60
    )
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
    return jsonify({
        'id': user.id,
        'username': user.username
    })

# endpoint of to keep backend alive and reactive
@app.route("/api/health")
def health():
    return {"status": "ok"}, 200

@app.errorhandler(Exception)
def handle_exception(e):
    logging.error(traceback.format_exc())
    return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    print("Registered routes:")
    for rule in app.url_map.iter_rules():
        print(rule)
    app.run(debug=True)
