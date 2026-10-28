import os

# Most suites exercise a two-owner board with login enabled; no-login tests opt out explicitly.
os.environ.setdefault("BOARD_OWNERS", '{"blackoutburst": "Blackoutburst", "thescreamer": "TheScreamer"}')
os.environ.setdefault("BOARD_REQUIRE_LOGIN", "1")
os.environ.setdefault("BOARD_FILEBROWSER_MAP", '{"blackout": "blackoutburst", "TheScreamer": "thescreamer"}')
