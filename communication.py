"""Addressed conversation delivery, focused inboxes, and reusable project labels."""
import json
import re
import time

from projects import _fail, _text


def migrate_communication(db):
    db.execute('''CREATE TABLE project_labels(project_id TEXT NOT NULL REFERENCES projects(id),
        name TEXT NOT NULL COLLATE NOCASE,color TEXT NOT NULL DEFAULT '#8250df',
        PRIMARY KEY(project_id,name))''')
    db.execute("INSERT INTO project_labels SELECT id,'feature','#238636' FROM projects")
    db.execute("INSERT INTO project_labels SELECT id,'bugfix','#da3633' FROM projects")
    db.execute('''INSERT OR IGNORE INTO project_labels(project_id,name)
        SELECT t.project_id,j.value FROM tickets t,json_each(t.labels) j''')


class CommunicationMixin:
    def _mentionables(self, db, thread_id):
        owners=[dict(row) for row in db.execute('''SELECT o.id,o.display_name FROM owners o
            JOIN thread_members m ON m.owner_id=o.id WHERE m.thread_id=? ORDER BY o.display_name''',(thread_id,))]
        result=[{'id':o['id'],'name':o['display_name'],'kind':'human','owner_id':o['id']} for o in owners]
        project=db.execute('SELECT project_id FROM tickets WHERE thread_id=?',(thread_id,)).fetchone()
        for row in db.execute('''SELECT p.* FROM agent_profiles p JOIN thread_members m ON m.owner_id=p.owner_id
            WHERE m.thread_id=? AND p.status='active' ORDER BY p.created_at,p.id''',(thread_id,)):
            selected=json.loads(row['project_ids'])
            if selected is not None and (not project or project['project_id'] not in selected):
                continue
            result.append({'id':'agent:'+row['id'],'agent_id':row['id'],'name':json.loads(row['fields'])['name'],
                           'kind':'agent','owner_id':row['owner_id']})
        return result

    def list_mention_targets(self, actor, thread_id):
        with self._connection() as db:
            actor=self._actor(db,actor,'allow_read')
            self._member(db,actor,thread_id)
            return self._mentionables(db,thread_id)

    def _resolve_mentions(self, db, actor, thread_id, body, mentions, reply_to):
        candidates=self._mentionables(db,thread_id)
        by_id={item['id']:item for item in candidates}
        if any(identifier not in by_id for identifier in mentions):
            _fail('Mention a human or active agent who can access this conversation')
        result=list(mentions)
        matches={}
        for item in candidates:
            for match in re.finditer(r'(?<![\w@])@'+re.escape(item['name'])+r'(?!\w)',body,re.IGNORECASE):
                matches.setdefault((match.start(),match.end()),[]).append(item['id'])
        occupied=[]
        for (start,end),identifiers in sorted(matches.items(),key=lambda item:-(item[0][1]-item[0][0])):
            if any(start<right and end>left for left,right in occupied):continue
            occupied.append((start,end))
            if len(identifiers)>1 and not set(identifiers).intersection(result):
                _fail('This mention name is ambiguous; select its specific recipient')
            if len(identifiers)==1:
                result.extend(identifiers)
        if reply_to:
            original=db.execute('SELECT author FROM messages WHERE id=? AND thread_id=?',(reply_to,thread_id)).fetchone()
            if original:
                author=json.loads(original['author'])
                target='agent:'+author['agent_id'] if author.get('agent_id') else author.get('owner_id')
                current=self._author(db,actor)
                same=(author.get('agent_id')==current.get('agent_id')) if author.get('agent_id') else (current['kind']=='human' and current['owner_id']==target)
                if not same and target in by_id:
                    result.append(target)
        return list(dict.fromkeys(result))

    def _mention_details(self, db, references):
        result=[]
        for ref in references:
            if ref.startswith('agent:'):
                row=db.execute('SELECT fields,owner_id FROM agent_profiles WHERE id=?',(ref[6:],)).fetchone()
                if row:result.append({'id':ref,'name':json.loads(row['fields'])['name'],'kind':'agent','owner_id':row['owner_id']})
            else:
                row=db.execute('SELECT display_name FROM owners WHERE id=?',(ref,)).fetchone()
                if row:result.append({'id':ref,'name':row['display_name'],'kind':'human','owner_id':ref})
        return result

    def _inbox_targets(self, db, actor):
        if actor['kind']=='human':return [actor['owner_id']]
        row=db.execute('SELECT agent_id,agent_kind FROM sessions WHERE id=?',(actor['session_id'],)).fetchone()
        if not row or not row['agent_id']:return [actor['owner_id']]
        return ['agent:'+row['agent_id']]+([actor['owner_id']] if row['agent_kind']=='agent' else [])

    def _addressed_messages(self, db, actor, *, boundary=0, unread=False, limit=200):
        targets=self._inbox_targets(db,actor)
        placeholders=','.join('?' for _ in targets)
        rows=db.execute(f'''SELECT x.* FROM messages x JOIN thread_members m ON m.thread_id=x.thread_id
            WHERE m.owner_id=? AND x.seq>? AND EXISTS(SELECT 1 FROM json_each(x.mentions) j WHERE j.value IN ({placeholders}))
            AND (?=0 OR x.seq>COALESCE((SELECT r.seq FROM read_state r WHERE r.owner_id=? AND r.thread_id=x.thread_id),0))
            ORDER BY x.seq''',(actor['owner_id'],boundary,*targets,int(unread),actor['owner_id']))
        selected=[]
        for row in rows:
            if not self._profile_thread_allowed(db,actor,row['thread_id']):continue
            author=json.loads(row['author'])
            if actor['kind']=='human' and author['kind']=='human' and author['owner_id']==actor['owner_id']:continue
            selected.append(row)
            if limit and len(selected)>=limit:break
        return selected

    def inbox_snapshot(self, actor):
        with self._connection() as db:
            actor=self._human(db,actor)
            rows=self._addressed_messages(db,actor,unread=True,limit=None)
            tickets=[{**self._ticket(row,db),'project_name':row['project_name']} for row in db.execute('''SELECT t.*,p.name AS project_name FROM tickets t
                JOIN projects p ON p.id=t.project_id JOIN project_members m ON m.project_id=p.id
                LEFT JOIN agent_profiles a ON a.id=t.assignee_agent_id
                WHERE m.owner_id=? AND p.archived=0 AND t.status!='done'
                AND (t.assignee_owner_id=? OR a.owner_id=? OR json_extract(t.claimant,'$.owner_id')=?)
                ORDER BY t.updated_at DESC''',(actor['owner_id'],)*4)]
            legacy=[self._task(db,row) for row in db.execute('''SELECT t.* FROM tasks t
                JOIN thread_members m ON m.thread_id=t.thread_id WHERE m.owner_id=? AND t.status!='complete'
                AND t.destination_kind IN ('human','group') AND t.destination_id=?''',(actor['owner_id'],)*2)]
            return {'messages':[self._message(db,row) for row in reversed(rows[-200:])], 'tickets':tickets,'tasks':legacy,
                    'unread_count':len(rows),'task_count':len(tickets)+len(legacy),'count':len(rows)+len(tickets)+len(legacy)}

    def list_labels(self, actor, project_id):
        with self._connection() as db:
            actor=self._actor(db,actor,'allow_read')
            self._project_member(db,actor,project_id)
            result=[dict(row) for row in db.execute('SELECT name,color FROM project_labels WHERE project_id=? ORDER BY name',(project_id,))]
            names={item['name'].lower() for item in result}
            return result+[{'name':name,'color':color} for name,color in [('feature','#238636'),('bugfix','#da3633')] if name not in names]

    def create_label(self, actor, project_id, name, color='#8250df'):
        name=_text(name,'name',1,80).strip()
        if not name or not re.fullmatch(r'#[0-9a-fA-F]{6}',color):_fail('Supply a tag name and a six-digit hex color')
        with self._tx() as db:
            actor=self._actor(db,actor,'allow_requests')
            project=self._project_member(db,actor,project_id)
            if project['archived']:_fail('Restore the project before creating tags',409)
            db.execute('INSERT OR IGNORE INTO project_labels VALUES(?,?,?)',(project_id,name,color.lower()))
            return dict(db.execute('SELECT name,color FROM project_labels WHERE project_id=? AND name=?',(project_id,name)).fetchone())

    def _remember_labels(self, db, project_id, labels):
        db.executemany('INSERT OR IGNORE INTO project_labels(project_id,name) VALUES(?,?)',[(project_id,name) for name in labels])
