"""Loopback-only, synthetic two-owner fixture for the Needs you regression."""
import os
import tempfile

os.environ['BOARD_DATA_DIR'] = tempfile.mkdtemp(prefix='board-requests-ui-')
os.environ['BOARD_ALLOW_LOCAL_PASSWORDS'] = '1'
os.environ['BOARD_REQUIRE_LOGIN'] = '1'
os.environ.setdefault('BOARD_OWNERS', '{"blackoutburst": "Blackoutburst", "thescreamer": "TheScreamer"}')
os.environ['BOARD_BASE_URL'] = os.environ.get('BOARD_TEST_BASE_URL', 'http://127.0.0.1:18767')

from app import create_app
import uvicorn

app = create_app(os.environ['BOARD_DATA_DIR'], base_url=os.environ['BOARD_BASE_URL'],
                 filebrowser_url='', allow_local_passwords=True)
board = app.state.board
for owner, name in [('blackoutburst', 'Blackoutburst'), ('thescreamer', 'TheScreamer')]:
    board.ensure_owner(owner, name, 'local-ui-fixture-only')
    board.set_policy({'kind': 'human', 'owner_id': owner}, True, True, True, True, [], [])
human = {'kind': 'human', 'owner_id': 'blackoutburst'}
project = board.create_project(human, 'Request targeting fixture', ['blackoutburst', 'thescreamer'],
                               idempotency_key='requests-project')
for key, title, owner, question in [
    ('first', 'First owner request', 'blackoutburst', 'Question only Blackoutburst can answer'),
    ('second', 'Second owner request', 'thescreamer', 'Question only TheScreamer can answer'),
    ('third', 'Another second owner request', 'thescreamer', 'Another question for TheScreamer'),
]:
    ticket = board.create_ticket(human, project['id'], title, idempotency_key=key)
    board.request_human_input(human, ticket['id'], owner, question, key + '-input')

prerequisite = board.create_ticket(human, project['id'], 'Unfinished prerequisite', idempotency_key='prerequisite')
board.create_ticket(human, project['id'], 'To do waiting for prerequisite', idempotency_key='dependent',
                    depends_on=[prerequisite['id']])

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=18767, log_level='warning')
