import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from .db import audit, dumps, now, uid
from .models import DemoModel, Preprocessor, TYPES, validate_extraction
from .security import DomainError, mask, mask_values, require_role

PIPELINE = '1.0'


def timestamp(value):
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if not date.tzinfo:
            raise ValueError()
        return date.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError):
        raise DomainError('Нужна дата ISO 8601 с часовым поясом')


def allowed(actor, topic):
    return topic['product'] in actor['products'] and actor['id'] in json.loads(topic['acl'])


class Manager:
    def __init__(self, db, model=None, preprocessor=None, tracker=None, shadow=True):
        self.db = db
        self.model = model or DemoModel()
        self.preprocessor = preprocessor or Preprocessor()
        self.tracker = tracker
        self.shadow = shadow

    def actor(self, user_id):
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        if not row:
            raise DomainError('Требуется авторизация', 401)
        result = dict(row)
        result['products'] = json.loads(result['products'])
        return result

    def topic(self, conn, actor, topic_id):
        topic = conn.execute('SELECT * FROM topics WHERE id=?', (topic_id,)).fetchone()
        if not topic or not allowed(actor, topic):
            raise DomainError('Тема недоступна', 404)
        live = conn.execute('''SELECT 1 FROM topic_evidence te JOIN evidence e ON e.id=te.evidence_id
            JOIN sources s ON s.id=e.source_id WHERE te.topic_id=? AND e.active=1
            AND s.deleted=0 AND s.expires_at>? LIMIT 1''', (topic_id, now())).fetchone()
        if not live:
            raise DomainError('Нет доступных подтверждений', 404)
        return topic

    def invalidate(self, conn, topic_id):
        topic = conn.execute('SELECT * FROM topics WHERE id=?', (topic_id,)).fetchone()
        times = conn.execute('''SELECT MIN(s.created_at), MAX(s.created_at), COUNT(DISTINCT s.id)
          FROM topic_evidence te JOIN evidence e ON e.id=te.evidence_id
          JOIN sources s ON s.id=e.source_id
          WHERE te.topic_id=? AND e.active=1 AND s.deleted=0''', (topic_id,)).fetchone()
        linked = conn.execute('SELECT 1 FROM backlog WHERE topic_id=? AND external_issue_id IS NOT NULL', (topic_id,)).fetchone()
        state = 'empty' if not times[2] else ('backlog' if linked else 'review')
        conn.execute('''UPDATE topics SET first_seen=?,last_seen=?,revision=revision+1,
                     status=?,approved_revision=NULL,approved_by=NULL WHERE id=?''',
                     (times[0], times[1], state, topic_id))
        conn.execute("UPDATE backlog SET sync_status='cancelled' WHERE topic_id=? AND external_issue_id IS NULL", (topic_id,))

    def deactivate(self, conn, source_id):
        topics = conn.execute('''SELECT DISTINCT te.topic_id FROM topic_evidence te
            JOIN evidence e ON e.id=te.evidence_id WHERE e.source_id=? AND e.active=1''', (source_id,)).fetchall()
        conn.execute('UPDATE evidence SET active=0 WHERE source_id=?', (source_id,))
        for topic in topics:
            self.invalidate(conn, topic[0])
            conn.execute("UPDATE reviews SET reason='' WHERE topic_id=?", (topic[0],))
            remaining = conn.execute('''SELECT e.extraction FROM evidence e JOIN topic_evidence te
                ON te.evidence_id=e.id WHERE te.topic_id=? AND e.active=1 LIMIT 1''', (topic[0],)).fetchone()
            if remaining:
                value = json.loads(remaining[0])
                conn.execute('UPDATE topics SET title=?,user_need=?,scenario=?,draft_task=?,uncertainties=? WHERE id=?',
                    (value['summary'], value['user_need'], value.get('scenario'), dumps(value['draft_task']),
                     dumps(value['uncertainties']), topic[0]))
            else:
                conn.execute("UPDATE topics SET title='Источники недоступны',user_need='',scenario=NULL,draft_task='{}',uncertainties='[]' WHERE id=?", (topic[0],))

    def ingest(self, actor, records):
        require_role(actor, ('source',))
        if not isinstance(records, list) or len(records) > 500:
            raise DomainError('Ожидается не более 500 обращений')
        accepted = 0
        with self.db.transaction() as conn:
            for record in records:
                if not isinstance(record, dict):
                    raise DomainError('Неверный формат обращения')
                required = ('source_id', 'source_version', 'product_id', 'created_at', 'updated_at', 'permitted_acl')
                if any(k not in record for k in required):
                    raise DomainError('Отсутствуют обязательные поля источника')
                sid, version, product = record['source_id'], record['source_version'], record['product_id']
                if not isinstance(sid, str) or not 1 <= len(sid) <= 200 or type(version) is not int or version < 1:
                    raise DomainError('Неверный ID или версия источника')
                if product not in actor['products']:
                    raise DomainError('Источник вне разрешённых продуктов', 403)
                acl = record['permitted_acl']
                if not isinstance(acl, list) or not acl or any(not isinstance(x, str) for x in acl):
                    raise DomainError('Нужен явный ACL источника')
                for principal in acl:
                    user = conn.execute('SELECT products FROM users WHERE id=?', (principal,)).fetchone()
                    if not user or product not in json.loads(user['products']):
                        raise DomainError('ACL содержит неизвестного получателя или чужой продукт')
                created, updated = timestamp(record['created_at']), timestamp(record['updated_at'])
                if updated < created:
                    raise DomainError('updated_at раньше created_at')
                expires = timestamp(record.get('expires_at') or
                                    (datetime.fromisoformat(created) + timedelta(days=365)).isoformat())
                old = conn.execute('SELECT * FROM sources WHERE id=?', (sid,)).fetchone()
                if old and version <= old['version']:
                    continue
                if old and old['product'] != product:
                    raise DomainError('Смена продукта источника требует отдельной миграции')
                if not isinstance(record.get('text', ''), str) or not isinstance(record.get('attachments', []), list):
                    raise DomainError('Неверный текст или вложения')
                if any(not isinstance(x, dict) for x in record.get('attachments', [])):
                    raise DomainError('Неверный формат вложения')
                self.deactivate(conn, sid)
                # ACL/retention updates purge old derived content immediately.
                if old and (old['acl'] != dumps(sorted(set(acl))) or record.get('deleted')):
                    conn.execute("UPDATE evidence SET text_redacted='',extraction='{}' WHERE source_id=?", (sid,))
                conn.execute('''INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    version=excluded.version,updated_at=excluded.updated_at,acl=excluded.acl,
                    deleted=excluded.deleted,expires_at=excluded.expires_at''',
                    (sid, version, product, record.get('channel', 'ticket'), created, updated,
                     dumps(sorted(set(acl))), int(bool(record.get('deleted'))), expires))
                conn.execute("UPDATE jobs SET status='superseded',payload=NULL WHERE source_id=? AND status IN ('pending','retry','running','dead')", (sid,))
                if not record.get('deleted') and expires > now():
                    # Do not persist the unmasked body even in the queue.
                    safe = dict(record)
                    safe['text'] = mask(record.get('text', ''))
                    safe['attachments'] = [dict(a, **({'text': mask(a['text'])} if isinstance(a.get('text'), str) else {}))
                                           for a in record.get('attachments', [])]
                    conn.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?,0,?,NULL,NULL,?)',
                                 (uid(), sid, version, PIPELINE, dumps(safe), 'pending', now(), uid()))
                accepted += 1
                audit(conn, actor['id'], 'source.ingest', sid, details={'version': version})
        return {'accepted': accepted, 'received': len(records)}

    def work_one(self):
        with self.db.transaction() as conn:
            stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            conn.execute("UPDATE jobs SET status='retry',available_at=? WHERE status='running' AND locked_at<?", (now(), stale))
            row = conn.execute("SELECT * FROM jobs WHERE status IN ('pending','retry') AND available_at<=? ORDER BY available_at LIMIT 1", (now(),)).fetchone()
            if not row:
                return False
            job = dict(row)
            conn.execute("UPDATE jobs SET status='running',attempts=attempts+1,locked_at=? WHERE id=?", (now(), job['id']))
        try:
            record = json.loads(job['payload'])
            text, flags = self.preprocessor.process(record)
            eid = uid()
            result = validate_extraction(self.model.extract(text, eid, record['product_id']), eid, record['product_id'])
            # Candidate retrieval never decides a merge. Scope by product, ACL and type.
            with self.db.connect() as conn:
                candidates = conn.execute("SELECT * FROM topics WHERE product=? AND acl=? AND type=? AND status!='empty'",
                                          (record['product_id'], dumps(sorted(set(record['permitted_acl']))), result['type'])).fetchall()
            words = set(re.findall(r'\w+', result['user_need'].lower()))
            ranked = sorted(candidates, key=lambda c: len(words & set(re.findall(r'\w+', c['user_need'].lower()))), reverse=True)[:5]
            selected = None
            for candidate in ranked:
                if self.model.same_need(result, candidate) is True:
                    selected = (candidate['id'], candidate['revision'])
                    break
            with self.db.transaction() as conn:
                source = conn.execute('SELECT * FROM sources WHERE id=?', (job['source_id'],)).fetchone()
                state = conn.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
                if source['version'] != job['version'] or source['deleted'] or source['expires_at'] <= now() or state != 'running':
                    conn.execute("UPDATE jobs SET status='superseded',payload=NULL WHERE id=?", (job['id'],))
                    return True
                conn.execute('INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (eid, source['id'], source['version'], hashlib.sha256(text.encode()).hexdigest(), text,
                              self.model.version, dumps(flags), dumps(result), 1, now()))
                target = conn.execute('SELECT * FROM topics WHERE id=?', (selected[0],)).fetchone() if selected else None
                if target and (target['revision'] != selected[1] or target['status'] == 'empty'):
                    target = None
                if target:
                    tid = target['id']
                    conn.execute('UPDATE topics SET uncertainties=? WHERE id=?',
                        (dumps(sorted(set(json.loads(target['uncertainties'])+result['uncertainties']+flags))), tid))
                else:
                    tid = uid()
                    uncertainties = result['uncertainties'] + flags
                    if result['product'] is None:
                        uncertainties.append('Продукт модели неизвестен; маршрутизация по метаданным источника')
                    conn.execute('''INSERT INTO topics(id,product,type,title,user_need,scenario,draft_task,
                        uncertainties,acl,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                        (tid, source['product'], result['type'], result['summary'], result['user_need'],
                         result.get('scenario'), dumps(result['draft_task']), dumps(uncertainties), source['acl'], now()))
                conn.execute('INSERT INTO topic_evidence VALUES(?,?,?,?,?)', (tid, eid, 1.0, 'auto', now()))
                self.invalidate(conn, tid)
                conn.execute("UPDATE jobs SET status='done',payload=NULL,error=NULL WHERE id=?", (job['id'],))
                audit(conn, 'worker', 'evidence.register', tid, job['correlation_id'], {'source_id': source['id']})
        except Exception as exc:
            attempts = job['attempts'] + 1
            with self.db.transaction() as conn:
                # Error class only: exception messages may contain unredacted upstream content.
                conn.execute("UPDATE jobs SET status=?,available_at=?,error=? WHERE id=? AND status='running'",
                             ('dead' if attempts >= 3 else 'retry',
                              (datetime.now(timezone.utc) + timedelta(seconds=2 ** attempts)).isoformat(),
                              type(exc).__name__, job['id']))
                audit(conn, 'worker', 'job.failed', job['id'], job['correlation_id'], {'error': type(exc).__name__})
        return True

    def topics(self, actor, search='', kind='', state='', rare=False):
        require_role(actor, ('po', 'admin'))
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT * FROM topics WHERE status!='empty' ORDER BY last_seen DESC").fetchall()
            result = []
            current = datetime.now(timezone.utc)
            for row in rows:
                if not allowed(actor, row):
                    continue
                item = self.serialize_topic(conn, row)
                if item['count'] == 0:
                    continue
                if search.lower() not in (item['title'] + ' ' + item['user_need']).lower():
                    continue
                if kind and item['type'] != kind or state and item['status'] != state:
                    continue
                item['age_days'] = max(0, (current - datetime.fromisoformat(item['first_seen'])).days)
                item['rare_old'] = item['count'] <= 3 and item['age_days'] >= 30
                if rare and not item['rare_old']:
                    continue
                result.append(item)
            audit(conn, actor['id'], 'topics.read', 'registry', details={'count': len(result)})
            return result

    def serialize_topic(self, conn, row):
        item = dict(row)
        for field in ('draft_task', 'uncertainties', 'acl'):
            item[field] = json.loads(item[field])
        stats = conn.execute('''SELECT COUNT(DISTINCT s.id),
           COUNT(DISTINCT CASE WHEN s.created_at>=? THEN s.id END),
           COUNT(DISTINCT CASE WHEN s.created_at>=? AND s.created_at<? THEN s.id END)
           FROM topic_evidence te JOIN evidence e ON e.id=te.evidence_id JOIN sources s ON s.id=e.source_id
           WHERE te.topic_id=? AND e.active=1 AND s.deleted=0 AND s.expires_at>?''',
           ((datetime.now(timezone.utc)-timedelta(days=7)).isoformat(),
            (datetime.now(timezone.utc)-timedelta(days=14)).isoformat(),
            (datetime.now(timezone.utc)-timedelta(days=7)).isoformat(), row['id'], now())).fetchone()
        item.update(count=stats[0], recent_count=stats[1], previous_count=stats[2], trend=stats[1]-stats[2])
        link = conn.execute('SELECT * FROM backlog WHERE topic_id=?', (row['id'],)).fetchone()
        item['backlog'] = dict(link) if link else None
        return item

    def detail(self, actor, topic_id):
        with self.db.transaction() as conn:
            topic = self.topic(conn, actor, topic_id)
            result = self.serialize_topic(conn, topic)
            evidence = conn.execute('''SELECT e.*,s.acl,s.channel,s.created_at AS observed_at FROM evidence e
                JOIN topic_evidence te ON te.evidence_id=e.id JOIN sources s ON s.id=e.source_id
                WHERE te.topic_id=? AND e.active=1 AND s.deleted=0 AND s.expires_at>?''', (topic_id, now())).fetchall()
            result['evidence'] = [{k: e[k] for k in ('id','source_id','version','text_redacted','model_version','quality_flags','channel','observed_at')}
                                  for e in evidence if actor['id'] in json.loads(e['acl'])]
            result['history'] = [dict(r) for r in conn.execute('SELECT * FROM reviews WHERE topic_id=? ORDER BY timestamp DESC', (topic_id,))]
            audit(conn, actor['id'], 'topic.read', topic_id)
            return result

    def review(self, actor, topic_id, payload):
        require_role(actor, ('po',))
        action = payload.get('action')
        if action not in ('approve', 'reject', 'edit', 'merge', 'split'):
            raise DomainError('Неизвестное действие')
        reason = mask(str(payload.get('reason', ''))[:2000])
        with self.db.transaction() as conn:
            topic = self.topic(conn, actor, topic_id)
            if payload.get('revision') != topic['revision']:
                raise DomainError('Карточка изменилась. Обновите её перед решением.', 409)
            if topic['status'] == 'empty':
                raise DomainError('Нет активных подтверждений', 409)
            if action == 'approve':
                conn.execute("UPDATE topics SET status='approved',approved_revision=revision,approved_by=?,owner_id=? WHERE id=?",
                             (actor['id'], actor['id'], topic_id))
            elif action == 'reject':
                conn.execute("UPDATE topics SET status='rejected',approved_revision=NULL,approved_by=NULL,revision=revision+1 WHERE id=?", (topic_id,))
                conn.execute("UPDATE backlog SET sync_status='cancelled' WHERE topic_id=? AND external_issue_id IS NULL", (topic_id,))
            elif action == 'edit':
                title, need = payload.get('title'), payload.get('user_need')
                if not isinstance(title, str) or not title.strip() or len(title) > 250 or not isinstance(need, str) or not need.strip() or len(need)>4000:
                    raise DomainError('Укажите заголовок до 250 и потребность до 4000 символов')
                conn.execute('UPDATE topics SET title=?,user_need=?,draft_task=? WHERE id=?',
                             (mask(title), mask(need), dumps({'title': mask(title), 'description': mask(need)}), topic_id))
                self.invalidate(conn, topic_id)
            elif action == 'merge':
                target = self.topic(conn, actor, payload.get('target_id'))
                if (target['id'] == topic_id or target['product'] != topic['product'] or target['acl'] != topic['acl'] or
                        target['type'] != topic['type'] or target['status'] == 'empty'):
                    raise DomainError('Нельзя объединить темы с разными продуктами, ACL или типами')
                if conn.execute('SELECT 1 FROM backlog WHERE topic_id IN (?,?) AND external_issue_id IS NOT NULL', (topic_id, target['id'])).fetchone():
                    raise DomainError('Объединение связанных с бэклогом тем требует ручного согласования', 409)
                conn.execute('INSERT OR IGNORE INTO topic_evidence SELECT ?,evidence_id,1,?,? FROM topic_evidence WHERE topic_id=?',
                             (target['id'], 'manual', now(), topic_id))
                conn.execute('DELETE FROM topic_evidence WHERE topic_id=?', (topic_id,))
                self.invalidate(conn, topic_id)
                self.invalidate(conn, target['id'])
                audit(conn, actor['id'], 'topic.merge_target', target['id'], details={'from': topic_id})
            elif action == 'split':
                ids = payload.get('evidence_ids', [])
                current = [r[0] for r in conn.execute('''SELECT e.id FROM evidence e JOIN topic_evidence te ON te.evidence_id=e.id
                    WHERE te.topic_id=? AND e.active=1''', (topic_id,))]
                if not isinstance(ids, list) or not ids or not set(ids) < set(current):
                    raise DomainError('Выберите часть активных подтверждений для новой темы')
                title = payload.get('title', '')
                if not isinstance(title, str) or not title.strip() or len(title)>250:
                    raise DomainError('Укажите заголовок новой темы')
                new_id = uid()
                # New text must be derived from selected evidence, never from an unrelated source.
                texts = [conn.execute('SELECT text_redacted FROM evidence WHERE id=?', (eid,)).fetchone()[0] for eid in ids]
                need = '\n'.join(texts)[:4000]
                conn.execute('''INSERT INTO topics(id,product,type,title,user_need,scenario,draft_task,uncertainties,acl,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''', (new_id, topic['product'], topic['type'], mask(title), need, topic['scenario'],
                    dumps({'title':mask(title),'description':need}), topic['uncertainties'], topic['acl'], now()))
                for eid in ids:
                    conn.execute('UPDATE topic_evidence SET topic_id=?,decision=? WHERE topic_id=? AND evidence_id=?', (new_id, 'manual', topic_id, eid))
                self.invalidate(conn, topic_id)
                self.invalidate(conn, new_id)
                audit(conn, actor['id'], 'topic.split_created', new_id, details={'from': topic_id})
            conn.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?)', (uid(), topic_id, topic['revision'], actor['id'], action, now(), reason))
            audit(conn, actor['id'], 'topic.' + action, topic_id, details={'revision':topic['revision']})
        return {'ok': True}

    def create_backlog(self, actor, topic_id, payload):
        require_role(actor, ('po',))
        if self.shadow:
            raise DomainError('Теневой режим: создание задач отключено', 409)
        with self.db.transaction() as conn:
            topic = self.topic(conn, actor, topic_id)
            existing = conn.execute('SELECT * FROM backlog WHERE topic_id=?', (topic_id,)).fetchone()
            if existing and existing['external_issue_id']:
                return dict(existing)
            if (topic['approved_revision'] != topic['revision'] or topic['status'] != 'approved' or
                    payload.get('approved_revision') != topic['revision'] or
                    payload.get('approved_by') != topic['approved_by'] or
                    payload.get('idempotency_key') != 'topic:' + topic_id):
                raise DomainError('Нужны актуальная одобренная версия, автор и ключ topic:<id>', 409)
            # Transaction spans HTTP intentionally: source changes cannot invalidate approval during dispatch.
            # Remote adapter MUST support durable idempotency to recover a timeout-after-create.
            conn.execute('''INSERT INTO backlog VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(topic_id) DO UPDATE SET
                sync_status='pending',approved_revision=excluded.approved_revision,approved_by=excluded.approved_by''',
                (topic_id, 'internal' if self.tracker else 'local-demo', None, 'pending', None,
                 'topic:'+topic_id, topic['revision'], topic['approved_by'], None))
            draft = json.loads(topic['draft_task'])
            try:
                requested_id = payload.get('external_issue_id')
                if requested_id:
                    if not isinstance(requested_id, str) or len(requested_id)>200:
                        raise DomainError('Некорректный ID задачи')
                    if self.tracker:
                        from urllib.parse import quote
                        issue = self.tracker.request('/issues/'+quote(requested_id, safe=''))
                        if issue.get('product_id') != topic['product'] or issue.get('id') != requested_id:
                            raise DomainError('Задача недоступна для этого продукта')
                    else:
                        linked = conn.execute('''SELECT t.product,t.acl FROM backlog b JOIN topics t ON t.id=b.topic_id
                            WHERE b.external_issue_id=?''', (requested_id,)).fetchone()
                        if not linked or linked['product']!=topic['product'] or linked['acl']!=topic['acl']:
                            raise DomainError('Демонстрационная задача недоступна')
                    issue_id=requested_id
                elif self.tracker:
                    issue = self.tracker.request('/issues', {'idempotency_key': 'topic:'+topic_id, 'topic_id':topic_id,
                        'product_id':topic['product'], 'approved_revision':topic['revision'],'approved_by':topic['approved_by'], **draft})
                    if not isinstance(issue.get('id'), str) or not issue['id']:
                        raise DomainError('Неверный ID задачи')
                    issue_id = issue['id']
                else:
                    issue_id = 'DEMO-' + topic_id[:8].upper()
                    conn.execute('INSERT OR IGNORE INTO local_issues VALUES(?,?,?,?,?)',
                                 (issue_id, 'topic:'+topic_id, draft['title'], draft['description'], 'open'))
                conn.execute("UPDATE backlog SET external_issue_id=?,sync_status='open',last_synced_at=?,error=NULL WHERE topic_id=?", (issue_id, now(), topic_id))
                conn.execute("UPDATE topics SET status='backlog' WHERE id=?", (topic_id,))
                audit(conn, actor['id'], 'backlog.created', topic_id, details={'issue_id':issue_id})
            except Exception as exc:
                conn.execute("UPDATE backlog SET sync_status='error',error=? WHERE topic_id=?", (type(exc).__name__, topic_id))
                audit(conn, actor['id'], 'backlog.failed', topic_id)
            return dict(conn.execute('SELECT * FROM backlog WHERE topic_id=?', (topic_id,)).fetchone())

    def sync_backlog(self):
        with self.db.connect() as conn:
            links = [dict(r) for r in conn.execute('SELECT * FROM backlog WHERE external_issue_id IS NOT NULL')]
        for link in links:
            try:
                if self.tracker:
                    from urllib.parse import quote
                    state = self.tracker.request('/issues/'+quote(link['external_issue_id'], safe=''))['status']
                else:
                    with self.db.connect() as conn:
                        state = conn.execute('SELECT status FROM local_issues WHERE id=?', (link['external_issue_id'],)).fetchone()[0]
                if state not in ('open','in_progress','done','cancelled'):
                    raise DomainError('Неподдерживаемый статус трекера')
                with self.db.transaction() as conn:
                    conn.execute('UPDATE backlog SET sync_status=?,last_synced_at=?,error=NULL WHERE topic_id=?', (state, now(), link['topic_id']))
                    audit(conn, 'backlog-service', 'backlog.sync', link['topic_id'], details={'status':state})
            except Exception as exc:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE backlog SET sync_status='error',error=? WHERE topic_id=?", (type(exc).__name__,link['topic_id']))

    def expire(self):
        with self.db.transaction() as conn:
            rows = conn.execute('SELECT id FROM sources WHERE expires_at<=? AND deleted=0', (now(),)).fetchall()
            for row in rows:
                self.deactivate(conn, row['id'])
                conn.execute('UPDATE sources SET deleted=1 WHERE id=?', (row['id'],))
                conn.execute("UPDATE evidence SET text_redacted='',extraction='{}' WHERE source_id=?", (row['id'],))
                conn.execute("UPDATE jobs SET status='expired',payload=NULL WHERE source_id=?", (row['id'],))
                audit(conn, 'retention', 'source.expired', row['id'])
            # Remove derived text and ACL from empty topics, keep only non-content audit identifiers.
            conn.execute("UPDATE topics SET title='Источники недоступны',user_need='',scenario=NULL,draft_task='{}',uncertainties='[]' WHERE status='empty'")
        return len(rows)

    def digest(self, actor):
        topics = self.topics(actor)
        result = []
        cutoff = (datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
        with self.db.transaction() as conn:
            for topic in sorted(topics, key=lambda t:(t['rare_old'],t['recent_count'],t['age_days']), reverse=True):
                if topic['status'] != 'review':
                    continue
                old = conn.execute('SELECT sent_at FROM digests WHERE actor_id=? AND topic_id=?', (actor['id'],topic['id'])).fetchone()
                if old and old[0] > cutoff:
                    continue
                conn.execute('INSERT OR REPLACE INTO digests VALUES(?,?,?)', (actor['id'],topic['id'],now()))
                result.append(topic)
                if len(result) >= 10:
                    break
            audit(conn, actor['id'], 'digest.generated', 'registry', details={'count':len(result)})
        return result

    def operations(self, actor):
        require_role(actor, ('admin',))
        with self.db.connect() as conn:
            jobs = [dict(r) for r in conn.execute('SELECT id,source_id,version,status,attempts,error,correlation_id FROM jobs ORDER BY available_at DESC LIMIT 100')]
            events = [dict(r) for r in conn.execute('SELECT * FROM audit ORDER BY timestamp DESC LIMIT 150')]
            counts = {r[0]:r[1] for r in conn.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status')}
            ages = sorted(max(0, (datetime.now(timezone.utc)-datetime.fromisoformat(r[0])).total_seconds())
                for r in conn.execute("SELECT s.updated_at FROM jobs j JOIN sources s ON s.id=j.source_id WHERE j.status IN ('pending','retry','running','dead')"))
            p95 = ages[min(len(ages)-1, int(len(ages)*.95))] if ages else 0
            return {'jobs':jobs,'audit':events,'queue':counts,
                    'oldest_unprocessed_seconds':max(ages,default=0), 'p95_unprocessed_seconds':p95,
                    'slo_at_risk':p95>86400,
                    'backlog_errors':conn.execute("SELECT COUNT(*) FROM backlog WHERE sync_status='error'").fetchone()[0]}

    def retry_job(self, actor, job_id):
        require_role(actor, ('admin',))
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=? AND status='dead' AND payload IS NOT NULL", (job_id,)).fetchone()
            if not row:
                raise DomainError('Повторная обработка недоступна', 409)
            conn.execute("UPDATE jobs SET status='pending',attempts=0,available_at=?,error=NULL WHERE id=?", (now(),job_id))
            audit(conn, actor['id'], 'job.retry', job_id)
