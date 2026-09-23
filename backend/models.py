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

    __table_args__ = (
        # Scoped to the owner. The deployed database had UNIQUE (name) with no
        # user_id in it, which meant the first account to create "Groceries"
        # took that name away from everyone else. Type is part of the key so an
        # income and an expense category may share a name.
        db.UniqueConstraint('user_id', 'name', 'type',
                            name='uq_category_user_name_type'),
    )

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


class IngestToken(db.Model):
    """A long-lived bearer token for machines rather than browsers.

    A phone automation rule and a CI runner cannot perform the 15-minute cookie
    refresh dance that `login_required` assumes, so ingest routes accept
    `Authorization: Bearer <token>` instead. Stored hashed: the plaintext is
    shown once at creation and is unrecoverable afterwards.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('user.id'), nullable=False, index=True
    )
    name = db.Column(db.String(120), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    # First characters of the plaintext, kept so the UI can tell two tokens
    # apart without being able to reconstruct either.
    prefix = db.Column(db.String(8), nullable=False)
    scope = db.Column(db.String(30), nullable=False, server_default='ingest')
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    @staticmethod
    def hash_token(raw_token):
        return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()

    @property
    def is_active(self):
        return self.revoked_at is None


class SourceProfile(db.Model):
    """One bank or card account. Holds no credentials, by design.

    Shared by every sync tier -- statement upload today, an automated fetcher
    later -- so the settings page built against it does not need revisiting.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('user.id'), nullable=False, index=True
    )
    name = db.Column(db.String(120), nullable=False)
    kind = db.Column(db.String(10), nullable=False)          # bank | card
    company_id = db.Column(db.String(30), nullable=False)    # leumi | max | ...
    account_last4 = db.Column(db.String(8), nullable=True)
    combine_installments = db.Column(
        db.Boolean, nullable=False, server_default=db.text('false')
    )
    # Regex list. On a bank profile these suppress the aggregate card charge,
    # which is the same money as the individual card rows.
    exclude_patterns = db.Column(db.JSON, nullable=True)
    # Remembered upload column mapping, so a messy spreadsheet is mapped once.
    column_mapping = db.Column(db.JSON, nullable=True)
    active = db.Column(db.Boolean, nullable=False, server_default=db.text('true'))
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())


class ImportBatch(db.Model):
    """One ingest run, so progress can be shown and the whole run undone."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('user.id'), nullable=False, index=True
    )
    source = db.Column(db.String(30), nullable=False)  # wallet | upload | scraper
    source_profile_id = db.Column(
        db.Integer, db.ForeignKey('source_profile.id'), nullable=True
    )
    window_start = db.Column(db.Date, nullable=True)
    window_end = db.Column(db.Date, nullable=True)

    received = db.Column(db.Integer, nullable=False, server_default='0')
    new = db.Column(db.Integer, nullable=False, server_default='0')
    matched = db.Column(db.Integer, nullable=False, server_default='0')
    proposed = db.Column(db.Integer, nullable=False, server_default='0')
    ambiguous = db.Column(db.Integer, nullable=False, server_default='0')
    suppressed = db.Column(db.Integer, nullable=False, server_default='0')

    status = db.Column(
        db.String(10), nullable=False, server_default='running'
    )  # running | done | failed
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())


# StagedTransaction.state
STAGED_NEW = 'new'
STAGED_MATCHED = 'matched'
STAGED_PROPOSED = 'proposed'
STAGED_AMBIGUOUS = 'ambiguous'
STAGED_IGNORED = 'ignored'
STAGED_CONFIRMED = 'confirmed'
STAGED_SKIPPED = 'skipped'

STAGED_STATES = (
    STAGED_NEW, STAGED_MATCHED, STAGED_PROPOSED, STAGED_AMBIGUOUS,
    STAGED_IGNORED, STAGED_CONFIRMED, STAGED_SKIPPED,
)


class StagedTransaction(db.Model):
    """Every ingested row lands here first, whatever fed it.

    Nothing reaches the real `transaction` table without passing through here
    and being reviewed. `proposed` is the product of the whole pipeline: a real
    charge with no counterpart in the tracked data.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('user.id'), nullable=False, index=True
    )
    batch_id = db.Column(
        db.Integer, db.ForeignKey('import_batch.id'), nullable=True, index=True
    )
    source_profile_id = db.Column(
        db.Integer, db.ForeignKey('source_profile.id'), nullable=True
    )
    source = db.Column(db.String(30), nullable=False)
    # The issuer's own identifier when it gave one; absent for wallet captures.
    external_id = db.Column(db.String(120), nullable=True)
    dedup_hash = db.Column(db.String(64), nullable=False)
    raw = db.Column(db.JSON, nullable=True)

    txn_date = db.Column(db.Date, nullable=True)
    posted_date = db.Column(db.Date, nullable=True)
    amount = db.Column(db.Float, nullable=True)      # absolute value
    currency = db.Column(db.String(10), nullable=True)
    exchange_rate = db.Column(db.Float, nullable=True)
    type = db.Column(db.String(10), nullable=True)   # income | expense

    merchant_raw = db.Column(db.String(300), nullable=True)
    merchant_clean = db.Column(db.String(300), nullable=True)
    memo = db.Column(db.String(300), nullable=True)
    issuer_status = db.Column(db.String(30), nullable=True)
    installment_no = db.Column(db.Integer, nullable=True)
    installment_total = db.Column(db.Integer, nullable=True)

    state = db.Column(
        db.String(15), nullable=False, server_default=STAGED_NEW, index=True
    )
    matched_txn_id = db.Column(
        db.Integer, db.ForeignKey('transaction.id'), nullable=True
    )
    match_score = db.Column(db.Float, nullable=True)
    candidate_ids = db.Column(db.JSON, nullable=True)
    suggested_category = db.Column(db.String(120), nullable=True)
    suggested_description = db.Column(db.String(300), nullable=True)
    category_confidence = db.Column(db.Float, nullable=True)
    created_txn_id = db.Column(
        db.Integer, db.ForeignKey('transaction.id'), nullable=True
    )
    ignored_reason = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())

    __table_args__ = (
        # The idempotency guarantee: re-running an import, or the wallet
        # notification arriving by both the safety net and the deep link,
        # converges on one row instead of duplicating.
        db.UniqueConstraint('user_id', 'dedup_hash', name='uq_staged_user_dedup'),
    )


class MerchantRule(db.Model):
    """Learned merchant -> (category, description) mapping.

    Written whenever a proposal is confirmed, so both guesses improve with use
    rather than staying as good as the seed keyword map.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('user.id'), nullable=False, index=True
    )
    pattern = db.Column(db.String(300), nullable=False)
    is_regex = db.Column(db.Boolean, nullable=False, server_default=db.text('false'))
    category = db.Column(db.String(120), nullable=True)
    description = db.Column(db.String(300), nullable=True)
    txn_type = db.Column(db.String(10), nullable=True)
    hits = db.Column(db.Integer, nullable=False, server_default='0')
    auto_learned = db.Column(
        db.Boolean, nullable=False, server_default=db.text('false')
    )
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())

    __table_args__ = (
        db.UniqueConstraint('user_id', 'pattern', name='uq_merchant_rule_pattern'),
    )
