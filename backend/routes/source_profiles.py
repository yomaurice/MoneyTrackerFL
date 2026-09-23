"""Bank and card accounts. The settings page behind the Sync button.

A profile describes an account and holds no credentials, by design -- so this
table stays valid whichever tier ends up feeding it.
"""
import re

from flask import Blueprint, g, jsonify, request

from auth import login_required
from models import ImportBatch, SourceProfile, StagedTransaction, db
from services.reconcile import DEFAULT_CARD_AGGREGATE_PATTERNS

source_profiles_bp = Blueprint(
    'source_profiles', __name__, url_prefix='/api/source-profiles'
)

KINDS = ('bank', 'card')
COMPANIES = (
    'leumi', 'hapoalim', 'discount', 'mizrahi', 'onezero',
    'max', 'isracard', 'visaCal', 'amex', 'other',
)
MAX_PROFILES = 30
MAX_PATTERNS = 50


def _serialise(profile):
    return {
        'id': profile.id,
        'name': profile.name,
        'kind': profile.kind,
        'company_id': profile.company_id,
        'account_last4': profile.account_last4,
        'combine_installments': profile.combine_installments,
        'exclude_patterns': profile.exclude_patterns or [],
        'column_mapping': profile.column_mapping,
        'active': profile.active,
        'created_at': (
            profile.created_at.isoformat() if profile.created_at else None
        ),
    }


def _validate_patterns(patterns):
    """Reject a pattern that will not compile, at the point it is entered.

    The reconciler survives a broken pattern by skipping it, but silently
    ignoring an exclusion the user believes is active would mean double-counted
    money -- so it is refused here instead.
    """
    if patterns is None:
        return None, None
    if not isinstance(patterns, list):
        return None, 'exclude_patterns must be a list'
    if len(patterns) > MAX_PATTERNS:
        return None, f'at most {MAX_PATTERNS} exclude patterns'

    cleaned = []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            return None, 'each exclude pattern must be a non-empty string'
        pattern = pattern.strip()
        try:
            re.compile(pattern)
        except re.error as exc:
            return None, f'invalid regular expression {pattern!r}: {exc}'
        cleaned.append(pattern)
    return cleaned, None


def _apply(profile, data, creating):
    if 'name' in data or creating:
        name = (data.get('name') or '').strip()
        if not name:
            return 'name is required'
        if len(name) > 120:
            return 'name exceeds 120 characters'
        profile.name = name

    if 'kind' in data or creating:
        kind = data.get('kind')
        if kind not in KINDS:
            return f"kind must be one of {', '.join(KINDS)}"
        profile.kind = kind

    if 'company_id' in data or creating:
        company = data.get('company_id')
        if company not in COMPANIES:
            return f"company_id must be one of {', '.join(COMPANIES)}"
        profile.company_id = company

    if 'account_last4' in data:
        last4 = (data.get('account_last4') or '').strip()
        if last4 and not re.fullmatch(r'\d{2,8}', last4):
            return 'account_last4 must be 2-8 digits'
        profile.account_last4 = last4 or None

    if 'combine_installments' in data:
        if not isinstance(data['combine_installments'], bool):
            return 'combine_installments must be true or false'
        profile.combine_installments = data['combine_installments']

    if 'exclude_patterns' in data:
        cleaned, error = _validate_patterns(data['exclude_patterns'])
        if error:
            return error
        profile.exclude_patterns = cleaned

    if 'column_mapping' in data:
        mapping = data['column_mapping']
        if mapping is not None and not isinstance(mapping, dict):
            return 'column_mapping must be an object'
        profile.column_mapping = mapping

    if 'active' in data:
        if not isinstance(data['active'], bool):
            return 'active must be true or false'
        profile.active = data['active']

    return None


@source_profiles_bp.route('', methods=['GET'])
@login_required
def list_profiles():
    profiles = (
        SourceProfile.query
        .filter_by(user_id=g.user_id)
        .order_by(SourceProfile.kind.asc(), SourceProfile.name.asc())
        .all()
    )
    return jsonify([_serialise(p) for p in profiles])


@source_profiles_bp.route('', methods=['POST'])
@login_required
def create_profile():
    data = request.get_json(silent=True) or {}

    if SourceProfile.query.filter_by(user_id=g.user_id).count() >= MAX_PROFILES:
        return jsonify({'message': f'at most {MAX_PROFILES} accounts'}), 409

    profile = SourceProfile(user_id=g.user_id)
    error = _apply(profile, data, creating=True)
    if error:
        return jsonify({'message': error}), 400

    # A bank profile starts with the card-settlement exclusions, since that is
    # the double-count guard and nobody would think to add it by hand.
    if profile.kind == 'bank' and profile.exclude_patterns is None:
        profile.exclude_patterns = list(DEFAULT_CARD_AGGREGATE_PATTERNS)

    db.session.add(profile)
    db.session.commit()
    return jsonify(_serialise(profile)), 201


@source_profiles_bp.route('/<int:profile_id>', methods=['PUT', 'PATCH'])
@login_required
def update_profile(profile_id):
    profile = SourceProfile.query.filter_by(
        id=profile_id, user_id=g.user_id
    ).first()
    if profile is None:
        return jsonify({'message': 'Account not found'}), 404

    data = request.get_json(silent=True) or {}
    error = _apply(profile, data, creating=False)
    if error:
        return jsonify({'message': error}), 400

    db.session.commit()
    return jsonify(_serialise(profile))


@source_profiles_bp.route('/<int:profile_id>', methods=['DELETE'])
@login_required
def delete_profile(profile_id):
    profile = SourceProfile.query.filter_by(
        id=profile_id, user_id=g.user_id
    ).first()
    if profile is None:
        return jsonify({'message': 'Account not found'}), 404

    staged = StagedTransaction.query.filter_by(
        source_profile_id=profile_id
    ).count()
    if staged:
        # Deleting would orphan history, and the row's provenance is the only
        # record of where a charge came from. Deactivating keeps it readable.
        return jsonify({
            'message': (
                f'{staged} imported rows reference this account. '
                'Deactivate it instead of deleting.'
            ),
            'staged_rows': staged,
        }), 409

    ImportBatch.query.filter_by(source_profile_id=profile_id).update(
        {'source_profile_id': None}, synchronize_session=False
    )
    db.session.delete(profile)
    db.session.commit()
    return jsonify({'message': 'Account deleted', 'id': profile_id})


@source_profiles_bp.route('/defaults', methods=['GET'])
@login_required
def defaults():
    """What the settings form should offer, so the UI holds no duplicate list."""
    return jsonify({
        'kinds': list(KINDS),
        'companies': list(COMPANIES),
        'default_bank_exclude_patterns': list(DEFAULT_CARD_AGGREGATE_PATTERNS),
    })
