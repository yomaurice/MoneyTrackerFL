import datetime
import hashlib

from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)   # NEW
    password_hash = db.Column(db.String(256), nullable=False)
    reset_token = db.Column(db.String(256), nullable=True)
    reset_token_expiration = db.Column(db.DateTime, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

# Add a user_id foreign key to Transaction and Category models:
class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String)
    type = db.Column(db.String)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String, nullable=False)  # 'income' or 'expense'
    category = db.Column(db.String, nullable=False)  # category name or could be foreign key
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String)
    date = db.Column(db.Date, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    currency = db.Column(db.String(10), nullable=False, server_default='ILS')
    exchange_rate = db.Column(db.Float, nullable=False, server_default='1.0')
    created_at = db.Column(
        db.DateTime, nullable=False, server_default=db.func.now()
    )


REFRESH_TOKEN_DAYS = 90


class RefreshToken(db.Model):
    """One row per issued refresh token, so sessions can be rotated and revoked.

    A stateless JWT cannot be withdrawn once issued. Storing a hash of each
    token buys two things: a logout that genuinely ends the session, and reuse
    detection -- a token presented after it has already been rotated means a
    copy is circulating, so the whole family is revoked.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('user.id'), nullable=False, index=True
    )
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    issued_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    expires_at = db.Column(db.DateTime, nullable=False)
    rotated_to = db.Column(db.String(64), nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    user_agent = db.Column(db.String(300), nullable=True)

    @staticmethod
    def hash_token(raw_token):
        return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()

    @property
    def is_active(self):
        return (
            self.revoked_at is None
            and self.rotated_to is None
            and self.expires_at > datetime.datetime.utcnow()
        )
