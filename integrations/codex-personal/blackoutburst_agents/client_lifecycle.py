"""Explicit ownership of board clients, including narrow control channels."""
from contextlib import contextmanager, ExitStack


def own_client(resources, client):
    """Close an acquired client with its owner; allow older close-less fakes."""
    close = getattr(client, 'close', None)
    if callable(close):
        resources.callback(close)
    return client


@contextmanager
def managed_client(client):
    with ExitStack() as resources:
        yield own_client(resources, client)
