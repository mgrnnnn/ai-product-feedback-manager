import hmac
import json
import mimetypes
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs
from .db import audit, dumps, now
from .security import DomainError, password_check, require_role

STATIC = Path(__file__).parent / 'static'


class Application:
    def __init__(self, manager, secure_cookie=False):
        self.manager=manager
        self.secure_cookie=secure_cookie
        self.login_attempts={}

    def __call__(self, environ, start_response):
        headers=[('X-Content-Type-Options','nosniff'),('X-Frame-Options','DENY'),
                 ('Cache-Control','no-store'),('Referrer-Policy','no-referrer'),
                 ('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
        try:
            status, content_type, body, extra=self.handle(environ)
            headers.extend(extra)
        except DomainError as exc:
            status,content_type,body=exc.status,'application/json',dumps({'error':str(exc)}).encode()
        except Exception:
            status,content_type,body=500,'application/json',b'{"error":"Internal error; inspect operation audit"}'
        headers.extend([('Content-Type',content_type),('Content-Length',str(len(body)))])
        names={200:'OK',400:'Bad Request',401:'Unauthorized',403:'Forbidden',404:'Not Found',405:'Method Not Allowed',409:'Conflict',413:'Payload Too Large',429:'Too Many Requests',500:'Internal Server Error'}
        start_response(str(status)+' '+names.get(status,'Error'),headers)
        return [body]

    def handle(self, env):
        path,method=env.get('PATH_INFO','/'),env['REQUEST_METHOD']
        if method not in ('GET','POST'):
            raise DomainError('Метод не поддерживается',405)
        if path=='/health' and method=='GET':
            return self.json({'status':'ok'})
        if not path.startswith('/api/'):
            if method!='GET':
                raise DomainError('Метод не поддерживается',405)
            files={'/':'index.html','/app.js':'app.js','/styles.css':'styles.css'}
            if path not in files:
                raise DomainError('Не найдено',404)
            file=STATIC/files[path]
            return 200, (mimetypes.guess_type(str(file))[0] or 'text/plain')+'; charset=utf-8',file.read_bytes(),[]
        payload={}
        if method=='POST':
            if env.get('CONTENT_TYPE','').split(';')[0]!='application/json':
                raise DomainError('Требуется application/json')
            try:
                length=int(env.get('CONTENT_LENGTH','0'))
                if length>2_000_000 or length<0:
                    raise DomainError('Слишком большой запрос',413)
                payload=json.loads(env['wsgi.input'].read(length))
                if not isinstance(payload,dict):
                    raise ValueError()
            except (ValueError,UnicodeDecodeError):
                raise DomainError('Неверный JSON')
        if path=='/api/login' and method=='POST':
            ip=env.get('REMOTE_ADDR','unknown')
            recent=[t for t in self.login_attempts.get(ip,[]) if t>time.monotonic()-60]
            if len(recent)>=10:
                raise DomainError('Слишком много попыток. Повторите через минуту.',429)
            recent.append(time.monotonic())
            self.login_attempts[ip]=recent
            with self.manager.db.transaction() as conn:
                user=conn.execute('SELECT * FROM users WHERE id=?',(payload.get('username'),)).fetchone()
                if not isinstance(payload.get('password'),str) or len(payload['password'])>1000:
                    raise DomainError('Неверный логин или пароль',401)
                if not user or not password_check(payload['password'],user['password']):
                    raise DomainError('Неверный логин или пароль',401)
                token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
                expires=(datetime.now(timezone.utc)+timedelta(hours=8)).isoformat()
                conn.execute('DELETE FROM sessions WHERE expires_at<=?',(now(),))
                conn.execute('INSERT INTO sessions VALUES(?,?,?,?)',(token,user['id'],csrf,expires))
                audit(conn,user['id'],'session.login',user['id'])
            return self.json({'csrf':csrf},[('Set-Cookie','session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'+('; Secure' if self.secure_cookie else ''))])
        cookie=SimpleCookie()
        cookie.load(env.get('HTTP_COOKIE',''))
        token=cookie['session'].value if 'session' in cookie else ''
        with self.manager.db.connect() as conn:
            session=conn.execute('SELECT * FROM sessions WHERE token=? AND expires_at>?',(token,now())).fetchone()
        if not session:
            raise DomainError('Требуется вход',401)
        actor=self.manager.actor(session['user_id'])
        if method=='POST' and not hmac.compare_digest(env.get('HTTP_X_CSRF_TOKEN',''),session['csrf']):
            raise DomainError('Неверный CSRF token',403)
        self.manager.expire()
        if path=='/api/me' and method=='GET':
            return self.json({'user':{k:actor[k] for k in ('id','name','role','products')},'csrf':session['csrf'],
                              'shadow':self.manager.shadow,'model':self.manager.model.version})
        if path=='/api/logout' and method=='POST':
            with self.manager.db.transaction() as conn:
                conn.execute('DELETE FROM sessions WHERE token=?',(token,))
            return self.json({'ok':True},[('Set-Cookie','session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')])
        if path=='/api/topics' and method=='GET':
            query=parse_qs(env.get('QUERY_STRING',''))
            get=lambda k:query.get(k,[''])[0]
            return self.json(self.manager.topics(actor,get('q'),get('type'),get('status'),get('rare')=='1'))
        parts=path.strip('/').split('/')
        if len(parts)>=3 and parts[:2]==['api','topics']:
            tid=parts[2]
            if len(parts)==3 and method=='GET':
                return self.json(self.manager.detail(actor,tid))
            if len(parts)==4 and method=='POST':
                if parts[3]=='review':
                    return self.json(self.manager.review(actor,tid,payload))
                if parts[3]=='backlog':
                    return self.json(self.manager.create_backlog(actor,tid,payload))
        if path=='/api/digest' and method=='POST':
            return self.json(self.manager.digest(actor))
        if path=='/api/ingest' and method=='POST':
            return self.json(self.manager.ingest(actor,payload.get('records')))
        if path=='/api/operations' and method=='GET':
            return self.json(self.manager.operations(actor))
        if path=='/api/jobs/retry' and method=='POST':
            self.manager.retry_job(actor,payload.get('job_id'))
            return self.json({'ok':True})
        raise DomainError('Не найдено',404)

    @staticmethod
    def json(value,extra=None):
        return 200,'application/json; charset=utf-8',dumps(value).encode(),extra or []
