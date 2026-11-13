import argparse
import json
import os
import threading
import time
from wsgiref.simple_server import make_server, WSGIRequestHandler
from .db import Database, dumps, now
from .demo import seed
from .models import InternalHTTP, Preprocessor, QwenModel
from .service import Manager
from .web import Application
from .security import password_hash


def configured_manager(db):
    allowed=set(filter(None,os.getenv('INTERNAL_ALLOWED_HOSTS','').split(',')))
    def http(name):
        url=os.getenv(name+'_URL')
        return InternalHTTP(url,allowed,os.getenv(name+'_TOKEN','')) if url else None
    qwen,media,tracker=http('QWEN'),http('MEDIA'),http('TRACKER')
    return Manager(db,model=QwenModel(qwen,os.getenv('QWEN_MODEL','Qwen-3.8')) if qwen else None,
                   preprocessor=Preprocessor(media),tracker=tracker,shadow=os.getenv('SHADOW_MODE','1')!='0')


class QuietHandler(WSGIRequestHandler):
    def log_message(self,format,*args):
        pass  # URLs and request bodies are not logged. Domain actions go to audit.


def main():
    parser=argparse.ArgumentParser(description='AI Product Feedback Manager')
    parser.add_argument('--db',default=os.getenv('DATABASE_PATH','data/feedback.db'))
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('demo',help='Synthetic data and generated passwords')
    serve=sub.add_parser('serve')
    serve.add_argument('--host',default='127.0.0.1')
    serve.add_argument('--port',type=int,default=8080)
    serve.add_argument('--no-worker',action='store_true')
    sub.add_parser('worker')
    ingest=sub.add_parser('import')
    ingest.add_argument('file')
    sub.add_parser('expire')
    create=sub.add_parser('user')
    create.add_argument('id')
    create.add_argument('--name',required=True)
    create.add_argument('--role',choices=['po','admin','source'],required=True)
    create.add_argument('--products',default='')
    password_cmd=sub.add_parser('password',help='Reset a local user password interactively')
    password_cmd.add_argument('id')
    args=parser.parse_args()
    os.umask(0o077)
    db=Database(args.db)
    manager=configured_manager(db)
    if args.command=='demo':
        print('Синтетические данные. Пароли показаны только при создании пользователя:')
        credentials=seed(manager)
        if credentials:
            destination=os.path.join(os.path.dirname(os.path.abspath(args.db)),'demo-credentials.json')
            with open(destination,'w',encoding='utf-8') as file:
                json.dump(credentials,file,ensure_ascii=False,indent=2)
            os.chmod(destination,0o600)
            print('Пароли сохранены локально: '+destination)
        print(json.dumps(credentials,ensure_ascii=False,indent=2))
    elif args.command=='user':
        import getpass
        password=getpass.getpass('Пароль (не менее 12 символов): ')
        if len(password)<12:
            parser.error('Слишком короткий пароль')
        with db.transaction() as conn:
            conn.execute('INSERT INTO users VALUES(?,?,?,?,?)',(args.id,args.name,args.role,dumps(list(filter(None,args.products.split(',')))),password_hash(password)))
    elif args.command=='import':
        with open(args.file,encoding='utf-8') as file:
            print(manager.ingest(manager.actor('source'),json.load(file)))
    elif args.command=='expire':
        print({'expired':manager.expire()})
    elif args.command=='password':
        import getpass
        value=getpass.getpass('Новый пароль (не менее 12 символов): ')
        if len(value)<12:
            parser.error('Слишком короткий пароль')
        with db.transaction() as conn:
            if not conn.execute('SELECT 1 FROM users WHERE id=?',(args.id,)).fetchone():
                parser.error('Неизвестный пользователь')
            conn.execute('UPDATE users SET password=? WHERE id=?',(password_hash(value),args.id))
            conn.execute('DELETE FROM sessions WHERE user_id=?',(args.id,))
    else:
        stop=threading.Event()
        def worker():
            next_poll=0
            from .connectors import SourceConnector
            source=None
            if os.getenv('SOURCE_URL'):
                source=SourceConnector(manager,InternalHTTP(os.environ['SOURCE_URL'],set(os.getenv('INTERNAL_ALLOWED_HOSTS','').split(',')),os.getenv('SOURCE_TOKEN','')))
            while not stop.is_set():
                try:
                    if time.monotonic()>=next_poll:
                        manager.expire()
                        manager.sync_backlog()
                        if source:
                            source.poll()
                        next_poll=time.monotonic()+60
                    if not manager.work_one():
                        stop.wait(1)
                except Exception as exc:
                    # No raw exception data from corporate services.
                    print(dumps({'event':'worker.error','class':type(exc).__name__,'at':now()}),flush=True)
                    stop.wait(5)
        if args.command=='worker':
            try:
                worker()
            except KeyboardInterrupt:
                stop.set()
        else:
            if not args.no_worker:
                threading.Thread(target=worker,daemon=True).start()
            app=Application(manager,secure_cookie=os.getenv('SECURE_COOKIE','0')=='1')
            print('Feedback Manager: http://%s:%s · shadow=%s'%(args.host,args.port,manager.shadow),flush=True)
            try:
                with make_server(args.host,args.port,app,handler_class=QuietHandler) as server:
                    server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                stop.set()


if __name__=='__main__':
    main()
