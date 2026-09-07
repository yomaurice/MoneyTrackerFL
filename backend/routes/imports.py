"""Statement upload, batch history, and undo.

The first feeder into the pipeline: a file the user downloaded from their bank
or card issuer becomes staged rows, gets reconciled against what is already
tracked, and lands in the review queue.

No credentials involved anywhere, which is why this ships before the automated
fetcher: it works for every user, on the free tier, and cannot leak a password
it never had.
"""
import datetime

from flask import Blueprint, g, jsonify, request

from auth import login_required
from models import (
    STAGED_CONFIRMED,
    ImportBatch,
    SourceProfile,
    StagedTransaction,
    db,
)
from services import reconcile
from services.statements import StatementError, parse_statement

imports_bp = Blueprint('imports', __name__, url_prefix='/api')

# Statements are small; anything larger is a mistake or an attack.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = ('.xlsx', '.xlsm', '.csv', '.txt')


def _serialise_batch(batch, profile_name=None):
    return {
        'id': batch.id,
        'source': batch.source,
        'source_profile_id': batch.source_profile_id,
        'source_profile_name': profile_name,
        'window_start': (
            batch.window_start.isoformat() if batch.window_start else None
        ),
        'window_end': batch.window_end.isoformat() if batch.window_end else None,
        'received': batch.received,
        'new': batch.new,
        'matched': batch.matched,
        'proposed': batch.proposed,
        'ambiguous': batch.ambiguous,
        'suppressed': batch.suppressed,
        'status': batch.status,
        'error': batch.error,
        'created_at': batch.created_at.isoformat() if batch.created_at else None,
    }


@imports_bp.route('/import/upload', methods=['POST'])
@login_required
def upload_statement():
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return jsonify({'message': 'Attach a statement file as "file".'}), 400

    filename = upload.filename
    if not filename.lower().endswith(ALLOWED_EXTENSIONS):
        return jsonify({
            'message': 'Upload an .xlsx or .csv export. '
                       'Legacy .xls is not supported -- save as .xlsx first.'
        }), 400

    # Read with one byte of headroom so an oversized file is detected rather
    # than silently truncated into a plausible-looking partial import.
    data = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return jsonify({
            'message': f'That file is larger than '
                       f'{MAX_UPLOAD_BYTES // (1024 * 1024)}MB.'
        }), 413
    if not data:
        return jsonify({'message': 'That file is empty.'}), 400

    profile = None
    profile_id = request.form.get('source_profile_id', type=int)
    if profile_id is not None:
        profile = SourceProfile.query.filter_by(
            id=profile_id, user_id=g.user_id
        ).first()
        if profile is None:
            return jsonify({'message': 'Account not found'}), 404

    # An explicit mapping from the UI wins; otherwise the profile's saved one;
    # otherwise keyword detection.
    mapping = None
    if request.form.get('mapping'):
        import json
        try:
            mapping = {
                field: int(index)
                for field, index in json.loads(request.form['mapping']).items()
            }
        except (ValueError, TypeError, AttributeError):
            return jsonify({'message': 'mapping must be a JSON object of '
                                       'field -> column index'}), 400
    elif profile is not None and profile.column_mapping:
        mapping = {
            field: int(index)
            for field, index in profile.column_mapping.items()
        }

    try:
        parsed = parse_statement(filename, data, mapping)
    except StatementError as exc:
        return jsonify({'message': str(exc)}), 400

    rows = parsed['rows']
    dates = [r['txn_date'] for r in rows if r['txn_date']]

    batch = ImportBatch(
        user_id=g.user_id,
        source='upload',
        source_profile_id=profile.id if profile else None,
        window_start=min(dates) if dates else None,
        window_end=max(dates) if dates else None,
        received=len(rows),
        status='running',
    )
    db.session.add(batch)
    db.session.flush()

    # Rows already seen in a previous import must not be staged again. The
    # unique (user_id, dedup_hash) index is the real guarantee; this check
    # keeps a re-upload from failing the whole request on a constraint error.
    seen = {
        h for (h,) in db.session.query(StagedTransaction.dedup_hash)
        .filter(StagedTransaction.user_id == g.user_id).all()
    }

    staged_rows, duplicates = [], 0
    for row in rows:
        dedup = reconcile.compute_dedup_hash(
            'upload',
            batch.source_profile_id,
            row.get('external_id'),
            row['txn_date'],
            row['amount'],
            row['currency'],
            row.get('merchant_clean') or row.get('merchant_raw'),
        )
        if dedup in seen:
            duplicates += 1
            continue
        seen.add(dedup)

        staged_rows.append(StagedTransaction(
            user_id=g.user_id,
            batch_id=batch.id,
            source_profile_id=batch.source_profile_id,
            source='upload',
            external_id=row.get('external_id'),
            dedup_hash=dedup,
            raw={'cells': row.get('raw'), 'file': filename},
            txn_date=row['txn_date'],
            posted_date=row.get('posted_date'),
            amount=row['amount'],
            currency=row['currency'],
            type=row['type'],
            merchant_raw=row.get('merchant_raw'),
            merchant_clean=row.get('merchant_clean'),
            memo=row.get('memo'),
            issuer_status=row.get('issuer_status'),
            installment_no=row.get('installment_no'),
            installment_total=row.get('installment_total'),
        ))

    db.session.add_all(staged_rows)
    db.session.flush()

    try:
        counts = reconcile.reconcile_batch(
            staged_rows,
            profile_kind=profile.kind if profile else None,
            exclude_patterns=profile.exclude_patterns if profile else None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        batch.status = 'failed'
        batch.error = str(exc)[:500]
        db.session.commit()
        return jsonify({
            'message': 'The file parsed but reconciliation failed.',
            'batch': _serialise_batch(batch),
        }), 500

    batch.new = len(staged_rows)
    batch.matched = counts['matched']
    batch.proposed = counts['proposed']
    batch.ambiguous = counts['ambiguous']
    batch.suppressed = counts['suppressed']
    batch.status = 'done'

    # Remember a mapping that worked, so the next upload needs no confirmation.
    if profile is not None and parsed['mapping']:
        profile.column_mapping = {
            field: int(index) for field, index in parsed['mapping'].items()
        }

    db.session.commit()

    return jsonify({
        'message': f"{counts['proposed']} missing, {counts['matched']} matched",
        'batch': _serialise_batch(batch, profile.name if profile else None),
        'duplicates_skipped': duplicates,
        'unreadable_rows': parsed['skipped'],
        'headers': parsed['headers'],
        'mapping': parsed['mapping'],
        'header_row': parsed['header_row'],
    }), 201


@imports_bp.route('/import/preview', methods=['POST'])
@login_required
def preview_statement():
    """Parse without staging, so the mapping can be confirmed first.

    The column-mapping step needs to show what a file looks like before
    anything is written, otherwise correcting a bad guess means undoing an
    import.
    """
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return jsonify({'message': 'Attach a statement file as "file".'}), 400

    data = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return jsonify({'message': 'That file is too large.'}), 413

    try:
        parsed = parse_statement(upload.filename, data)
    except StatementError as exc:
        return jsonify({'message': str(exc)}), 400

    return jsonify({
        'headers': parsed['headers'],
        'mapping': parsed['mapping'],
        'header_row': parsed['header_row'],
        'row_count': len(parsed['rows']),
        'unreadable_rows': parsed['skipped'],
        'sample': [
            {
                'date': r['txn_date'].isoformat(),
                'amount': r['amount'],
                'currency': r['currency'],
                'type': r['type'],
                'merchant': r.get('merchant_clean') or r.get('merchant_raw'),
            }
            for r in parsed['rows'][:5]
        ],
    })


@imports_bp.route('/import-batches', methods=['GET'])
@login_required
def list_batches():
    batches = (
        ImportBatch.query
        .filter_by(user_id=g.user_id)
        .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        .limit(20)
        .all()
    )

    names = {
        p.id: p.name
        for p in SourceProfile.query.filter_by(user_id=g.user_id).all()
    }
    return jsonify([
        _serialise_batch(b, names.get(b.source_profile_id)) for b in batches
    ])


@imports_bp.route('/import-batches/<int:batch_id>', methods=['DELETE'])
@login_required
def delete_batch(batch_id):
    """Undo an import.

    Only removes staged rows. A row already confirmed has become a real
    transaction the user chose to keep, so it is left alone and reported --
    silently deleting reviewed work would be far worse than a partial undo.
    """
    batch = ImportBatch.query.filter_by(
        id=batch_id, user_id=g.user_id
    ).first()
    if batch is None:
        return jsonify({'message': 'Import not found'}), 404

    confirmed = StagedTransaction.query.filter_by(
        batch_id=batch_id, user_id=g.user_id, state=STAGED_CONFIRMED
    ).count()

    removed = StagedTransaction.query.filter(
        StagedTransaction.batch_id == batch_id,
        StagedTransaction.user_id == g.user_id,
        StagedTransaction.state != STAGED_CONFIRMED,
    ).delete(synchronize_session=False)

    if confirmed:
        # Detach the survivors before removing the batch, or the foreign key
        # blocks the delete. They keep their own provenance -- source, profile
        # and the raw line from the file -- so nothing traceable is lost.
        StagedTransaction.query.filter(
            StagedTransaction.batch_id == batch_id,
            StagedTransaction.user_id == g.user_id,
        ).update({'batch_id': None}, synchronize_session=False)

    db.session.delete(batch)
    db.session.commit()

    return jsonify({
        'message': f'{removed} staged rows removed',
        'removed': removed,
        'kept_confirmed': confirmed,
    })
