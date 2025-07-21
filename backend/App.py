from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
from dateutil.relativedelta import relativedelta
from flask_sqlalchemy import SQLAlchemy
from models import Transaction, Category, db
import os
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
CORS(app)

with app.app_context():
    db.create_all()

# Database setup
# def init_database():
#     conn = sqlite3.connect('money_tracker.db')
#     cursor = conn.cursor()
#
#     # Create transactions table
#     cursor.execute('''
#         CREATE TABLE IF NOT EXISTS transactions (
#             id INTEGER PRIMARY KEY AUTOINCREMENT,
#             type TEXT NOT NULL,  -- 'income' or 'expense'
#             category TEXT NOT NULL,
#             amount REAL NOT NULL,
#             description TEXT,
#             date TEXT NOT NULL,
#             created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
#         )
#     ''')
#
#     # Create categories table with default categories
#     cursor.execute('''
#         CREATE TABLE IF NOT EXISTS categories (
#             id INTEGER PRIMARY KEY AUTOINCREMENT,
#             name TEXT NOT NULL UNIQUE,
#             type TEXT NOT NULL  -- 'income' or 'expense'
#         )
#     ''')
#
#     # Insert default categories if they don't exist
#     default_expense_categories = [
#         'Food & Dining', 'Transportation', 'Shopping', 'Entertainment',
#         'Bills & Utilities', 'Healthcare', 'Education', 'Travel', 'Other'
#     ]
#
#     default_income_categories = [
#         'Salary', 'Freelance', 'Investment', 'Gift', 'Other'
#     ]
#
#     for category in default_expense_categories:
#         cursor.execute('INSERT OR IGNORE INTO categories (name, type) VALUES (?, ?)',
#                        (category, 'expense'))
#
#     for category in default_income_categories:
#         cursor.execute('INSERT OR IGNORE INTO categories (name, type) VALUES (?, ?)',
#                        (category, 'income'))
#
#     conn.commit()
#     conn.close()


# Initialize database on startup
# init_database()



@app.route('/api/categories/<transaction_type>', methods=['GET'])
def get_categories(transaction_type):
    categories = Category.query.filter_by(type=transaction_type).all()
    return jsonify([cat.name for cat in categories])


@app.route('/api/transactions', methods=['POST'])
def add_transaction():
    data = request.json
    start_date = datetime.strptime(data.get('date'), '%Y-%m-%d')
    recurrence_months = int(data.get('recurrence_months', 1)) if data.get('is_recurring') else 1

    transaction_ids = []

    for i in range(recurrence_months):
        transaction_date = start_date + relativedelta(months=i)
        tx = Transaction(
            type=data['type'],
            category=data['category'],
            amount=float(data['amount']),
            description=data.get('description', ''),
            date=transaction_date.date()
        )
        db.session.add(tx)
        db.session.flush()  # Make sure ID is generated immediately
        transaction_ids.append(tx.id)

    db.session.commit()

    return jsonify({
        'ids': transaction_ids,
        'message': f'{recurrence_months} transaction(s) added successfully'
    }), 201


@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    period = request.args.get('period', 'monthly')
    categories = request.args.get('categories', '')
    category = request.args.get('category', '')

    if period == 'monthly':
        date_format = '%Y-%m'
    else:
        date_format = '%Y'

    query = Transaction.query
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
def get_transactions():
    transactions = Transaction.query.order_by(Transaction.date.desc(), Transaction.created_at.desc()).limit(100).all()

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
def update_transaction(transaction_id):
    data = request.json
    tx = Transaction.query.get(transaction_id)

    if not tx:
        return jsonify({'error': 'Transaction not found'}), 404

    tx.type = data['type']
    tx.category = data['category']
    tx.amount = float(data['amount'])
    tx.description = data.get('description', '')
    tx.date = datetime.strptime(data['date'], '%Y-%m-%d').date()

    db.session.commit()
    return jsonify({'message': 'Transaction updated successfully'})

@app.route('/api/transactions/<int:transaction_id>', methods=['DELETE'])
def delete_transaction(transaction_id):
    tx = Transaction.query.get(transaction_id)
    if not tx:
        return jsonify({'error': 'Transaction not found'}), 404

    db.session.delete(tx)
    db.session.commit()
    return jsonify({'message': 'Transaction deleted successfully'})


@app.route('/api/categories', methods=['POST'])
def add_category():
    data = request.json
    name = data.get('name')
    type_ = data.get('type')

    if not name or not type_:
        return jsonify({'error': 'Missing data'}), 400

    existing = Category.query.filter_by(name=name).first()
    if not existing:
        db.session.add(Category(name=name, type=type_))
        db.session.commit()

    categories = Category.query.filter_by(type=type_).all()
    return jsonify([cat.name for cat in categories])


@app.route('/api/category/delete/<name>', methods=['DELETE'])
def delete_category(name):
    category = Category.query.filter_by(name=name).first()
    if category:
        db.session.delete(category)
        db.session.commit()
    return jsonify({'message': 'Category deleted'})


if __name__ == '__main__':
    print("Registered routes:")
    for rule in app.url_map.iter_rules():
        print(rule)
    app.run(debug=True)
