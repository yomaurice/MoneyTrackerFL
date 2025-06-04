from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import json
from datetime import datetime
import calendar
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
import os
from dotenv import load_dotenv
from models import Transaction  # Import your SQLAlchemy models
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))


load_dotenv()
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
# migrate = Migrate(app, db)
CORS(app)  # Enable CORS for Next.js frontend


# Database setup
def init_database():
    conn = sqlite3.connect('money_tracker.db')
    cursor = conn.cursor()

    # Create transactions table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,  -- 'income' or 'expense'
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT,
            date TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Create categories table with default categories
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL  -- 'income' or 'expense'
        )
    ''')

    # Insert default categories if they don't exist
    default_expense_categories = [
        'Food & Dining', 'Transportation', 'Shopping', 'Entertainment',
        'Bills & Utilities', 'Healthcare', 'Education', 'Travel', 'Other'
    ]

    default_income_categories = [
        'Salary', 'Freelance', 'Investment', 'Gift', 'Other'
    ]

    for category in default_expense_categories:
        cursor.execute('INSERT OR IGNORE INTO categories (name, type) VALUES (?, ?)',
                       (category, 'expense'))

    for category in default_income_categories:
        cursor.execute('INSERT OR IGNORE INTO categories (name, type) VALUES (?, ?)',
                       (category, 'income'))

    conn.commit()
    conn.close()


# Initialize database on startup
init_database()



@app.route('/api/categories/<transaction_type>', methods=['GET'])
def get_categories(transaction_type):
    """Get categories for income or expense"""
    conn = sqlite3.connect('money_tracker.db')
    cursor = conn.cursor()

    cursor.execute('SELECT name FROM categories WHERE type = ?', (transaction_type,))
    categories = [row[0] for row in cursor.fetchall()]

    conn.close()
    return jsonify(categories)


@app.route('/api/transactions', methods=['POST'])
def add_transaction():
    """Add a new transaction"""
    data = request.json

    # Validate required fields
    required_fields = ['type', 'category', 'amount']
    if not all(field in data for field in required_fields):
        return jsonify({'error': 'Missing required fields'}), 400

    conn = sqlite3.connect('money_tracker.db')
    cursor = conn.cursor()

    # Insert transaction
    cursor.execute('''
        INSERT INTO transactions (type, category, amount, description, date)
        VALUES (?, ?, ?, ?, ?)
    ''', (
        data['type'],
        data['category'],
        float(data['amount']),
        data.get('description', ''),
        data.get('date', datetime.now().strftime('%Y-%m-%d'))
    ))

    conn.commit()
    transaction_id = cursor.lastrowid
    conn.close()

    return jsonify({'id': transaction_id, 'message': 'Transaction added successfully'}), 201

# @app.route('/api/transactions', methods=['POST'])
# def add_transaction():
#     data = request.json
#
#     required_fields = ['type', 'category', 'amount']
#     if not all(field in data for field in required_fields):
#         return jsonify({'error': 'Missing required fields'}), 400
#
#     new_transaction = Transaction(
#         type=data['type'],
#         category=data['category'],
#         amount=float(data['amount']),
#         description=data.get('description', ''),
#         date=datetime.strptime(data.get('date'), '%Y-%m-%d') if data.get('date') else datetime.utcnow()
#     )
#
#     db.session.add(new_transaction)
#     db.session.commit()
#
#     return jsonify({'id': new_transaction.id, 'message': 'Transaction added successfully'}), 201

# @app.route('/api/analytics', methods=['GET'])
# def get_analytics():
#     """Get analytics data"""
#     period = request.args.get('period', 'monthly')  # 'monthly' or 'yearly'
#     categories = request.args.get('categories', '')  # comma-separated categories
#
#     conn = sqlite3.connect('money_tracker.db')
#     cursor = conn.cursor()
#
#     # Base query
#     if period == 'monthly':
#         date_format = '%Y-%m'
#         date_group = "strftime('%Y-%m', date)"
#     else:  # yearly
#         date_format = '%Y'
#         date_group = "strftime('%Y', date)"
#
#     # Filter by categories if specified
#     category_filter = ""
#     params = []
#     if categories and categories != 'all':
#         category_list = categories.split(',')
#         placeholders = ','.join(['?' for _ in category_list])
#         category_filter = f" WHERE category IN ({placeholders})"
#         params = category_list
#
#     # Get summary data
#     cursor.execute(f'''
#         SELECT
#             {date_group} as period,
#             type,
#             SUM(amount) as total
#         FROM transactions
#         {category_filter}
#         GROUP BY {date_group}, type
#         ORDER BY period DESC
#     ''', params)
#
#     summary = {}
#     for row in cursor.fetchall():
#         period_key, transaction_type, total = row
#         if period_key not in summary:
#             summary[period_key] = {'income': 0, 'expense': 0}
#         summary[period_key][transaction_type] = total
#
#     # Get category breakdown
#     cursor.execute(f'''
#         SELECT
#             {date_group} as period,
#             category,
#             type,
#             SUM(amount) as total
#         FROM transactions
#         {category_filter}
#         GROUP BY {date_group}, category, type
#         ORDER BY period DESC, total DESC
#     ''', params)
#
#     category_breakdown = {}
#     for row in cursor.fetchall():
#         period_key, category, transaction_type, total = row
#         if period_key not in category_breakdown:
#             category_breakdown[period_key] = {'income': {}, 'expense': {}}
#         category_breakdown[period_key][transaction_type][category] = total
#
#     conn.close()
#
#     return jsonify({
#         'summary': summary,
#         'categoryBreakdown': category_breakdown
#     })

@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    period = request.args.get('period', 'monthly')
    categories = request.args.get('categories', '')

    conn = sqlite3.connect('money_tracker.db')
    cursor = conn.cursor()

    if period == 'monthly':
        date_group = "strftime('%Y-%m', date)"
    else:
        date_group = "strftime('%Y', date)"

    category_filter = ""
    params = []
    if categories and categories != 'all':
        category_list = categories.split(',')
        placeholders = ','.join(['?' for _ in category_list])
        category_filter = f" WHERE category IN ({placeholders})"
        params = category_list

    # Summary data (existing)
    cursor.execute(f'''
        SELECT 
            {date_group} as period,
            type,
            SUM(amount) as total
        FROM transactions
        {category_filter}
        GROUP BY {date_group}, type
        ORDER BY period DESC
    ''', params)
    summary = {}
    for row in cursor.fetchall():
        period_key, transaction_type, total = row
        if period_key not in summary:
            summary[period_key] = {'income': 0, 'expense': 0}
        summary[period_key][transaction_type] = total

    # Category breakdown (existing)
    cursor.execute(f'''
        SELECT 
            {date_group} as period,
            category,
            type,
            SUM(amount) as total
        FROM transactions
        {category_filter}
        GROUP BY {date_group}, category, type
        ORDER BY period DESC, total DESC
    ''', params)
    category_breakdown = {}
    for row in cursor.fetchall():
        period_key, category, transaction_type, total = row
        if period_key not in category_breakdown:
            category_breakdown[period_key] = {'income': {}, 'expense': {}}
        category_breakdown[period_key][transaction_type][category] = total

    # New: Get detailed transactions per period (for expense descriptions)
    cursor.execute(f'''
        SELECT
            {date_group} as period,
            id,
            type,
            category,
            amount,
            description,
            date
        FROM transactions
        {category_filter}
        ORDER BY date DESC, id DESC
    ''', params)

    details = {}
    for row in cursor.fetchall():
        period_key = row[0]
        tx = {
            'id': row[1],
            'type': row[2],
            'category': row[3],
            'amount': row[4],
            'description': row[5],
            'date': row[6],
        }
        if period_key not in details:
            details[period_key] = []
        details[period_key].append(tx)

    conn.close()

    return jsonify({
        'summary': summary,
        'categoryBreakdown': category_breakdown,
        'details': details
    })



@app.route('/api/transactions', methods=['GET'])
def get_transactions():
    """Get all transactions"""
    conn = sqlite3.connect('money_tracker.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, type, category, amount, description, date, created_at
        FROM transactions
        ORDER BY date DESC, created_at DESC
        LIMIT 100
    ''')

    transactions = []
    for row in cursor.fetchall():
        transactions.append({
            'id': row[0],
            'type': row[1],
            'category': row[2],
            'amount': row[3],
            'description': row[4],
            'date': row[5],
            'created_at': row[6]
        })

    conn.close()
    return jsonify(transactions)


if __name__ == '__main__':
    print("Registered routes:")
    for rule in app.url_map.iter_rules():
        print(rule)
    app.run(debug=True)
