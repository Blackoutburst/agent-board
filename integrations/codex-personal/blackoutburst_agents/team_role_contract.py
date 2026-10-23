"""Closed private role data. No selection, grant, Board mutation or execution."""
from .dev_journal import decode
from .dev_manifest import DevelopmentError,canonical,check_content,opaque


def fail(code='team_role_invalid'):
    raise DevelopmentError(code)


def text(value,maximum):
    if type(value) is not str or not value.strip() or len(value.encode('utf-8'))>maximum:
        fail()
    check_content(value.encode('utf-8'));return value


def manager_slots(value):
    if type(value) is not dict or set(value)!={'schema','slots'} or value['schema']!='team-manager-slots-v1':fail()
    slots=value['slots']
    if type(slots) is not list or not 1<=len(slots)<=16:fail()
    seen={key:set() for key in ('slot_id','resource','agent_id')}
    for slot in slots:
        if type(slot) is not dict or set(slot)!={'slot_id','resource','agent_id','model'}:fail()
        for key in slot:opaque(slot[key])
        for key,used in seen.items():
            if slot[key] in used:fail()
            used.add(slot[key])
    return decode(canonical(value))


def role_result(role,raw,inputs):
    if type(raw) is not str or len(raw.encode('utf-8'))>32768:fail('team_role_output_invalid')
    value=decode(raw.encode('utf-8'))
    if role=='manager':
        if type(value) is not dict or set(value)!={'schema','steps'} or value['schema']!='team-manager-plan-v1':fail()
        steps=value['steps'];catalog={row['slot_id']:row for row in manager_slots(inputs)['slots']}
        if type(steps) is not list or not 1<=len(steps)<=len(catalog):fail()
        identities=set();slots=set()
        for step in steps:
            if type(step) is not dict or set(step)!={'step_id','slot_id','title','acceptance_criteria','depends_on'}:fail()
            opaque(step['step_id']);opaque(step['slot_id']);text(step['title'],200);text(step['acceptance_criteria'],4000)
            if step['step_id'] in identities or step['slot_id'] in slots or step['slot_id'] not in catalog:fail()
            identities.add(step['step_id']);slots.add(step['slot_id'])
            if type(step['depends_on']) is not list or len(step['depends_on'])>16:fail()
            for dependency in step['depends_on']:opaque(dependency)
            if len(set(step['depends_on']))!=len(step['depends_on']):fail()
        pending={step['step_id']:set(step['depends_on']) for step in steps}
        if any(not dependencies<=identities for dependencies in pending.values()):fail()
        while pending:
            ready={key for key,dependencies in pending.items() if not dependencies}
            if not ready:fail('team_manager_cycle_refused')
            pending={key:dependencies-ready for key,dependencies in pending.items() if key not in ready}
    elif role=='writer':
        if type(value) is not dict or set(value)!={'schema','summary','items'} or value['schema']!='team-writer-summary-v1':fail()
        text(value['summary'],12000);items=value['items']
        expected={row['source_id']:row for row in inputs}
        if type(items) is not list or len(items)!=len(expected):fail()
        seen=set()
        for item in items:
            if type(item) is not dict or set(item)!={'source_id','review_operation_id','verdict','text'}:fail()
            source=expected.get(item['source_id'])
            if (source is None or item['source_id'] in seen or item['review_operation_id']!=source['review_operation_id']
                    or item['verdict']!=source['opinion']['verdict']):fail('team_writer_reference_changed')
            seen.add(item['source_id']);text(item['text'],4000)
    else:fail()
    return decode(canonical(value))
