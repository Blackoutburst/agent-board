"""Loopback-only conversation layout fixture with disposable accounts."""
import os
import tempfile

os.environ['BOARD_REQUIRE_LOGIN'] = '1'
os.environ.setdefault('BOARD_OWNERS', '{"blackoutburst": "Blackoutburst", "thescreamer": "TheScreamer"}')
os.environ['BOARD_ALLOW_LOCAL_PASSWORDS'] = '1'

from app import create_app
import uvicorn

with tempfile.TemporaryDirectory(prefix='board-conversation-list-ui-') as directory:
    app = create_app(directory, base_url='http://127.0.0.1:18769',
                     filebrowser_url='', allow_local_passwords=True)
    for owner in ('blackoutburst', 'thescreamer'):
        app.state.board.ensure_owner(owner, owner, 'local-ui-fixture-only')
        app.state.board.set_policy({'kind': 'human', 'owner_id': owner}, True, True, True, True, [], [])
    print('Synthetic conversation preview: 127.0.0.1:18769', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=18769, log_level='warning')
