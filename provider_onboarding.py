"""Human-only preparation view; provider preferences grant no execution access.

The schema14 manager publishes target/mode metadata, not authenticated provider
identities or model entitlement. This view deliberately reports those facts as
unattested, including when a target is ready or has the requested preferences.
No login, credential inspection, provider discovery or process is performed.
"""
from __future__ import annotations

import json

from server_instances import _opaque, _wire


def provider_catalog():
    """Closed descriptors of implemented code and official personal login paths.

    Return new objects: descriptors are not runtime observations. There is no
    built-in model list, API fallback, browser-login endpoint or hosted SSO.
    """
    return [
        {'id': 'codex_chatgpt', 'name': 'Codex · ChatGPT',
         'billing': 'personal_subscription', 'text_adapter_implemented': True,
         'board_login_implemented': False, 'connection_status': 'not_attested',
         'model_catalog_status': 'not_attested', 'confirmed_models': [],
         'personal_login': 'official_owner_cli',
         'documentation_url': 'https://learn.chatgpt.com/docs/auth'},
        {'id': 'claude_code', 'name': 'Claude Code',
         'billing': 'personal_subscription', 'text_adapter_implemented': False,
         'board_login_implemented': False, 'connection_status': 'not_attested',
         'model_catalog_status': 'not_attested', 'confirmed_models': [],
         'personal_login': 'official_owner_cli',
         'documentation_url': 'https://code.claude.com/docs/en/authentication'},
    ]


def overview(board, actor, profile_id=None):
    """Reuse normal owner/host permissions; expose bounded metadata only.

    A published target attests its selected workflow and exact launch identity,
    not availability of any provider/model. The active host ID is intentionally
    visible to its human owner for initial manager setup; it is not a credential.
    """
    if profile_id is not None:
        _opaque(profile_id)
    with board._connection() as db:
        actor = board._human(db, actor)
        requested = None
        if profile_id is not None:
            profile = board._profile_owned(db, actor, profile_id)
            fields = json.loads(profile['fields'])
            requested = {'profile_id': profile['id'], 'profile_version': profile['version'],
                'name': fields['name'], 'provider': fields['preferred_provider'],
                'model': fields['preferred_model']}
        row = db.execute('SELECT * FROM server_hosts WHERE owner_id=? AND revoked_at IS NULL',
            (actor['owner_id'],)).fetchone()
        host = None
        if row is not None:
            published = board._server_host_view(db, row, instances=True)
            targets = [{key: item[key] for key in ('id', 'profile_id', 'profile_version',
                'mode', 'launchspec_sha256', 'observed_state', 'desired_state',
                'can_start', 'control_reason', 'cleanup_confirmed')}
                for item in published['instances']]
            host = {key: published[key] for key in ('id', 'owner_id', 'name', 'enabled',
                'device_id', 'catalog_version', 'last_seen_at')}
            host['published_targets'] = targets
        registration = 'no_profile' if requested is None else 'not_published'
        if requested is not None and host is not None:
            matched = [item for item in host['published_targets'] if item['profile_id'] == requested['profile_id']]
            if matched:
                registration = ('published' if matched[0]['profile_version'] == requested['profile_version']
                    else 'profile_version_changed')
        return _wire({'schema_version': 1, 'owner_id': actor['owner_id'],
            'providers': provider_catalog(), 'host': host, 'requested': requested,
            'target_registration': registration,
            'connection': {'status': 'not_attested', 'provider_id': None, 'observed_at': None},
            'models': {'catalog_status': 'not_attested', 'access_status': 'not_attested', 'models': []},
            'provisioning': {'board_provisioning_implemented': False,
                'board_login_implemented': False, 'isolated_owner_required': True,
                'api_fallback': False}})
