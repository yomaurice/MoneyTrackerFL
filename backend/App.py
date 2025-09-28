from flask import Flask, request, jsonify
from flask_cors import CORS
import datetime
from dateutil.relativedelta import relativedelta
from flask_sqlalchemy import SQLAlchemy
from models import db, User, Transaction, Category
import os
from dotenv import load_dotenv

import jwt
from functools import wraps


load_dotenv()
app = Flask(__name__)

app.config['SECRET_KEY'] = 'your-secret-key'
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
# CORS(app, supports_credentials=True, origins=["http://localhost:3000"])
# CORS(app, supports_credentials=True, resources={r"/api/*": {"origins": "http://localhost:3000"}}, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
CORS(app, supports_credentials=True, resources={r"/api/*": {"origins": "*"}})

with app.app_context():
    db.create_all()

def generate_token(user_id):
    payload = {
        'user_id': user_id,
        'exp': datetime.datetime.utcnow() + datetime.timedelta(days=1)
    }
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')

def decode_token(token):
    try:
        payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
        return payload['user_id']
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        user_id = decode_token(token)
        if not user_id:
            return jsonify({'message': 'Unauthorized'}), 401
        return f(user_id=user_id, *args, **kwargs)
    return decorated



@app.route('/api/categories/<type>', methods=['GET'])
def get_categories(type):
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Missing token'}), 401

    token = auth_header.split(' ')[1]

    try:
        payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
        user_id = payload['user_id']
        categories = Category.query.filter_by(type=type, user_id=user_id).all()
        return jsonify([cat.name for cat in categories])

    except jwt.ExpiredSignatureError:
        return jsonify({'error': 'Token expired'}), 401
    except jwt.InvalidTokenError:
        return jsonify({'error': 'Invalid token'}), 401

@app.route('/api/transactions', methods=['POST'])
@login_required
def add_transaction(user_id):
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
def get_analytics(user_id):
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
def get_transactions(user_id):
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
def update_transaction(transaction_id, user_id):
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
def delete_transaction(transaction_id, user_id):
    tx = Transaction.query.filter_by(id=transaction_id, user_id=user_id).first()
    if not tx:
        return jsonify({'error': 'Transaction not found'}), 404
    db.session.delete(tx)
    db.session.commit()
    return jsonify({'message': 'Transaction deleted successfully'})


@app.route('/api/categories', methods=['POST'])
@login_required
def add_category(user_id):
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
def delete_category(name, user_id):
    category = Category.query.filter_by(name=name, user_id=user_id).first()
    if category:
        db.session.delete(category)
        db.session.commit()
    return jsonify({'message': 'Category deleted'})


@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.get_json()
    username = data['username']
    password = data['password']

    if User.query.filter_by(username=username).first():
        return jsonify({'message': 'Username already exists'}), 400

    user = User(username=username)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return jsonify({'message': 'User created'}), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data['username']
    password = data['password']

    user = User.query.filter_by(username=username).first()
    if user and user.check_password(password):
        token = generate_token(user.id)
        return jsonify({'token': token})
    return jsonify({'message': 'Invalid credentials'}), 401

@app.errorhandler(Exception)
def handle_exception(e):
    # Optionally, you can log the error to console or file
    print(e)
    return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    print("Registered routes:")
    for rule in app.url_map.iter_rules():
        print(rule)
    app.run(debug=True)
