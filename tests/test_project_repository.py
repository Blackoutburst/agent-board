"""Project repository addresses: HTTPS, SSH remotes and absolute local paths."""
import pytest


def test_repository_accepts_local_paths_and_ssh_but_not_other_schemes():
    from projects import _repository
    from board import BoardError
    for value in ("https://github.com/o/r", "git@github.com:o/r.git", "ssh://git@host/o/r",
                  "/Users/me/My Repos/app", "C:\\code\\app"):
        assert _repository(value) == value
    for value in ("http://host/o/r", "javascript:alert(1)", "https://user:pass@host/r", "file:///etc", "/a\tb"):
        with pytest.raises(BoardError):
            _repository(value)
