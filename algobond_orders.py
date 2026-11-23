"""Read-only view of the local order ledger; never creates a database or broker."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3

ORDER_COLUMNS=('orderId','securityCode','board','side','quantity','filled_quantity',
               'remaining_quantity','price','status','created_at','expires_at','managed')
ACTIVE_STATUSES={'submitting','unknown','new','partial','pending_cancel'}


def readOrdersSnapshot(project, login, broker, when=''):
    path=Path(project).resolve()/'data/orders/orders.sqlite3'
    result=dict(orders=[],warning='',updated_at='')
    if not path.is_file():
        result['warning']='Локальный журнал заявок ещё не создан. Он появляется после первого подключения торгового процесса к брокеру.'
        return result
    if path.is_symlink() or not path.resolve().is_relative_to(Path(project).resolve()):
        result['warning']='Локальный журнал заявок недоступен.'
        return result
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            args=(str(login),str(broker).lower())
            if when:
                stamp=datetime.fromisoformat(str(when))
                # AccountInfo uses the project's Moscow wall-clock timestamps.
                if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone(timedelta(hours=3)))
                cutoff=stamp.timestamp()
                rows=conn.execute("""SELECT h.payload FROM order_history h JOIN (
                    SELECT scope,order_key,MAX(event_id) event_id FROM order_history
                    WHERE login=? AND broker=? AND updated_at<=? GROUP BY scope,order_key
                ) last ON h.event_id=last.event_id ORDER BY h.event_id DESC LIMIT 10001""",args+(cutoff,)).fetchall()
                rows=[json.loads(row['payload']) for row in rows]
            else:
                rows=[dict(row) for row in conn.execute('SELECT * FROM orders WHERE login=? AND broker=? ORDER BY created_at DESC LIMIT 10001',args)]
            if when:
                latest=conn.execute('SELECT MAX(updated_at) FROM order_history WHERE login=? AND broker=? AND updated_at<=?',args+(cutoff,)).fetchone()[0]
            else:
                sync=conn.execute('SELECT MAX(updated_at),MAX(error) FROM order_sync WHERE login=? AND broker=?',args).fetchone()
                latest=sync[0]
                if sync[1]:result['warning']=str(sync[1])
            if latest:result['updated_at']=datetime.fromtimestamp(latest,timezone.utc).isoformat()
            if len(rows)>10000:result['warning']='Локальный журнал: показаны первые 10 000 записей.'
            for row in rows[:10000]:
                if row.get('status') not in ACTIVE_STATUSES:continue
                item={key:row.get(key) for key in ORDER_COLUMNS}
                for key in ('created_at','expires_at'):
                    if item[key]:item[key]=datetime.fromtimestamp(item[key],timezone.utc).isoformat()
                item['checked_at']=result['updated_at']
                item['quantity_unit']='шт.' if row.get('broker') in ('finam','bcs') else 'лоты'
                item['price_unit']='% номинала' if row.get('managed') else 'Единицы API брокера'
                item['managed']='AlgoBond' if row.get('managed') else 'Внешняя'
                result['orders'].append(item)
    except (sqlite3.Error,ValueError,TypeError,KeyError,OverflowError):
        result['orders']=[]
        result['warning']='Не удалось прочитать локальный журнал заявок.'
    return result
