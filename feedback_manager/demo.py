import secrets
from datetime import datetime, timedelta, timezone
from .db import dumps
from .security import password_hash


def seed(manager):
    db = manager.db
    credentials = {}
    users = [('po','Анна Смирнова','po',['portal','analytics']),
             ('po2','PO аналитики','po',['analytics']),
             ('admin','Администратор','admin',[]),
             ('source','Read-only коннектор','source',['portal','analytics'])]
    with db.transaction() as conn:
        for user_id, name, role, products in users:
            if not conn.execute('SELECT 1 FROM users WHERE id=?',(user_id,)).fetchone():
                password = secrets.token_urlsafe(15)
                credentials[user_id] = password
                conn.execute('INSERT INTO users VALUES(?,?,?,?,?)',(user_id,name,role,dumps(products),password_hash(password)))
    with db.connect() as conn:
        exists = conn.execute('SELECT 1 FROM sources LIMIT 1').fetchone()
    if exists:
        return credentials
    needs = [
        ('portal', 'Нужна возможность выгружать историю заявок в Excel', [1,2,3,5,8,12,16,21]),
        ('portal', 'Не работает фильтр заявок по подразделению', [0,1,2,4]),
        ('portal', 'Добавьте сохранение черновика обращения', [48,67]),
        ('portal', 'Хочу получать уведомления об изменении статуса в личном кабинете', [2,4,6]),
        ('analytics', 'Нужна возможность сравнить отчёты за два периода', [1,3,5,10,14]),
        ('analytics', 'Ошибка при открытии отчёта за прошлый месяц', [0,2,3]),
        ('analytics', 'Добавьте тёмную тему для работы в ночную смену', [83]),
        ('portal', 'Как изменить ответственного за заявку?', [1]),
    ]
    records=[]
    for i,(product,text,days) in enumerate(needs):
        for j,age in enumerate(days):
            dt=(datetime.now(timezone.utc)-timedelta(days=age)).isoformat()
            record={'source_id':'DEMO-%03d-%02d'%(i,j),'source_version':1,'product_id':product,
                    'text':text,'created_at':dt,'updated_at':dt,
                    'permitted_acl':['po','po2'] if product=='analytics' else ['po']}
            if i==2 and j==0:
                record['attachments']=[{'kind':'application/pdf','ref':'internal-file:demo'}]
            records.append(record)
    manager.ingest(manager.actor('source'),records)
    while manager.work_one():
        pass
    return credentials
