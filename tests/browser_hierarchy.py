"""Standalone loopback browser recipe on an isolated temporary Board.

Run with Playwright and Chromium installed locally; never targets production.
"""
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright, expect
from app import create_app


def run():
    with tempfile.TemporaryDirectory(prefix='lead80-browser-') as folder:
        app = create_app(Path(folder), base_url='http://127.0.0.1:18789', filebrowser_url='', allow_local_passwords=True)
        board = app.state.board
        board.ensure_owner('blackoutburst', 'Fixture owner', 'fixture-password-only')
        human = dict(kind='human', owner_id='blackoutburst')
        board.set_policy(human, True, True, True, True, [], [])
        profile = board.create_agent_profile(human, 'Hierarchy fixture', project_ids=[])
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=18789, log_level='error'))
        thread = threading.Thread(target=server.run, daemon=True); thread.start()
        try:
            for _ in range(100):
                if server.started: break
                time.sleep(.05)
            assert server.started
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport=dict(width=1280, height=900))
                    errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto('http://127.0.0.1:18789')
                    page.get_by_text('Local development sign-in', exact=True).click()
                    page.locator('[name=username]').fill('blackoutburst')
                    page.locator('[name=password]').fill('fixture-password-only')
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.locator('#agents-nav').click()
                    page.get_by_role('button', name='Délégations', exact=True).click()
                    page.get_by_role('button', name='Configurer les plafonds', exact=True).click()
                    dialog = page.locator('#modal')
                    dialog.locator('[name=delegations_live]').fill('2')
                    dialog.locator('[name=delegations_created]').fill('2')
                    dialog.locator('[name=confirm_limits]').check()
                    dialog.get_by_role('button', name='Enregistrer les plafonds', exact=True).click()
                    expect(dialog).not_to_be_visible()
                    page.get_by_role('button', name='Accorder une délégation racine', exact=True).click()
                    dialog.locator('[name=beneficiary_id]').select_option(profile['id'])
                    dialog.locator('[name=confirm_limits]').check()
                    dialog.get_by_role('button', name='Accorder la délégation', exact=True).click()
                    expect(dialog).not_to_be_visible()
                    expect(page.get_by_role('button', name='Réduire les droits', exact=True)).to_be_visible()
                    page.get_by_role('button', name='Réduire les droits', exact=True).click()
                    dialog.locator('[name=confirm_limits]').check()
                    dialog.get_by_role('button', name='Réduire les droits', exact=True).click()
                    expect(dialog).not_to_be_visible()
                    page.get_by_role('button', name='Révoquer', exact=True).click()
                    dialog.get_by_role('button', name='Confirmer la révocation', exact=True).click()
                    expect(dialog).not_to_be_visible()
                    expect(page.get_by_text('État enregistré : revoked · Version : 3', exact=True)).to_be_visible()
                    # Prepare a second chain through normal fixture-only APIs.
                    from hierarchy_store import HierarchyStore
                    from test_hierarchy_policy import policy, child
                    store = HierarchyStore(board)
                    ceiling = policy(project_ids=[], actions=[], resources=[])
                    store.configure(human, 1, ceiling, 'archive-fixture-limits')
                    creator = board.create_agent_profile(human, 'Archive creator', project_ids=[], policy=dict(
                        allow_read=True, allow_messages=False, allow_requests=True,
                        actions=[], resources=[], autonomy='queue'))
                    device = board.create_device(human, 'Archive fixture', [])
                    board.bind_agent_profile(human, creator['id'], creator['version'], device['id'], 'archive-creator', 'codex')
                    raw = board.authenticate(device['token'])
                    board.register_agent(raw, 'archive-creator', 'Archive fixture', [], 'codex', True, 'archive-creator')
                    root = store.create_root(human, creator['id'], ceiling, 'archive-fixture-root')
                    created = store.create_project_lead(board.actor_session(raw, 'archive-creator'), root['id'],
                        'Archive project', 'Archive Lead', child(project_ids=[], actions=[], resources=[]), 1, 'archive-fixture-project')
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    page.get_by_role('button', name='Archiver projet et Lead', exact=True).click()
                    expect(dialog.get_by_text('La restauration sans nouvelle comptabilisation sera refusée.', exact=False)).to_be_visible()
                    dialog.get_by_role('button', name='Confirmer l’archivage', exact=True).click()
                    expect(dialog).not_to_be_visible()
                    expect(page.get_by_text('Archivage projet et Lead confirmé ; historique conservé.', exact=True)).to_be_visible()
                    expect(page.get_by_role('button', name='Archiver projet et Lead', exact=True)).to_have_count(0)
                    assert board.get_project(human, created['project_id'])['archived']
                    assert board.get_agent_profile(human, created['profile_id'])['status'] == 'archived'
                    assert store.project_archive_preview(human, created['project_id'])['released']
                    limits = store.list_for_owner(human)['limits']['counters']
                    assert limits['projects'] == dict(live=0, created=1)
                    page.screenshot(path='/tmp/lead80-browser82-desktop.png', full_page=True)
                    page.set_viewport_size(dict(width=390, height=844))
                    page.screenshot(path='/tmp/lead80-browser82-mobile.png', full_page=True)
                    assert not errors, errors
                    print('PASS browser: login, limits, explicit root, reduction, revoke, retained history, project/Lead archive and quota conservation; no JS errors')
                except Exception:
                    if 'page' in locals():
                        page.screenshot(path='/tmp/lead80-browser82-failure.png', full_page=True)
                        print(page.locator('body').inner_text()[-2500:])
                    raise
                finally:
                    browser.close()
        finally:
            server.should_exit = True; thread.join(timeout=10)


if __name__ == '__main__':
    run()
