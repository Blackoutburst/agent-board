"""Closed worker transport; human root grants are deliberately absent."""
from hierarchy_policy import exact_string
from hierarchy_store import HierarchyStore
from projects import _fail, _version

FIELDS = {
    'list_hierarchy_delegations': set(),
    'inspect_hierarchy': {'delegation_id'},
    'delegate_hierarchy_project': {'parent_id', 'beneficiary_id', 'policy', 'expected_version', 'idempotency_key'},
    'create_hierarchy_project_lead': {'parent_id', 'name', 'lead_name', 'policy', 'expected_version', 'idempotency_key'},
    'create_hierarchy_team_profile': {'parent_id', 'name', 'role', 'policy', 'expected_version', 'idempotency_key'},
    'update_hierarchy_team_profile': {'parent_id', 'profile_id', 'changes', 'expected_version', 'idempotency_key'},
    'archive_hierarchy_team_profile': {'parent_id', 'profile_id', 'expected_version', 'idempotency_key'},
}
HIERARCHY_TOOLS = frozenset(FIELDS)


def validate_hierarchy_arguments(name, args):
    required = FIELDS[name] | {'session_id'}
    optional = {'limit', 'after'} if name == 'list_hierarchy_delegations' else set()
    if type(args) is not dict or not required <= set(args) or set(args) - required - optional:
        _fail('Supply only hierarchy tool fields')
    for key, value in args.items():
        if key == 'limit':
            if type(value) is not int or not 1 <= value <= 50:
                _fail('hierarchy_page_invalid')
        elif key == 'after' and value is None:
            continue
        elif key == 'expected_version':
            _version(value)
        elif key in ('policy', 'changes'):
            if type(value) is not dict:
                _fail('Hierarchy policy and changes must be JSON objects')
        else:
            exact_string(value)


def mount_hierarchy_tools(mcp, board, session_actor):
    store = HierarchyStore(board)

    @mcp.tool()
    async def list_hierarchy_delegations(session_id: str, limit: int = 20, after: str | None = None) -> dict:
        """Discover only your own currently authorized hierarchy grants and effective ceilings. No grants means an empty page, never implicit authority. Follow next_after even after an empty page: revoked or out-of-scope grants are skipped within bounded scans. Current session/profile/ancestor permissions are rechecked on each page. No work is claimed."""
        return store.list_for_agent(session_actor(session_id), limit, after)

    @mcp.tool()
    async def inspect_hierarchy(session_id: str, delegation_id: str) -> dict:
        """Read your explicit delegation, current ceilings and ancestry. Requires current read permission. Roles grant nothing; no owner-wide history or connection information is returned."""
        return store.inspect(session_actor(session_id), delegation_id)

    @mcp.tool()
    async def delegate_hierarchy_project(session_id: str, parent_id: str, beneficiary_id: str,
                                         policy: dict, expected_version: int, idempotency_key: str) -> dict:
        """Delegate one project to an existing ordinary profile under your current chain. Requires explicit delegate_project grant; all ancestor quotas and depth apply. Cannot create a root, companion, connection or Runtime."""
        return store.create_child(session_actor(session_id), parent_id, beneficiary_id, policy, expected_version, idempotency_key)

    @mcp.tool()
    async def create_hierarchy_project_lead(session_id: str, parent_id: str, name: str, lead_name: str,
                                            policy: dict, expected_version: int, idempotency_key: str) -> dict:
        """Atomically create one project and its unbound ordinary Lead under an explicit create_project_lead grant. Supply an empty project_ids list; the server binds the new singleton. No device, provider, session or execution is created."""
        return store.create_project_lead(session_actor(session_id), parent_id, name, lead_name, policy, expected_version, idempotency_key)

    @mcp.tool()
    async def create_hierarchy_team_profile(session_id: str, parent_id: str, name: str, role: str,
                                            policy: dict, expected_version: int, idempotency_key: str) -> dict:
        """Create one unbound ordinary team leaf in your singleton project under create_team_profile. Role is descriptive. Current authority, quotas and immutable provenance are checked transactionally; no connection or execution starts."""
        return store.create_team_profile(session_actor(session_id), parent_id, name, role, policy, expected_version, idempotency_key)

    @mcp.tool()
    async def update_hierarchy_team_profile(session_id: str, parent_id: str, profile_id: str,
                                            changes: dict, expected_version: int, idempotency_key: str) -> dict:
        """Edit presentation or active/paused state of your direct team leaf under manage_team_profile. Cannot change policy, scope, kind, binding or archive through changes. Current target scope and CAS are enforced."""
        return store.update_team_profile(session_actor(session_id), parent_id, profile_id, expected_version, changes, idempotency_key)

    @mcp.tool()
    async def archive_hierarchy_team_profile(session_id: str, parent_id: str, profile_id: str,
                                             expected_version: int, idempotency_key: str) -> dict:
        """Archive your direct ordinary leaf only after claims, Runtime and families are durably clear. Requires current manage_team_profile and target scope. Releases live quota once; cumulative quota and historical work remain. Lease expiry is not cleanup."""
        return store.archive_team_profile(session_actor(session_id), parent_id, profile_id, expected_version, idempotency_key)
