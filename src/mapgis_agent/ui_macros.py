"""State-verified macros for the existing owned MapGIS private desktop."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import time

from .core import BridgeError, read_native
from .native_ui_core import digest, save
from .raster import file_sha


OPERATIONS = {'set_text', 'button', 'command', 'set_check', 'select', 'pointer', 'key', 'capture', 'tree_items', 'tree'}
EXACT = {'hwnd', 'class', 'id', 'text', 'visible', 'enabled', 'check_state', 'selected_index', 'item_count'}


def _matches(window, selector):
    if not isinstance(selector, dict) or set(selector)-EXACT-{'title_regex', 'text_regex'}:
        raise BridgeError('UI_MACRO_SELECTOR')
    for key, expected in selector.items():
        if key in {'title_regex', 'text_regex'}:
            if not isinstance(expected, str) or len(expected) > 250:
                raise BridgeError('UI_MACRO_SELECTOR_REGEX')
            if re.search(expected, str(window.get('text', ''))) is None:
                return False
        elif window.get(key) != expected:
            return False
    return True


def _targets(snapshot, selector):
    if not isinstance(selector, dict) or not set(selector) <= {'window', 'control'}:
        raise BridgeError('UI_MACRO_SELECTOR: select a window and optional control')
    selected = [window for window in snapshot['windows'] if _matches(window, selector.get('window', {}))]
    if 'control' in selector:
        return [child for window in selected for child in window.get('children', []) if _matches(child, selector['control'])]
    return selected


def _satisfied(snapshot, conditions, before=None):
    for rule in conditions:
        if rule == {'changed': True}:
            if before is not None and digest(before) == digest(snapshot):
                return False
            continue
        if not isinstance(rule, dict) or set(rule)-{'window', 'control', 'present'} or type(rule.get('present', True)) is not bool:
            raise BridgeError('UI_MACRO_CONDITION')
        selector = {key: value for key, value in rule.items() if key != 'present'}
        if bool(_targets(snapshot, selector)) != rule.get('present', True):
            return False
    return True


class VerifiedUIMacroRunner:
    def __init__(self, bridge, session_id, *, control=None):
        from .private_control import private_control
        self.bridge, self.session_id = bridge, bridge.request_id(session_id)
        self.control = control or private_control

    def _call(self, verb, **parameters):
        return self.control(self.bridge, verb, session_id=self.session_id, **parameters)

    def _files(self, checks):
        from .private_control import _relative
        if not checks:
            return []
        bindings = self._call('status')['bindings']
        result = []
        for item in checks:
            if not isinstance(item, dict) or set(item)-{'binding', 'member', 'reader', 'expected_sha256'}:
                raise BridgeError('UI_MACRO_ARTIFACT_CHECK')
            binding = bindings.get(item.get('binding'))
            if not binding or binding['role'] != 'output':
                raise BridgeError('UI_MACRO_OUTPUT_BINDING')
            root = self.bridge.destination_path(binding['file'])
            path = root if 'member' not in item else self.bridge.destination_path(root/_relative(item['member']))
            if not path.is_relative_to(root) or item.get('reader') not in (None, 'read_native', 'native_file_info'):
                raise BridgeError('UI_MACRO_ARTIFACT_PATH')
            result.append((path, item, file_sha(path) if path.is_file() else None))
        return result

    def _fresh_file(self, item):
        path, check, before = item
        if not path.is_file() or not path.stat().st_size:
            return None
        after = file_sha(path)
        if 'expected_sha256' in check:
            return after if after == check['expected_sha256'] else None
        return after if after != before else None

    def run(self, steps, artifact_checks=(), *, request_id, timeout_seconds=4):
        request_id = self.bridge.request_id(request_id)
        if (not isinstance(steps, list) or not 1 <= len(steps) <= 100
                or not isinstance(artifact_checks, (list, tuple)) or len(artifact_checks) > 30
                or type(timeout_seconds) not in (float, int) or not 0 < timeout_seconds <= 30):
            raise BridgeError('UI_MACRO_PARAMETERS')
        files = self._files(artifact_checks)
        folder = self.bridge.destination_path(self.bridge.root/'work/private-controls'/digest(self.session_id)[:24])
        output = folder/'macros'/f'{request_id}.json'
        fingerprint = digest({'steps': steps, 'artifact_checks': list(artifact_checks), 'timeout_seconds': timeout_seconds})
        result = {'id': request_id, 'session_id': self.session_id, 'macro_sha256': fingerprint,
                  'state': 'VERIFICATION_FAILED', 'steps': [], 'artifacts': []}
        if output.is_file():
            import json
            prior = json.loads(output.read_text(encoding='utf-8'))
            if prior['macro_sha256'] != fingerprint:
                raise BridgeError('UI_MACRO_REQUEST_CONFLICT')
            if prior['state'] == 'VERIFIED' and any(not Path(a['file']).is_file() or file_sha(Path(a['file'])) != a['sha256']
                                                    for a in prior['artifacts']):
                return {**prior, 'state': 'VERIFICATION_FAILED', 'error': 'ARTIFACT_CHANGED'}
            return {**prior, 'reconciled': True}
        output.parent.mkdir(parents=True, exist_ok=True)
        prefix = 'macro-' + hashlib.sha256(request_id.encode()).hexdigest()[:26]
        # A retry after process loss reconciles the intent. It never repeats a
        # possibly posted native command with a fresh low-level request ID.
        try:
            with output.open('x', encoding='utf-8') as ledger:
                from .native_ui_core import encoded
                ledger.write(encoded({**result, 'error': 'MACRO_PENDING_RECONCILE'}))
        except FileExistsError:
            return self.run(steps, artifact_checks, request_id=request_id, timeout_seconds=timeout_seconds)
        try:
            for index, spec in enumerate(steps):
                if not isinstance(spec, dict) or set(spec)-{'preconditions', 'action', 'postconditions'}:
                    raise BridgeError('UI_MACRO_STEP')
                before_rules = spec.get('preconditions', [])
                after_rules = spec.get('postconditions', [])
                action = spec.get('action')
                if (not isinstance(before_rules, list) or not isinstance(after_rules, list) or not isinstance(action, dict)
                        or action.get('operation') not in OPERATIONS):
                    raise BridgeError('UI_MACRO_STEP')
                if not after_rules and (index != len(steps)-1 or not files):
                    raise BridgeError('POSTED_NOT_COMPLETION_PROOF: each step needs a verified postcondition')
                sequence = f'{prefix}-{index}'
                before = self._call('observe', request_id=sequence+'-before')
                if before.get('state') != 'OBSERVED' or not _satisfied(before['snapshot'], before_rules):
                    raise BridgeError('UI_MACRO_PRECONDITION')
                params = dict(action)
                if 'target' in params:
                    targets = _targets(before['snapshot'], params.pop('target'))
                    if len(targets) != 1 or 'hwnd' in params:
                        raise BridgeError('UI_MACRO_TARGET_AMBIGUOUS')
                    params['hwnd'] = targets[0]['hwnd']
                receipt = self._call('act', request_id=sequence+'-act', token=before['token'], parameters=params)
                event = {'index': index, 'before_sha256': before['snapshot_sha256'], 'action': receipt}
                result['steps'].append(event)
                if receipt.get('state') in {None, 'REJECTED', 'UNKNOWN'}:
                    raise BridgeError(receipt.get('error', 'UI_MACRO_ACTION_UNCONFIRMED'))
                deadline = time.monotonic()+timeout_seconds
                poll = 0
                while True:
                    after = self._call('observe', request_id=f'{sequence}-after-{poll}')
                    if (after.get('state') == 'OBSERVED' and _satisfied(after['snapshot'], after_rules, before['snapshot'])
                            and (after_rules or all(self._fresh_file(item) for item in files))):
                        event['after_sha256'] = after['snapshot_sha256']
                        break
                    if time.monotonic() >= deadline:
                        raise BridgeError('POSTED_NOT_COMPLETION_PROOF: postcondition failed')
                    poll += 1
                    time.sleep(.1)
            for item in files:
                path, check, _ = item
                fingerprint = self._fresh_file(item)
                if fingerprint is None:
                    raise BridgeError('UI_MACRO_ARTIFACT_UNVERIFIED: '+path.name)
                record = {'file': str(path), 'sha256': fingerprint, 'bytes': path.stat().st_size}
                if check.get('reader') == 'read_native':
                    _, reader, _ = read_native(path)
                    record['read_native'] = {'type': reader.shapeType, 'active': len(reader.geom)}
                elif check.get('reader') == 'native_file_info':
                    from .native_backend import native_file_info
                    record['native_reopen'] = native_file_info(self.bridge.root, str(path), fingerprint)
                if file_sha(path) != fingerprint:
                    raise BridgeError('UI_MACRO_ARTIFACT_CHANGED')
                result['artifacts'].append(record)
            result['state'] = 'VERIFIED'
        except (OSError, RuntimeError, ValueError, re.error) as error:
            result['error'] = str(error)
        save(output, result)
        return result
