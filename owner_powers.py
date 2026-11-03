"""Fixed owner powers for actors already resolved by Board._actor.

Caller-supplied markers are not authentication. Board._actor discards them and
derives companion_for from the live, bound SQL profile before using this helper.
"""

OWNER_POWERS = frozenset({'dispatch', 'answer_requests', 'accept_reviews', 'manage_agents'})


def has_owner_power(actor, power, owner_id=None):
    if not isinstance(actor, dict) or not isinstance(power, str) or power not in OWNER_POWERS:
        return False
    owner = actor.get('owner_id')
    if not isinstance(owner, str) or not owner or owner_id is not None and owner_id != owner:
        return False
    return actor.get('kind') == 'human' or (
        actor.get('kind') == 'agent' and actor.get('companion_for') == owner)
