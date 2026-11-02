"""Normal Lead MCP channel for explicit plan publication, never model tools."""
from copy import deepcopy

from .board_reader import BoardReader, BoardReadError, MAX_CONNECTION_BYTES, _text
from .dev_manifest import canonical


READ_FIELDS = {'get_project': 'project_id', 'get_ticket': 'ticket_id',
               'get_managed_agent_profile': 'profile_id'}
CREATE_FIELDS = frozenset({'project_id', 'title', 'description', 'acceptance_criteria',
    'priority', 'assignee_owner_id', 'assignee_agent_id', 'idempotency_key',
    'depends_on', 'labels', 'required_skills', 'resource'})


class TeamTicketBoardClient(BoardReader):
    """Use the existing paired Lead identity without adding worker capabilities.

    Registration is explicit and non-autonomous. Only create_ticket mutates
    work; there is no claim, update, message, command, model or grant channel.
    """

    def registration(self):
        return super().register()

    def call(self, name, args):
        if type(name) is not str or name not in {*READ_FIELDS, 'create_ticket'}:
            raise BoardReadError('tool_not_allowed')
        if type(args) is not dict:
            raise BoardReadError('invalid_arguments')
        values = deepcopy(args)
        if name in READ_FIELDS:
            field = READ_FIELDS[name]
            if set(values) != {field} or not _text(values[field], 200):
                raise BoardReadError('invalid_arguments')
        elif (set(values) != CREATE_FIELDS
                or any(not _text(values.get(key), 200) for key in
                       ('project_id', 'assignee_owner_id', 'assignee_agent_id', 'idempotency_key'))
                or any(type(values.get(key)) is not str for key in
                       ('title', 'description', 'acceptance_criteria', 'priority', 'resource'))
                or any(type(values.get(key)) is not list for key in
                       ('depends_on', 'labels', 'required_skills'))):
            raise BoardReadError('invalid_arguments')
        if len(canonical(values)) > MAX_CONNECTION_BYTES - 1024:
            raise BoardReadError('invalid_arguments')
        if not self._registered:
            raise BoardReadError('team_ticket_registration_required')
        return self._tool(name, {**values, 'session_id': self.session_id, 'view': 'full'})
