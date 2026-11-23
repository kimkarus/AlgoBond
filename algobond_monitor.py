"""Read-only portfolio views using the project's unmodified DatabaseProvider.

The web process has no broker or database imports. Native drivers run in a
short-lived worker, so a driver crash cannot terminate the web application.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import start_config_editor as editor

TABLES = ("AccountInfo", "SymbolsInfo", "PlanningSymbolInfo")
IDENTITY = ("client_id", "login", "server", "company", "currency", "type_account")
ACCOUNT_IDENTITY = ("login",)
FIELDS = {
    "AccountInfo": (*IDENTITY, "name", "date_record", "equity", "balance", "profit", "free_margin", "margin", "assets", "liabilities", "changes", "changes_abs", "currency_digits", "fifo_close", "hedge_allowed", "credit", "margin_level", "margin_so_call", "margin_so_so", "margin_initial", "margin_maintenance", "comission_blocked", "sum_volume", "sum_profit", "sum_profile", "year_record", "month_record", "day_record", "day_of_year_record", "day_of_week_record", "date_update_analytics"),
    "SymbolsInfo": ("date_record", "symbol", "ticket", "type", "secname", "shortname", "volume", "price_open", "price_current", "profit", "estimate", "changes", "changes_abs", "boardid", "faceunit", "date_opened", "sl", "tp", "profit_perday", "profit_perday0", "type_account", "latname", "for_trading", "date_update_analytics"),
    "PlanningSymbolInfo": ("date_record", "symbol", "ticket", "type", "secname", "shortname", "matdate", "nextcoupon", "count", "lotvalue", "price", "couponpercent", "accruedint", "profit", "outcome", "income", "leftdays", "profit_perday", "profit_perday0", "boardid", "faceunit", "type_account", "latname", "for_trading"),
}
MAX_ROWS = 10000
MAX_SNAPSHOTS = 2000


def safe_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return str(value)  # Keep decimal precision on the wire.
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def account_key(row):
    # A login is the only logical account key. Storage labels do not split it.
    values = [identity_text(row.get("login"))]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:24]


def identity_text(value):
    return "" if value is None else str(value).strip()


def public_account(account):
    return {key: value for key, value in account.items() if not key.startswith("_")}


def sql_literal(value, dialect):
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise editor.EditorError("Недопустимые символы в идентификаторе счета.")
    if dialect in ("clickhouse", "postgresql"):
        value = value.replace("\\", "\\\\").replace("'", "\\'")
    else:
        value = value.replace("'", "''")
    return ("E'" if dialect == "postgresql" else "'") + value + "'"


class PortfolioReader:
    """Only fixed SELECTs and metadata reads; no SQL is accepted from the UI."""
    def __init__(self, provider, cfg):
        self.provider, self.cfg = provider, cfg
        self.dialect = cfg.database_type
        self.columns = {table: self.describe(table) for table in TABLES}
        required = {*IDENTITY, "date_record", "name"}
        if not required.issubset(self.columns["AccountInfo"]):
            raise editor.EditorError("В базе нет совместимой таблицы AccountInfo. Проверьте выбранную базу.")

    def table(self, table):
        if table not in TABLES:
            raise editor.EditorError("Неизвестная таблица.")
        if self.dialect == "clickhouse":
            name = self.cfg.database_name
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise editor.EditorError("Имя базы ClickHouse должно состоять из букв, цифр и подчеркивания.")
            return f"`{name}`.`{table}` FINAL"
        return table.lower() if self.dialect == "postgresql" else table

    def query(self, sql):
        result = self.provider.selectDataList(sql)
        if not isinstance(result, (list, tuple)):
            raise editor.EditorError("База вернула неподдерживаемый результат.")
        return result

    def describe(self, table):
        if self.dialect == "sqlite3":
            rows = self.query(f"PRAGMA table_info({self.table(table)})")
            return {str(r[1]).lower() for r in rows}
        if self.dialect == "postgresql":
            rows = self.query("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=" + sql_literal(table.lower(), self.dialect))
        else:
            rows = self.query("SELECT name FROM system.columns WHERE database=" + sql_literal(self.cfg.database_name, self.dialect) + " AND table=" + sql_literal(table, self.dialect))
        return {str(r[0]).lower() for r in rows}

    def records(self, table, sql, fields):
        return [dict(zip(fields, [safe_value(v) for v in row])) for row in self.query(sql)]

    def text_cast(self, field):
        return f"toString({field})" if self.dialect == "clickhouse" else f"CAST({field} AS TEXT)"

    def physical_where(self, account, fields=IDENTITY):
        return " AND ".join(
            f"{key} IS NULL" if account.get(key) is None else
            f"{self.text_cast(key)}={sql_literal(account[key], self.dialect)}"
            for key in fields)

    def where(self, account):
        # The login always comes from the selected INI, never from a request.
        return self.physical_where(dict(login=self.cfg.login), ACCOUNT_IDENTITY)

    def currency_where(self, account, currency):
        # Currency is a unit of a snapshot, not another account identifier.
        # Keep equal-unit aliases, but never add RUB and USD into one value.
        unit=identity_text(currency).casefold()
        values={row.get("currency") for row in account.get("_variants",[account])
                if identity_text(row.get("currency")).casefold()==unit}
        if not values:values={currency}
        return "("+" OR ".join(self.physical_where(dict(currency=value),("currency",))
                               for value in sorted(values,key=lambda value:"" if value is None else str(value)))+")"

    def variant_rank(self, row, preferred):
        values=lambda item: tuple("" if item.get(key) is None else str(item[key]) for key in IDENTITY)
        return (values(row)!=values(preferred), identity_text(row.get("client_id"))!=identity_text(self.cfg.client_id), values(row))

    def accounts(self):
        login = str(self.cfg.login) if self.cfg.login is not None else ""
        if not login.strip():
            raise editor.EditorError("В выбранной конфигурации не заполнен client.login. Укажите логин в редакторе.")
        fields = (*IDENTITY, "name", "latest")
        # Distinct aliases matter for the provider's PostgreSQL RealDictCursor.
        predicate = f"{self.text_cast('login')}={sql_literal(login, self.dialect)}"
        sql = f"SELECT {','.join(IDENTITY)},MAX(name) AS name,MAX(date_record) AS latest FROM {self.table('AccountInfo')} WHERE {predicate} GROUP BY {','.join(IDENTITY)} ORDER BY MAX(date_record) DESC LIMIT 1001"
        rows = self.records("AccountInfo", sql, fields)
        if len(rows) > 1000:
            raise editor.EditorError("В базе более 1000 вариантов счета с указанным client.login.")
        groups={}
        for row in rows:groups.setdefault(account_key(row), []).append(row)
        accounts=[]
        for key,variants in groups.items():
            # Newest storage identity is the display representative; client_id
            # from the selected config wins when timestamps coincide.
            latest=max(str(row.get("latest") or "") for row in variants)
            newest=[row for row in variants if str(row.get("latest") or "")==latest]
            representative=min(newest,key=lambda row:self.variant_rank(row, {}))
            account=dict(representative)
            for field in IDENTITY:account[field]=identity_text(account.get(field))
            account.update(id=key,_variants=[{field:row.get(field) for field in IDENTITY} for row in variants])
            account["currencies"]=sorted({identity_text(row.get("currency")).upper() for row in variants})
            accounts.append(account)
        return sorted(accounts,key=lambda row:(str(row.get("latest") or ""),row["id"]),reverse=True)

    def catalog(self):
        accounts = self.accounts()
        matches = [a for a in accounts if a["client_id"] == str(self.cfg.client_id)]
        default = accounts[0]["id"] if len(accounts) == 1 else matches[0]["id"] if len(matches) == 1 else None
        return dict(accounts=[public_account(account) for account in accounts], default_account=default, configured_match=bool(accounts),
                    database_type=self.dialect, tables={t: bool(c) for t, c in self.columns.items()},
                    message="" if accounts else "В базе нет снимков с login выбранной конфигурации. Проверьте client.login; другие счета не отображаются.")

    def snapshot_dates(self, account):
        sql = f"SELECT DISTINCT date_record FROM {self.table('AccountInfo')} WHERE {self.where(account)} ORDER BY date_record DESC LIMIT {MAX_SNAPSHOTS+1}"
        rows = self.query(sql)
        return [safe_value(r[0]) for r in rows[:MAX_SNAPSHOTS]], len(rows) > MAX_SNAPSHOTS

    def asof_rows(self, table, account, cutoff, preferred=None):
        cols = self.columns[table]
        if not {*IDENTITY, "date_record"}.issubset(cols):
            return [], None, False
        predicate = self.where(account)
        if table!="AccountInfo" and preferred is not None:
            predicate += " AND "+self.currency_where(account,preferred.get("currency"))
        cutoff_sql = sql_literal(cutoff, self.dialect)
        latest = self.query(f"SELECT MAX(date_record) FROM {self.table(table)} WHERE {predicate} AND date_record<={cutoff_sql}")
        stamp = latest[0][0] if latest and latest[0] else None
        if stamp in (None, "", r"\N"):
            return [], None, False
        identities=self.records(table,
            f"SELECT DISTINCT {','.join(IDENTITY)} FROM {self.table(table)} WHERE {predicate} AND date_record={sql_literal(safe_value(stamp), self.dialect)} ORDER BY {','.join(IDENTITY)} LIMIT 1001",
            IDENTITY)
        if len(identities)>1000:raise editor.EditorError("Слишком много вариантов записи одного счета на одну дату.")
        if not identities:return [],None,False
        chosen=min(identities,key=lambda row:self.variant_rank(row, preferred or account))
        predicate=self.physical_where(chosen)
        fields = [c for c in FIELDS[table] if c in cols]
        ordering = ",".join(c for c in ("symbol", "ticket", "date_opened", "matdate", "nextcoupon", "type") if c in cols) or "date_record"
        sql = f"SELECT {','.join(fields)} FROM {self.table(table)} WHERE {predicate} AND date_record={sql_literal(safe_value(stamp), self.dialect)} ORDER BY {ordering} LIMIT {MAX_ROWS+1}"
        rows = self.records(table, sql, fields)
        return rows[:MAX_ROWS], safe_value(stamp), len(rows) > MAX_ROWS

    def snapshot(self, key, when="", before="", at=""):
        if sum(bool(value) for value in (when, before, at)) > 1:
            raise editor.EditorError("Выберите один способ задания даты снимка.")
        account = next((a for a in self.accounts() if a["id"] == key), None)
        if account is None:
            raise editor.EditorError("Счет недоступен для login выбранной конфигурации.", 404)
        dates, dates_limited = self.snapshot_dates(account)
        if not dates:
            return dict(account=public_account(account), dates=[], account_info=[], positions=[], planning=[], history=[], warnings=["Снимков счета нет."])
        if before:
            try:
                next_day = (datetime.strptime(before, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"
            except (ValueError, OverflowError):
                raise editor.EditorError("Выберите дату в формате ГГГГ-ММ-ДД.") from None
            nearest = self.query(f"SELECT MAX(date_record) FROM {self.table('AccountInfo')} WHERE {self.where(account)} AND date_record<{sql_literal(next_day,self.dialect)}")
            when = safe_value(nearest[0][0]) if nearest and nearest[0] else None
            if when in (None, "", r"\N"):
                raise editor.EditorError("В базе нет снимка на выбранную дату или раньше.", 404)
        if at:
            try:
                stamp = datetime.fromisoformat(at.replace("T", " "))
                if stamp.tzinfo is not None:
                    raise ValueError
                upper = stamp.isoformat(sep=" ")
            except ValueError:
                raise editor.EditorError("Некорректная дата общего снимка.") from None
            nearest = self.query(f"SELECT MAX(date_record) FROM {self.table('AccountInfo')} WHERE {self.where(account)} AND date_record<={sql_literal(upper,self.dialect)}")
            when = safe_value(nearest[0][0]) if nearest and nearest[0] else None
            if when in (None, "", r"\N"):
                raise editor.EditorError("Нет состояния счета не позднее выбранного времени.", 404)
        if when and when not in dates:
            found = self.query(f"SELECT date_record FROM {self.table('AccountInfo')} WHERE {self.where(account)} AND date_record={sql_literal(when,self.dialect)} LIMIT 1")
            if not found:
                raise editor.EditorError("Такого снимка нет в истории выбранного счета.")
            dates = sorted({*dates, when}, reverse=True)
        cutoff = when or dates[0]
        warnings = []
        if before and str(cutoff)[:10] != before:
            warnings.append("На выбранную дату снимков нет; показан ближайший предыдущий снимок: " + str(cutoff) + ".")
        result = dict(account=public_account(account), dates=dates, selected_date=cutoff, latest_date=dates[0], source_dates={})
        preferred=account
        for table, dest in zip(TABLES, ("account_info", "positions", "planning")):
            rows, stamp, limited = self.asof_rows(table, account, cutoff, preferred)
            result[dest] = rows
            if table=="AccountInfo" and rows:
                preferred=rows[0]
                for field in (*IDENTITY,"name"):
                    if field in preferred:result["account"][field]=identity_text(preferred[field])
                result["account"]["currency"]=identity_text(preferred.get("currency")).upper()
            result["source_dates"][table] = stamp
            if stamp != cutoff:
                warnings.append(f"{table}: " + (f"показан ближайший предыдущий снимок {stamp}." if stamp else "нет данных на выбранный момент."))
            if limited:
                warnings.append(f"{table}: показаны первые {MAX_ROWS} строк. Итоги по таблице неполные.")
        fields = [c for c in ("date_record", "equity", "balance", "profit", "changes", "changes_abs") if c in self.columns["AccountInfo"]]
        storage_fields=list(IDENTITY)+fields
        history_scope=self.where(account)+" AND "+self.currency_where(account,preferred.get("currency"))
        sql = f"SELECT {','.join(storage_fields)} FROM {self.table('AccountInfo')} WHERE {history_scope} AND date_record<={sql_literal(cutoff,self.dialect)} ORDER BY date_record DESC,{','.join(IDENTITY)} LIMIT {MAX_ROWS+1}"
        raw_history=self.records("AccountInfo",sql,storage_fields)
        by_date={}
        for row in raw_history[:MAX_ROWS]:
            stamp=row["date_record"]
            if stamp not in by_date or self.variant_rank(row,preferred)<self.variant_rank(by_date[stamp],preferred):by_date[stamp]=row
        history=[by_date[stamp] for stamp in sorted(by_date,reverse=True)[:MAX_SNAPSHOTS]]
        result["history"]=[{field:row[field] for field in fields} for row in reversed(history)]
        #if len(raw_history)>MAX_ROWS:warnings.append("История ограничена при чтении повторных записей; показан доступный диапазон графика.")
        #if len(account.get("_variants",[]))>1:warnings.append("Записи счета объединены по login. Различия client_id, сервера, компании и типа не создают отдельные счета; одинаковые снимки не суммируются.")
        #if len(account.get("currencies",[]))>1:warnings.append("У login есть записи в разных валютах. Показан снимок и график в "+(result["account"]["currency"] or "валюте без обозначения")+"; суммы других валют в них не включаются.")
        if dates_limited:
            warnings.append(f"В списке дат показаны последние {MAX_SNAPSHOTS} снимков. Для более ранней истории выберите день в календаре. График истории ограничен {MAX_SNAPSHOTS} точками до выбранного момента.")
        if len(result["account_info"]) > 1:
            warnings.append("В AccountInfo несколько записей на один момент; в карточках показана первая. Все записи доступны в таблице счета.")
        result["warnings"] = warnings
        return result


def open_project_provider(project, mode, name):
    store = editor.ConfigStore(project)
    _, text = store.read(mode, name)
    config = editor.parse_ini(text)
    if not config.has_section("database"):
        raise editor.EditorError("В выбранном профиле нет секции [database]. Добавьте ее в редакторе.")
    database_type = config.get("database", "type", fallback="clickhouse")
    if database_type not in ("sqlite3", "postgresql", "clickhouse"):
        raise editor.EditorError("Этот тип базы не поддерживается.")
    if database_type == "sqlite3":
        db = project / "data/algobond.db"
        if not db.is_file():
            raise editor.EditorError("Файл data/algobond.db не найден. Пустая база автоматически не создается.")
    sys.path.insert(0, str(project))
    # The current package __init__ modules import one another. This is the
    # project's working initialization order; it does not create broker clients.
    import libs.analyzer.prepare  # noqa: F401
    from libs.configs.model import Configs
    from libs.interfaces.databases.database_provider import DatabaseProvider
    _cfg = Configs.parseConfigsettings(config, "", str(project))
    _dpr = DatabaseProvider(_cfg)
    conn = getattr(_dpr.db_config, "conn", None)
    if database_type == "sqlite3":
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
    elif database_type == "postgresql":
        conn.set_session(readonly=True, autocommit=False, isolation_level="REPEATABLE READ")
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 10000")
    probe = _dpr.selectDataList("SELECT 1")
    if not probe:
        raise editor.EditorError("База не ответила на проверочный запрос. Проверьте подключение.")
    return _dpr, _cfg


class MonitorService:
    def __init__(self, store, timeout=40):
        self.store = store
        self.timeout = timeout
        self.slots = threading.BoundedSemaphore(2)

    def run(self, payload):
        if not isinstance(payload, dict) or payload.get("action") not in ("catalog", "snapshot"):
            raise editor.EditorError("Неизвестный запрос мониторинга.")
        mode, name = payload.get("mode"), payload.get("name")
        self.store.read(mode, name)  # Validate mode, filename and confinement.
        for key in ("account", "date", "before", "at"):
            value = payload.get(key, "")
            if not isinstance(value, str) or len(value) > 100:
                raise editor.EditorError("Некорректный параметр мониторинга.")
        if not self.slots.acquire(blocking=False):
            raise editor.EditorError("Подключение уже выполняется. Повторите через несколько секунд.", 429)
        try:
            command = [sys.executable, "-I", "-B", "-X", "faulthandler", str(Path(__file__).resolve()), "--worker", "--project", str(self.store.project)]
            try:
                process = subprocess.run(command, input=json.dumps(payload).encode(), capture_output=True,
                                         timeout=self.timeout, cwd=str(self.store.project),
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired:
                raise editor.EditorError("База не ответила вовремя. Проверьте адрес, порт и доступность сервера.", 504) from None
            if process.returncode:
                code = process.returncode & 0xFFFFFFFF
                detail = f"0x{code:08X}"
                raise editor.EditorError(f"Процесс чтения базы завершился с кодом {detail}. Главное окно продолжает работать. Запустите diagnose_monitoring.py для диагностики.", 502)
            try:
                result = json.loads(process.stdout)
            except (ValueError, UnicodeDecodeError):
                raise editor.EditorError("Процесс базы вернул некорректный ответ.", 502) from None
            if not isinstance(result, dict):
                raise editor.EditorError("Процесс базы вернул неподдерживаемый ответ.", 502)
            if "error" in result:
                raise editor.EditorError(result["error"], result.get("status", 400))
            return result
        finally:
            self.slots.release()


def worker_main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    payload = json.loads(sys.stdin.buffer.read(8192))
    provider = None
    try:
        with contextlib.redirect_stdout(sys.stderr):
            provider, cfg = open_project_provider(args.project.resolve(), payload.get("mode"), payload.get("name"))
            reader = PortfolioReader(provider, cfg)
            if payload.get("action") == "catalog":
                result = reader.catalog()
            elif payload.get("action") == "snapshot":
                result = reader.snapshot(payload.get("account"), payload.get("date", ""), payload.get("before", ""), payload.get("at", ""))
                from algobond_orders import readOrdersSnapshot
                historical=any(payload.get(key) for key in ('date','before','at'))
                orders=readOrdersSnapshot(args.project,cfg.login,cfg.broker_name,result['selected_date'] if historical else '')
                result['orders']=orders['orders']
                result['source_dates']['orders']=orders['updated_at']
                if orders['warning']:result['warnings'].append(orders['warning'])
                result['warnings'].append('Заявки: локальный журнал последней проверки торговым процессом. Обновление страницы не запрашивает брокера.')
            else:
                raise editor.EditorError("Неизвестное действие.")
    except editor.EditorError as exc:
        result = dict(error=str(exc), status=exc.status)
    except (ImportError, ModuleNotFoundError):
        result = dict(error="Не удалось загрузить зависимости AlgoBond. Запустите приложение через Python из .venv проекта; проверьте diagnose_monitoring.py.", status=503)
    except Exception as exc:
        # Never return driver exception text: it may contain passwords/hosts.
        result = dict(error=f"Не удалось прочитать базу ({type(exc).__name__}). Проверьте реквизиты, доступ и таблицы.", status=502)
    finally:
        conn = getattr(getattr(provider, "db_config", None), "conn", None)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == "__main__":
    worker_main()
