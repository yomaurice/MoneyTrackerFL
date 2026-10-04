"""The notification parser, against the fixtures the frontend twin also uses."""
import json
import os

import pytest

from services import wallet_parse

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures',
                        'wallet_notifications.json')
CASES = json.load(open(FIXTURES, encoding='utf-8'))['cases']


@pytest.mark.parametrize('case', CASES, ids=[c['name'] for c in CASES])
def test_notification(case):
    assert wallet_parse.parse(case['title'], case['text']) == case['expect']
