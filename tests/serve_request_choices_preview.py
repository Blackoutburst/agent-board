"""Disposable requests only; no provider, command or real account is used."""
import os
os.environ['BOARD_TEST_BASE_URL'] = 'http://127.0.0.1:18775'
from serve_requests_preview import app, board, human, project
import uvicorn

for key, kind, summary, question, choices, command in [
    ('Choices', 'question', 'Where should this preview go?',
     'Where should this preview go?\n\n' + 'LONG_CONTEXT_SENTINEL with evidence. ' * 300,
     ['Staging first', 'Production now', 'Wait'], ''),
    ('Custom reply', 'question', 'Which colour?', 'Which colour?', ['Blue', 'Green'], ''),
    ('Command', 'action', 'Run this local check', 'Run this local check', [], "printf '%s\\n' 'été 日本語'\n"),
    ('Approval', 'approval', 'Approve this preview?', 'Approve this preview?', [], ''),
    ('Legacy action', 'action', '', 'Check the old command\n\n```sh\nprintf legacy\n```\n\n' + 'Legacy background. ' * 200, [], ''),
]:
    task = board.create_ticket(human, project['id'], key, idempotency_key='choices-' + key)
    board.request_human_input(human, task['id'], 'blackoutburst', question, 'choices-input-' + key,
        kind=kind, summary=summary, choices=choices, command=command)

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=18775, log_level='warning')
