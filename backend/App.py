from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import json
from datetime import datetime
import calendar
from flask_sqlalchemy import SQLAlchemy
from dateutil.relativedelta import relativedelta

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

    # Handle optional fields
    description = data.get('description', '')
    start_date_str = data.get('date', datetime.now().strftime('%Y-%m-%d'))
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d")

    is_recurring = data.get('is_recurring', False)
    recurrence_months = int(data.get('recurrence_months', 1)) if is_recurring else 1

    conn = sqlite3.connect('money_tracker.db')
    cursor = conn.cursor()

    transaction_ids = []

    for i in range(recurrence_months):
        transaction_date = (start_date + relativedelta(months=i)).strftime("%Y-%m-%d")

        cursor.execute('''
            INSERT INTO transactions (type, category, amount, description, date)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            data['type'],
            data['category'],
            float(data['amount']),
            description,
            transaction_date
        ))

        transaction_ids.append(cursor.lastrowid)

    conn.commit()
    conn.close()

    return jsonify({
        'ids': transaction_ids,
        'message': f'{recurrence_months} transaction(s) added successfully'
    }), 201



@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    period = request.args.get('period', 'monthly')
    categories = request.args.get('categories', '')
    category = request.args.get('category', '')  # for yearly single category filter

    conn = sqlite3.connect('money_tracker.db')
    cursor = conn.cursor()

    if period == 'monthly':
        date_group = "strftime('%Y-%m', date)"
    else:
        date_group = "strftime('%Y', date)"

    # --- Category Filter Logic ---
    category_filter = ""
    params = []

    if period == 'monthly' and categories and categories.lower() != 'all':
        category_list = categories.split(',')
        placeholders = ','.join(['?' for _ in category_list])
        category_filter = f" WHERE category IN ({placeholders})"
        params = category_list

    elif period == 'yearly' and category and category.lower() != 'all':
        category_filter = " WHERE category = ?"
        params = [category]

    # --- Summary ---
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

    # --- Category Breakdown ---
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
        period_key, category_name, transaction_type, total = row
        if period_key not in category_breakdown:
            category_breakdown[period_key] = {'income': {}, 'expense': {}}
        category_breakdown[period_key][transaction_type][category_name] = total

    # --- Transaction Details ---
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
@app.route('/api/transactions/<int:transaction_id>', methods=['PUT'])
def update_transaction(transaction_id):
    data = request.json

    conn = sqlite3.connect('money_tracker.db')
    cursor = conn.cursor()

    cursor.execute('SELECT id FROM transactions WHERE id = ?', (transaction_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({'error': 'Transaction not found'}), 404

    cursor.execute('''
        UPDATE transactions
        SET type = ?, category = ?, amount = ?, description = ?, date = ?
        WHERE id = ?
    ''', (
        data['type'],
        data['category'],
        float(data['amount']),
        data.get('description', ''),
        data['date'],
        transaction_id
    ))

    conn.commit()
    conn.close()
    return jsonify({'message': 'Transaction updated successfully'})
@app.route('/api/transactions/<int:transaction_id>', methods=['DELETE'])
def delete_transaction(transaction_id):
    conn = sqlite3.connect('money_tracker.db')
    cursor = conn.cursor()

    cursor.execute('SELECT id FROM transactions WHERE id = ?', (transaction_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({'error': 'Transaction not found'}), 404

    cursor.execute('DELETE FROM transactions WHERE id = ?', (transaction_id,))
    conn.commit()
    conn.close()

    return jsonify({'message': 'Transaction deleted successfully'})

@app.route('/api/categories', methods=['POST'])
def add_category():
    data = request.json
    category = data.get('name')
    type_ = data.get('type')
    if not category or not type_:
        return jsonify({'error': 'Missing data'}), 400

    conn = sqlite3.connect('money_tracker.db')
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO categories (name, type) VALUES (?, ?)', (category, type_))
    except sqlite3.IntegrityError:
        pass  # Category already exists
    conn.commit()

    cursor.execute('SELECT name FROM categories WHERE type = ?', (type_,))
    categories = [row[0] for row in cursor.fetchall()]
    conn.close()

    return jsonify(categories)

@app.route('/api/category/delete/<name>', methods=['DELETE'])
def delete_category(name):
    conn = sqlite3.connect('money_tracker.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM categories WHERE name = ?', (name,))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Category deleted'})

if __name__ == '__main__':
    print("Registered routes:")
    for rule in app.url_map.iter_rules():
        print(rule)
    app.run(debug=True)
