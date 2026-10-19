"""Pure validation of a fresh fixed server frame, never a reusable grant."""
from copy import deepcopy
import json

from .board_reader import BoardReadError
from .dev_manifest import DevelopmentError

SCHEMA = 'blackoutburst-development-frame-v1'
LEGACY = 'legacy-sequential-v1'
FRAME = 'development-frame-v1'
MODES = (LEGACY, FRAME)
MAX_DATA_BYTES = 384 * 1024


def read_protocol(value):
    if type(value) is not str or value not in MODES:
        raise DevelopmentError('development_frame_invalid_protocol')
    return value


def parse_frame(value, *, scope, manifest, parent_scope, parent_manifest, family_id, terminal):
    try:
        if (type(value) is not dict or set(value) != {'schema', 'selection', 'selected', 'parent', 'family'}
                or value['schema'] != SCHEMA or type(terminal) is not bool):
            raise ValueError
        expected = {key: manifest['scope'][key] for key in ('owner_id', 'agent_id', 'device_id', 'profile_version')}
        if any(parent_manifest['scope'][key] != item for key, item in expected.items()):
            raise ValueError
        expected.update(project_id=scope['project_id'], ticket_id=scope['ticket_id'],
            session_id=scope['board_session_id'], family_id=family_id,
            parent_ticket_id=parent_scope['ticket_id'], parent_session_id=parent_scope['board_session_id'],
            terminal=terminal)
        selection = value['selection']
        if (type(selection) is not dict or set(selection) != set(expected)
                or any(type(selection[key]) is not type(item) or selection[key] != item for key, item in expected.items())):
            raise ValueError
        for role in ('selected', 'parent'):
            selected = value[role]
            if (type(selected) is not dict or set(selected) != {'policy_first', 'first', 'pages', 'last', 'policy_last'}
                    or any(type(selected[key]) is not dict for key in ('policy_first', 'first', 'last', 'policy_last'))
                    or type(selected['pages']) is not list or not 1 <= len(selected['pages']) <= 3):
                raise ValueError
        if (type(value['family']) is not dict or set(value['family']) != {'first', 'last'}
                or any(type(item) is not dict for item in value['family'].values())):
            raise ValueError
        encoded = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
        if len(encoded) > MAX_DATA_BYTES:
            raise ValueError
        # Reject lone surrogates even though ASCII JSON could escape them.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8', 'strict')
        return deepcopy(value)
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise BoardReadError('development_frame_invalid_response') from None
