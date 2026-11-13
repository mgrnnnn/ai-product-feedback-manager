"""Read-only source gateway contract. No source mutation method exists."""
from datetime import datetime, timedelta
from urllib.parse import urlencode
from .db import now
from .security import DomainError


class SourceConnector:
    def __init__(self, manager, http, actor_id='source', name='tickets'):
        self.manager, self.http, self.actor_id, self.name = manager, http, actor_id, name

    def poll(self):
        with self.manager.db.connect() as conn:
            row=conn.execute('SELECT watermark FROM checkpoints WHERE name=?',(self.name,)).fetchone()
        watermark=row[0] if row else '1970-01-01T00:00:00+00:00'
        since=(datetime.fromisoformat(watermark)-timedelta(minutes=10)).isoformat()
        until=now()
        cursor=''
        seen=set()
        count=0
        while True:
            page=self.http.request('/records?'+urlencode({'updated_since':since,'updated_until':until,'cursor':cursor,'limit':500}))
            if not isinstance(page.get('records'),list):
                raise DomainError('Неверный контракт источника')
            result=self.manager.ingest(self.manager.actor(self.actor_id),page['records'])
            count+=result['accepted']
            cursor=page.get('next_cursor')
            if not cursor:
                break
            if not isinstance(cursor,str) or cursor in seen or len(seen)>10000:
                raise DomainError('Некорректная пагинация источника')
            seen.add(cursor)
        # Only move the watermark after ALL pages are durably enqueued.
        with self.manager.db.transaction() as conn:
            conn.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?)',(self.name,until))
        return count
