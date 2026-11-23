"""AlgoBond job definitions, current-user schedulers, export and isolated runs.

Standard library only. Importing this module never registers or starts a job.
"""
from __future__ import annotations

import argparse
import ast
import base64
import configparser
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

VERSION = 1
INTERVALS = (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60, 120, 180, 240, 360, 480, 720, 1440)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
ACTIONS = {
    "monitoring": dict(title="Мониторинг у брокера", script="start_monitoring.py", code=0, folder="monitoring", file_scope=False,
                       description="Получает данные у брокеров и выполняет мониторинг активных профилей папки monitoring. При наличии базы записывает состояние по правилам исходного AlgoBond. Это сборщик данных, а не веб-дашборд."),
    "trading": dict(title="Торговля", script="start_trading.py", code=1, folder="trading", file_scope=True,
                    description="Выполняет торговый сценарий исходного AlgoBond. Может создавать и изменять заявки в соответствии с настройками активных профилей."),
    "rebalancing": dict(title="Ребалансировка", script="start_rebalancing.py", code=5, folder="trading", file_scope=True,
                        description="Запускает ребалансировку портфеля по настройкам активных торговых профилей. Сценарий может выполнять брокерские операции."),
    "degradating": dict(title="Деградация позиций", script="start_degradating.py", code=6, folder="trading", file_scope=True,
                        description="Выполняет сценарий обработки деградирующих позиций. Условия продажи определяются конфигурацией и исходной торговой логикой."),
    "liquidity": dict(title="Фонд ликвидности", script="start_liquidity_fund.py", code=2, folder="trading", file_scope=False,
                      description="Управляет фондом ликвидности по настройкам активных торговых профилей. Может выставлять заявки. Отдельный INI этот скрипт не выбирает."),
    "calculate": dict(title="Расчёт", script="start_calculate.py", code=3, folder="monitoring_or_trading", file_scope=False,
                      description="Выполняет расчётный сценарий с подключением к брокерам. Сначала читает monitoring; если каталог совсем пуст, использует trading. Отдельный INI не выбирает."),
    "analytics": dict(title="Аналитика", script="start_analytics.py", code=0, folder="trading", file_scope=True,
                      description="Запускает класс Analytics для активных торговых профилей. Параметры брокера, клиента, login и INI передаются в штатном порядке аргументов."),
    "program": dict(title="Запуск программы целиком", script="start_algobond_full.py", code=-1, folder="trading", file_scope=True,
                     description="Запускает выбранную сборку AlgoBond целиком. Для demo используются её демонстрационная последовательность и вопросы в отдельной консоли."),
}
EDITIONS = {key:dict(key=key, title=key.upper(), executable='start_algobond_'+key+'.exe',
                    interactive=key=='demo') for key in ('demo','full','ofz','trial','trial_180')}


class JobError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def host_os():
    return "windows" if os.name == "nt" else "linux"


def text(value, label, maximum=260, empty=True):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise JobError(f"Некорректное поле: {label}.")
    value = value.strip()
    if not empty and not value:
        raise JobError(f"Заполните поле: {label}.")
    return value


def job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise JobError("Некорректный идентификатор задания.")
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def validate_cron(expression):
    expression = text(expression, "cron", 120, False)
    fields = expression.split()
    if len(fields) != 5:
        raise JobError("Cron должен содержать ровно пять полей времени, без команды.")
    for value, lower, upper in zip(fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)):
        for item in value.split(","):
            match = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", item)
            if not match:
                raise JobError("В cron разрешены числа, *, диапазоны, списки и шаг /.")
            base, step = match.groups()
            if step and not 1 <= int(step) <= upper - lower + 1:
                raise JobError("Недопустимый шаг cron.")
            if base != "*":
                ends = [int(x) for x in base.split("-")]
                if any(n < lower or n > upper for n in ends) or ends[0] > ends[-1]:
                    raise JobError("Значение cron вне допустимого диапазона.")
    return " ".join(fields)


def validate_schedule(raw, target_os):
    if not isinstance(raw, dict):
        raise JobError("Ожидается описание расписания.")
    kind = raw.get("kind", "daily")
    if kind not in ("daily", "weekly", "interval", "startup", "cron"):
        raise JobError("Неизвестный вид расписания.")
    result = dict(kind=kind)
    if kind in ("daily", "weekly"):
        clock = raw.get("time", "09:00")
        if not isinstance(clock, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
            raise JobError("Время должно быть в формате ЧЧ:ММ.")
        result["time"] = clock
    if kind == "weekly":
        days = raw.get("days", [])
        if not isinstance(days, list) or not days or any(type(d) is not int or d not in range(1, 8) for d in days):
            raise JobError("Выберите дни недели.")
        result["days"] = sorted(set(days))
    if kind == "interval":
        minutes = raw.get("minutes", 15)
        if type(minutes) is not int or minutes not in INTERVALS:
            raise JobError("Выберите интервал из списка; интервалы согласованы с часовыми границами cron.")
        result["minutes"] = minutes
    if kind == "cron":
        if target_os != "linux":
            raise JobError("Свободное cron-выражение доступно только для Linux.")
        result["expression"] = validate_cron(raw.get("expression", ""))
    return result


def cron_time(schedule):
    kind = schedule["kind"]
    if kind == "cron":
        return schedule["expression"]
    if kind == "startup":
        return "@reboot"
    if kind == "interval":
        minutes = schedule["minutes"]
        return f"*/{minutes} * * * *" if minutes < 60 else "0 * * * *" if minutes == 60 else f"0 */{minutes//60} * * *" if minutes < 1440 else "0 0 * * *"
    hour, minute = map(int, schedule["time"].split(":"))
    days = "*" if kind == "daily" else ",".join(str(d % 7) for d in schedule["days"])
    return f"{minute} {hour} * * {days}"


def schedule_description(job):
    schedule = job["schedule"]
    if schedule["kind"] == "interval":
        anchor = "первый запуск примерно через минуту после установки" if job['target']['os'] == 'windows' else "по границам часов и суток cron"
        return f"Каждые {schedule['minutes']} мин.; {anchor}; время ОС установки."
    if schedule["kind"] == "startup":
        return "При входе текущего пользователя в Windows." if job["target"]["os"] == "windows" else "При запуске cron (@reboot)."
    if schedule["kind"] == "cron":
        return "Cron: " + schedule["expression"] + "; время сервера установки."
    prefix = "Ежедневно" if schedule["kind"] == "daily" else ", ".join(DAY_LABELS[d-1] for d in schedule["days"])
    return prefix + " в " + schedule["time"] + "; местное время ОС установки."


class FileLease:
    def __init__(self, path, wait=0):
        self.path, self.wait, self.stream = Path(path), wait, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, 2)
        if not self.stream.tell():
            self.stream.write(b"0"); self.stream.flush()
        end = time.monotonic() + self.wait
        while True:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= end:
                    self.stream.close(); self.stream = None
                    raise JobError("Задание уже выполняется или конфигурация занята.", 409) from None
                time.sleep(.05)

    def __exit__(self, *args):
        if self.stream:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0); msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


def update_crontab(original, marker, line=None):
    """Replace only our exact marked block, preserving all unrelated bytes."""
    begin, end = "# BEGIN " + marker, "# END " + marker
    lines = original.splitlines(keepends=True)
    starts = [i for i, row in enumerate(lines) if row.rstrip("\r\n") == begin]
    ends = [i for i, row in enumerate(lines) if row.rstrip("\r\n") == end]
    if len(starts) != len(ends) or len(starts) > 1 or starts and starts[0] >= ends[0]:
        raise JobError("Поврежден управляемый блок cron; чужие строки не изменены.", 409)
    block = begin + "\n" + line + "\n" + end + "\n" if line else ""
    if starts:
        return "".join(lines[:starts[0]]) + block + "".join(lines[ends[0]+1:])
    return original + ("\n" if original and not original.endswith("\n") else "") + block


class JobManager:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.root = self.project / "data/autostart"
        self.mutex = threading.RLock()
        self.actions_cache = None

    def safe(self, relative):
        path = self.root / relative
        if not path.resolve().is_relative_to(self.project) or path.is_symlink():
            raise JobError("Перенаправленная папка автозапусков не поддерживается.", 403)
        return path

    def definition_path(self, identity):
        return self.safe("jobs/" + job_id(identity) + ".json")

    def actions(self):
        inventory=self.executables()
        fingerprint=tuple((name,(self.project/spec['script']).stat().st_mtime_ns if (self.project/spec['script']).is_file() else None) for name,spec in ACTIONS.items())+tuple((row['key'],row['available']) for row in inventory)
        if self.actions_cache and self.actions_cache[0]==fingerprint:
            return [dict(row) for row in self.actions_cache[1]]
        result = []
        for key, spec in ACTIONS.items():
            path = self.project / spec["script"]
            available = path.is_file() and not path.is_symlink() and path.resolve().parent == self.project
            reason = "" if available else "Файл не найден в проекте."
            if available:
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
                    if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "input" for n in ast.walk(tree)):
                        available, reason = False, "Скрипт содержит интерактивный ввод."
                except (OSError, SyntaxError, UnicodeError):
                    available, reason = False, "Не удалось прочитать Python-скрипт."
            result.append(dict(key=key, **spec, available=available, reason=reason,python_available=available,python_reason=reason))
        exe_actions=set()
        if host_os()=='windows':
            for row in inventory:
                if row['available']:exe_actions.update(row['actions'])
        for row in result:
            if row['key'] in exe_actions:row.update(available=True,reason='')
            elif row['key']=='program':row.update(available=False,reason='Для полного запуска выберите сборку EXE для Windows.')
            if row['key']=='program':row.update(python_available=False,python_reason='Для полного запуска выберите сборку EXE для Windows.')
        self.actions_cache=(fingerprint,result)
        return [dict(row) for row in result]

    def executables(self):
        result=[]
        for key,spec in EDITIONS.items():
            path=self.project/spec['executable']
            available=(self.project/'data').is_dir() and path.is_file() and not path.is_symlink() and path.resolve().parent==self.project
            actions=['program'] if key=='demo' else [name for name in ACTIONS if name!='analytics']
            result.append(dict(spec,available=available,actions=actions))
        return result

    def launch(self, job, require_exists=True):
        """Resolve only the five named binaries in the target project root."""
        edition=job.get('edition','auto');system=job['target']['os'];action=job['action']
        if edition!='auto' and edition not in EDITIONS:raise JobError('Выберите известный вариант EXE.')
        if system!='windows' and edition!='auto':raise JobError('Сборки EXE доступны только для Windows.')
        selected=None
        if system=='windows':
            if edition!='auto':
                selected=edition
                if require_exists and self.local(job) and not next(row for row in self.executables() if row['key']==edition)['available']:
                    raise JobError('В корне проекта рядом с data не найден '+EDITIONS[edition]['executable']+'.')
            elif action!='analytics' and self.local(job):
                available=[row for row in self.executables() if row['available']]
                if len(available)>1:raise JobError('Найдено несколько EXE. Выберите вариант программы в форме; автоматический выбор неоднозначен.')
                if available:selected=available[0]['key']
        spec=dict(ACTIONS[action])
        if selected:
            if action=='analytics':raise JobError('В перечисленных сборках EXE нет отдельного сценария Analytics. Выберите «Авто» для запуска start_analytics.py.')
            if selected=='demo' and action!='program':raise JobError('DEMO не принимает код отдельного действия. Выберите «Запуск программы целиком».')
            return dict(kind='exe',edition=selected,script=EDITIONS[selected]['executable'],folder='trading',file_scope=True,
                        interactive=selected=='demo',code=None if selected=='demo' else spec['code'],pending=False)
        if action=='program':raise JobError('Для полного запуска выберите EXE и вариант программы для Windows.')
        return dict(kind='python',edition=None,script=spec['script'],folder=spec['folder'],file_scope=spec['file_scope'],
                    interactive=False,code=spec['code'],pending=system=='windows' and not self.local(job))

    def normalize(self, raw, identity=None, check_available=True):
        if not isinstance(raw, dict):
            raise JobError("Ожидается объект задания.")
        action = raw.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            raise JobError("Выберите доступное действие.")
        spec = next(item for item in self.actions() if item["key"] == action)
        filters = raw.get("filters", {})
        target = raw.get("target", {})
        if not isinstance(filters, dict) or not isinstance(target, dict):
            raise JobError("Некорректные параметры задания.")
        system = target.get("os", host_os())
        if system not in ("windows", "linux"):
            raise JobError("Выберите Windows или Linux.")
        flavor = PureWindowsPath if system == "windows" else PurePosixPath
        project = text(target.get("project", str(self.project)), "папка проекта", 1000, False)
        if not flavor(project).is_absolute() or '..' in flavor(project).parts:
            raise JobError("Пути проекта и Python должны быть абсолютными, без переходов '..'.")
        if system == "windows" and str(flavor(project)).startswith('\\\\'):
            raise JobError("Для Windows укажите локальные пути с буквой диска.")
        python_value = target['python'] if 'python' in target else self.default_python(project, system)
        python = text(python_value, "Python", 1000, False)
        if not flavor(python).is_absolute() or '..' in flavor(python).parts:
            raise JobError("Пути проекта и Python должны быть абсолютными, без переходов '..'.")
        if system == "windows" and str(flavor(python)).startswith('\\\\'):
            raise JobError("Для Windows укажите локальные пути с буквой диска.")
        if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", flavor(python).name, re.I):
            raise JobError("Укажите исполняемый файл Python.")
        edition=raw.get('edition','auto')
        if not isinstance(edition,str) or edition not in ('auto',*EDITIONS):raise JobError('Выберите известный вариант EXE.')
        target=dict(os=system,project=str(flavor(project)),python=str(flavor(python)))
        provisional=dict(action=action,edition=edition,target=target)
        launch=self.launch(provisional) if check_available else dict(folder='trading' if edition!='auto' else spec['folder'],file_scope=spec['file_scope'] or edition!='auto',kind='exe' if edition!='auto' else 'python')
        if check_available and launch.get('edition'):edition=launch['edition']
        if check_available and launch['kind']=='python' and not spec['python_available']:raise JobError(spec['python_reason'])
        config = text(raw.get("config_name", ""), "INI", 160)
        if config:
            if not launch["file_scope"]:
                raise JobError("Этот исходный скрипт не поддерживает выбор одного INI. Используйте фильтры брокера, клиента и login.")
            path = self.project / "data/configs/accounts" / launch["folder"] / config
            if not config.lower().endswith('.ini') or Path(config).name != config or any(c in config for c in '/\\:') or path.is_symlink() or (check_available and not path.is_file()) or path.resolve().parent != path.parent.resolve() or not path.resolve().is_relative_to(self.project):
                raise JobError("Выберите существующий INI из нужной папки.")
        enabled = raw.get("enabled", True)
        if type(enabled) is not bool:
            raise JobError("Некорректный признак включения.")
        return dict(version=VERSION, id=job_id(identity or raw.get('id') or uuid.uuid4().hex),
                    title=text(raw.get("title", ""), "название", 120, False), action=action,
                    filters={key:text(filters.get(key, ""), key, 160) for key in ("broker", "client_id", "login")},
                    config_name=config, target=target, edition=edition,
                    schedule=validate_schedule(raw.get("schedule", {}), system), enabled=enabled, deleted=False)

    def read(self, identity, include_deleted=False):
        path = self.definition_path(identity)
        try:
            if path.stat().st_size > 32768:
                raise JobError("Описание задания слишком велико.")
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise JobError("Задание не найдено.", 404) from None
        except (ValueError, UnicodeError):
            raise JobError("Повреждено описание задания.") from None
        if not isinstance(raw,dict) or raw.get("id") != identity or raw.get("version") != VERSION:
            raise JobError("Неизвестный формат задания.")
        if raw.get('revision')!=digest({key:value for key,value in raw.items() if key!='revision'}):
            raise JobError('Файл задания изменён вне приложения или повреждён. Восстановите его из резервной копии.',409)
        if raw.get("deleted") and not include_deleted:
            raise JobError("Задание удалено.", 404)
        return raw

    def write_json(self, path, data):
        self.safe(path.relative_to(self.root))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.job-', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save(self, raw, expected=None):
        identity = raw.get("id") if isinstance(raw, dict) else None
        job = self.normalize(raw, identity)
        with self.mutex, FileLease(self.safe("locks/registry.lock"), 5):
            path = self.definition_path(job["id"])
            if path.exists():
                previous = self.read(job["id"])
                if expected != previous.get("revision"):
                    raise JobError("Задание изменено. Обновите список перед сохранением.", 409)
                if any(job['target'][key] != previous['target'][key] for key in ('os','project')):
                    raise JobError('ОС и папка проекта сохраняются за заданием. Создайте новое задание для другого компьютера; старое можно отключить или удалить.')
                self.write_json(self.safe('backups/'+job['id']+'_'+uuid.uuid4().hex+'.json'), previous)
                job["created_at"] = previous["created_at"]
            else:
                if identity and expected:
                    raise JobError("Задание уже удалено.", 409)
                job["created_at"] = now()
            job["updated_at"] = now(); job["revision"] = digest(job)
            self.write_json(path, job)
        return self.present(job)

    def patch_flags(self, identity, **flags):
        with self.mutex, FileLease(self.safe("locks/registry.lock"), 5):
            job = self.read(identity, include_deleted=True)
            job.update(flags, updated_at=now()); job.pop('revision', None);job['revision']=digest(job)
            self.write_json(self.definition_path(identity), job)
        return job

    def local(self, job):
        if job["target"]["os"] != host_os():
            return False
        return os.path.normcase(str(Path(job["target"]["project"]).resolve())) == os.path.normcase(str(self.project))

    def default_python(self, project, system):
        flavor = PureWindowsPath if system == 'windows' else PurePosixPath
        venv = flavor(project) / ('.venv/Scripts/python.exe' if system == 'windows' else '.venv/bin/python')
        if self.local(dict(target=dict(project=project, os=system))) and not Path(venv).is_file():
            return sys.executable or str(venv)
        return str(venv)

    def command(self, job, target=True, require_exists=True):
        spec = self.launch(job,require_exists=require_exists)
        root = (PureWindowsPath if job['target']['os']=='windows' else PurePosixPath)(job['target']['project']) if target else self.project
        python = job['target']['python']
        args = [str(root/spec['script'])] if spec['kind']=='exe' else [python, '-u', '-B', '-X', 'faulthandler', str(root/spec['script'])]
        if spec['code'] is not None:args.append(str(spec['code']))
        args.extend(job['filters'][key] for key in ('broker','client_id','login'))
        if job['config_name']:
            args.append('./data/configs/accounts/'+spec['folder']+'/'+job['config_name'])
        return args

    def identity(self, job):
        value=job['target']['project'].replace('\\','/')
        if job['target']['os']=='windows': value=value.casefold()
        namespace=hashlib.sha256(value.encode()).hexdigest()[:12]
        return 'AlgoBondUI_'+namespace+'_'+job['id']

    def scope(self, job):
        spec=self.launch(job);folder=spec['folder']
        if folder=='monitoring_or_trading':
            path=self.project/'data/configs/accounts/monitoring'
            folder='monitoring' if path.is_dir() and any(path.iterdir()) else 'trading'
        path=self.project/'data/configs/accounts'/folder;profiles=[];warnings=[]
        if not path.is_dir(): return dict(folder=folder,profiles=[],warnings=['Папка конфигураций отсутствует.'])
        if path.is_symlink() or not path.resolve().is_relative_to(self.project):raise JobError('Перенаправленная папка конфигураций не поддерживается.')
        for file in sorted(path.iterdir()):
            if job['config_name'] and file.name!=job['config_name']:continue
            if not file.is_file() or file.is_symlink() or not file.name.lower().endswith('.ini'):
                warnings.append('Исходный скрипт видит посторонний элемент: '+file.name);continue
            cfg=configparser.ConfigParser()
            try:
                cfg.read(file,encoding='utf-8-sig')
                if not cfg.has_section('client') or cfg.getint('client','active',fallback=0)<=0:continue
                values={key:cfg.get('client',key,fallback='') for key in ('broker_name','client_id','login','client_name')}
                if any(job['filters'][key] and job['filters'][key].casefold()!=values[{'broker':'broker_name','client_id':'client_id','login':'login'}[key]].casefold() for key in job['filters']):continue
                profiles.append(dict(file=file.name,label=values['client_name'] or file.stem,broker=values['broker_name'],login=values['login']))
            except (ValueError,configparser.Error,UnicodeError,OSError):warnings.append('Не удалось разобрать '+file.name)
        return dict(folder=folder,profiles=profiles,warnings=warnings)

    def status(self, identity):
        path=self.safe('status/'+job_id(identity)+'.json')
        try:
            result=json.loads(path.read_text(encoding='utf-8')) if path.stat().st_size<32768 else {}
            return result if isinstance(result,dict) else {}
        except (OSError,ValueError,UnicodeError):return {}

    def running(self, identity):
        path=self.safe('locks/'+job_id(identity)+'.lock')
        if not path.exists():return False
        try:
            with FileLease(path):return False
        except JobError:return True

    def preview(self, raw):
        job=self.normalize(raw)
        argv=self.command(job);scope=self.scope(job);launch=self.launch(job)
        spec=dict(ACTIONS[job['action']],script=launch['script'],folder=launch['folder'],file_scope=launch['file_scope'],code=launch['code'])
        notes=['Профили обрабатываются по собственной логике выбранной программы AlgoBond.',
               'Задание работает от текущего пользователя ОС. Время расписания — локальное время ОС установки.',
               'Повторный запуск одного задания пропускается, пока предыдущий выполняется.',
               'Отключение расписания не прерывает уже запущенный процесс.']
        if launch['kind']=='exe':notes.append('EXE имеет приоритет над Python-сценариями. Сборки start_algobond_* читают INI из trading. Python используется для менеджера заданий, торговую программу запускает EXE.')
        if launch['interactive']:notes.append('DEMO открывается в отдельной видимой консоли и ожидает ответы пользователя. Планировщик не отвечает на вопросы; до закрытия процесса повтор этого задания пропускается.')
        if launch['pending']:notes.append('Наличие EXE в другой целевой папке здесь не проверяется. Показана резервная Python-команда; окончательный выбор «Авто» выполняется на целевой машине. Для однозначного EXE-пакета выберите вариант явно.')
        local = self.local(job)
        if local and not Path(job['target']['python']).is_file():
            default = self.default_python(self.project, job['target']['os'])
            notes.append(f"Python менеджера по сохранённому пути не найден. В поле Python можно указать {default} и сохранить задание.")
        return dict(job=job,action=spec,launch=launch,argv=argv,
                    command=subprocess.list2cmdline(argv) if job['target']['os']=='windows' else shlex.join(argv),
                    working_directory=job['target']['project'],schedule_text=schedule_description(job),scope=scope,
                    local=local,task_name=self.identity(job),cron=cron_time(job['schedule']) if job['target']['os']=='linux' else None,
                    notes=notes)

    def present(self, job):
        result=dict(job)
        try:result['preview']=self.preview(job)
        except JobError as error:result['preview_error']=str(error)
        result['runtime']=self.status(job['id']);result['running']=self.running(job['id'])
        result['scheduler_definition']=self.scheduler_definition(job)
        return result

    def scheduler_definition(self, job):
        # The process reads the current JSON. These fields are embedded in the OS task.
        return digest({key:job[key] for key in ('target','schedule')})

    def list(self):
        folder=self.safe('jobs');result=[]
        if not folder.exists():return result
        for file in sorted(folder.glob('*.json')):
            try:
                job=self.read(file.stem,include_deleted=True)
                result.append(self.present(job))
            except JobError:continue
        return sorted(result,key=lambda job:job.get('updated_at',''),reverse=True)

    def write_status(self, identity, **values):
        with FileLease(self.safe('locks/status_'+job_id(identity)+'.lock'), 5):
            state=self.status(identity);state.update(values)
            self.write_json(self.safe('status/'+identity+'.json'),state)

    def runner_command(self, job, manual=False):
        flavor=PureWindowsPath if job['target']['os']=='windows' else PurePosixPath
        args=[job['target']['python'],'-u','-B',str(flavor(job['target']['project'])/'algobond_autostart.py'),'run','--project',job['target']['project'],'--id',job['id']]
        if manual:args.append('--manual')
        return args

    def cron_line(self, job):
        command=shlex.join(self.runner_command(job)).replace('%',r'\%')
        return cron_time(job['schedule'])+' '+command

    def windows_program(self, job, operation):
        if operation not in ('install','inspect','disable','enable','remove'):
            raise JobError('Неизвестная операция планировщика.')
        name=self.identity(job);marker='AlgoBondUI|'+name+'|'
        prefix="$ErrorActionPreference='Stop'\n$Name="+ps_quote(name)+"\n$Marker="+ps_quote(marker)+"\n"
        prefix+="$Task=Get-ScheduledTask -TaskPath '\\' -ErrorAction Stop | Where-Object {$_.TaskName -eq $Name} | Select-Object -First 1\n"
        prefix+="if($Task -and -not $Task.Description.StartsWith($Marker)){throw 'Refusing to modify a task not owned by this AlgoBond job'}\n"
        if operation=='inspect':
            return prefix+"if($Task){$Info=Get-ScheduledTaskInfo -TaskName $Name -TaskPath '\\';@{exists=$true;enabled=($Task.State -ne 'Disabled');state=[string]$Task.State;next_run=[string]$Info.NextRunTime;last_result=$Info.LastTaskResult}|ConvertTo-Json -Compress}else{@{exists=$false;enabled=$false}|ConvertTo-Json -Compress}\n"
        if operation in ('disable','enable','remove'):
            verb={'disable':'Disable-ScheduledTask','enable':'Enable-ScheduledTask','remove':'Unregister-ScheduledTask'}[operation]
            extra=' -Confirm:$false' if operation=='remove' else ''
            existence='$false' if operation=='remove' else '([bool]$Task)'
            enabled='([bool]$Task)' if operation=='enable' else '$false'
            return prefix+f"if($Task){{{verb} -TaskName $Name -TaskPath '\\'{extra} | Out-Null}}\n@{{exists={existence};enabled={enabled}}}|ConvertTo-Json -Compress\n"
        args=self.runner_command(job);schedule=job['schedule'];kind=schedule['kind']
        code=prefix+"$User=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name\n"
        code+="$Action=New-ScheduledTaskAction -Execute "+ps_quote(args[0])+" -Argument "+ps_quote(subprocess.list2cmdline(args[1:]))+" -WorkingDirectory "+ps_quote(job['target']['project'])+"\n"
        if kind=='daily':trigger="New-ScheduledTaskTrigger -Daily -At "+ps_quote(schedule['time'])
        elif kind=='weekly':trigger="New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek "+','.join(DAYS[d-1] for d in schedule['days'])+" -At "+ps_quote(schedule['time'])
        elif kind=='interval':trigger=f"New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes {schedule['minutes']})"
        elif kind=='startup':trigger="New-ScheduledTaskTrigger -AtLogOn -User $User"
        else:raise JobError('Cron-выражение не переводится в триггер Windows.')
        code+="$Trigger="+trigger+"\n$Principal=New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited\n"
        code+="$Settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries\n"
        code+="$Definition=New-ScheduledTask -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description ($Marker+"+ps_quote(job['title'])+")\n"
        code+="Register-ScheduledTask -TaskName $Name -TaskPath '\\' -InputObject $Definition -Force | Out-Null\n@{exists=$true;enabled=$true}|ConvertTo-Json -Compress\n"
        return code

    def native(self, job, operation):
        if not self.local(job):
            raise JobError('Задание предназначено для другой ОС или папки. Скачайте пакет и выполните нужный скрипт на целевом компьютере.',409)
        if job['target']['os']=='windows':
            executable=shutil.which('powershell.exe')
            if not executable:raise JobError('Windows PowerShell недоступен. Используйте пакет установки.',503)
            encoded=base64.b64encode(self.windows_program(job,operation).encode('utf-16le')).decode('ascii')
            command=[executable,'-NoProfile','-NonInteractive','-EncodedCommand',encoded]
            result=self.call_native(command)
            try:state=json.loads(result)
            except ValueError:raise JobError('Планировщик вернул непонятный ответ. Выполните проверку состояния.',502) from None
            if not isinstance(state,dict) or type(state.get('exists')) is not bool or type(state.get('enabled')) is not bool:
                raise JobError('Планировщик вернул непонятный ответ. Выполните проверку состояния.',502)
        else:
            executable=shutil.which('crontab')
            if not executable:raise JobError('Команда crontab не найдена. Установите cron на целевой Linux или используйте экспортированный пакет.',503)
            with FileLease(self.safe('locks/cron.lock'),5):
                previous=self.read_cron(executable)
                marker=self.identity(job)
                if operation=='inspect':state=dict(exists=('# BEGIN '+marker) in previous.splitlines(),enabled=('# BEGIN '+marker) in previous.splitlines())
                else:
                    desired=update_crontab(previous,marker,self.cron_line(job) if operation in ('install','enable') else None)
                    backup=self.safe('backups/crontab_'+uuid.uuid4().hex+'.txt');backup.parent.mkdir(parents=True,exist_ok=True);backup.write_text(previous,encoding='utf-8')
                    if self.read_cron(executable)!=previous:raise JobError('Crontab изменён извне. Повторите операцию.',409)
                    self.call_native([executable,'-'],desired)
                    state=dict(exists=operation in ('install','enable'),enabled=operation in ('install','enable'))
        values=dict(scheduler=state,scheduler_checked_at=now())
        if operation=='install' or (job['target']['os']=='linux' and operation=='enable'):
            values['scheduler_definition']=self.scheduler_definition(job)
        self.write_status(job['id'],**values)
        return state

    def call_native(self, command, stdin=None):
        try:
            process=subprocess.run(command,input=stdin.encode('utf-8') if stdin is not None else None,capture_output=True,timeout=25,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except (FileNotFoundError,PermissionError,subprocess.TimeoutExpired):
            raise JobError('Нет доступа к планировщику или превышено время ожидания. Скачайте пакет установки/управления.',503) from None
        if process.returncode:
            detail=process.stderr.decode('utf-8','replace').strip()[-1200:]
            raise JobError('Планировщик отклонил операцию. Используйте пакет управления. '+detail,503)
        return process.stdout.decode('utf-8-sig','replace').strip()

    def read_cron(self, executable):
        env=dict(os.environ,LC_ALL='C')
        try:process=subprocess.run([executable,'-l'],capture_output=True,timeout=10,env=env)
        except (OSError,subprocess.TimeoutExpired):raise JobError('Не удалось прочитать crontab.',503) from None
        if process.returncode==0:
            try:return process.stdout.decode('utf-8')
            except UnicodeError:raise JobError('Crontab имеет кодировку, отличную от UTF-8. Изменение отменено.',503) from None
        if process.returncode==1 and b'no crontab for' in process.stderr.lower():return ''
        raise JobError('Не удалось прочитать crontab. Существующие задания не изменены.',503)

    def control(self, identity, operation, revision=None):
        with FileLease(self.safe('locks/control_'+job_id(identity)+'.lock'),5):
            return self._control(identity,operation,revision)

    def _control(self, identity, operation, revision=None):
        job=self.read(identity,include_deleted=operation=='remove')
        if revision is not None and revision!=job.get('revision'):raise JobError('Задание изменено. Обновите список.',409)
        if operation=='inspect':return self.native(job,operation)
        if operation=='install':
            self.normalize(job);self.ensure_local_paths(job)
            state=self.native(job,'install')
            try:
                if not job['enabled']:self.patch_flags(identity,enabled=True)
            except Exception:
                with contextlib.suppress(JobError):self.native(job,'disable')
                raise
            return dict(job=self.present(self.read(identity)),scheduler=state)
        if operation=='disable':
            job=self.patch_flags(identity,enabled=False);warning=''
            try:self.native(job,'disable')
            except JobError as error:warning=str(error)
            return dict(job=self.present(job),warning=warning)
        if operation=='enable':
            if not self.local(job):
                job=self.patch_flags(identity,enabled=True)
                return dict(job=self.present(job),warning='Конфигурация разрешена в этой копии. Выполните enable из пакета на целевом компьютере; его состояние отсюда не проверено.')
            self.normalize(job);self.ensure_local_paths(job)
            state=self.native(job,'enable' if job['target']['os']=='windows' else 'install')
            if not state.get('exists'):raise JobError('Задание ещё не установлено. Нажмите «Установить».',409)
            job=self.patch_flags(identity,enabled=True)
            return dict(job=self.present(job),scheduler=state)
        if operation=='remove':
            job=self.patch_flags(identity,enabled=False,deleted=True)
            try:self.native(job,'remove')
            except JobError as error:return dict(job=self.present(job),warning=str(error),needs_cleanup=True)
            with self.mutex,FileLease(self.safe('locks/registry.lock'),5):
                current=self.read(identity,include_deleted=True);self.write_json(self.safe('deleted/'+identity+'.json'),current);self.definition_path(identity).unlink()
            return dict(deleted=True,id=identity)
        raise JobError('Неизвестная операция.')

    def ensure_local_paths(self, job):
        if not self.local(job):raise JobError('Используйте пакет на целевой ОС и в указанной папке проекта.',409)
        if not Path(job['target']['python']).is_file():
            raise JobError('Python менеджера заданий не найден. Укажите существующий путь в поле «Python менеджера заданий на целевом компьютере» и сохраните задание.')
        if not (self.project/'algobond_autostart.py').is_file():raise JobError('Добавьте algobond_autostart.py в корень проекта перед установкой.')

    def import_definition(self, raw, cleanup=False, replace=False):
        if not isinstance(raw,dict):raise JobError('Ожидается объект задания.')
        if raw.get('version')!=VERSION or raw.get('revision')!=digest({key:value for key,value in raw.items() if key!='revision'}):raise JobError('Повреждён пакет задания.')
        identity=job_id(raw.get('id'));path=self.definition_path(identity)
        if path.exists():
            current=self.read(identity,include_deleted=True)
            if cleanup:return current
            fields=('title','action','filters','config_name','target','schedule')
            if current.get('revision')==raw.get('revision') or (all(current.get(key)==raw.get(key) for key in fields) and current.get('edition','auto')==raw.get('edition','auto')):return current
            if not replace:raise JobError('Описание отличается от существующего. Для явного обновления пакетом нужен --replace; прежняя версия будет сохранена.',409)
        job=self.normalize(raw,identity,check_available=not cleanup)
        if not self.local(job):raise JobError('В пакете другая целевая папка или ОС.')
        if raw.get('deleted') and not cleanup:raise JobError('Удалённое задание нельзя установить повторно.')
        job.update(created_at=raw.get('created_at',now()),updated_at=raw.get('updated_at',now()))
        if cleanup:job.update(deleted=True,enabled=False)
        job['revision']=digest(job)
        with self.mutex,FileLease(self.safe('locks/registry.lock'),5):
            if path.exists():
                if not replace:raise JobError('Задание уже появилось; повторите импорт.',409)
                self.write_json(self.safe('backups/'+identity+'_'+uuid.uuid4().hex+'.json'),self.read(identity,include_deleted=True))
            self.write_json(path,job)
        return job

    def archive_removed(self, identity, confirmed_remote=False):
        job=self.read(identity,include_deleted=True)
        if not job.get('deleted'):raise JobError('Сначала удалите задание.')
        if self.local(job):
            if self.native(job,'inspect').get('exists'):raise JobError('Задание ОС ещё существует; выполните remove из пакета.',409)
        elif confirmed_remote is not True:raise JobError('Подтвердите выполнение remove на целевом компьютере.',409)
        with self.mutex,FileLease(self.safe('locks/registry.lock'),5):
            self.write_json(self.safe('deleted/'+identity+'.json'),job);self.definition_path(identity).unlink()
        return dict(deleted=True,id=identity)

    def package(self, identity):
        job=self.read(identity,include_deleted=True);system=job['target']['os'];flavor=PureWindowsPath if system=='windows' else PurePosixPath
        root,python=job['target']['project'],job['target']['python'];runner=str(flavor(root)/'algobond_autostart.py')
        readme=self.instructions(job)
        files={'job.json':json.dumps(job,ensure_ascii=False,indent=2).encode('utf-8'),'algobond_autostart.py':Path(__file__).read_bytes(),'README.txt':readme.encode('utf-8')}
        for name,operation in [('install','install'),('enable','enable'),('disable','disable'),('remove','remove'),('run','run'),('status','inspect')]:
            if system=='windows':
                lines=["$ErrorActionPreference='Stop'",'$Root='+ps_quote(root),'$Python='+ps_quote(python),'$Runner='+ps_quote(runner),"if(-not (Test-Path -LiteralPath $Root -PathType Container)){throw 'Project directory does not exist'}",
                       "$Source=Join-Path $PSScriptRoot 'algobond_autostart.py'",
                       "if([IO.Path]::GetFullPath($Source) -ne [IO.Path]::GetFullPath($Runner)){",
                       "  if(-not (Test-Path -LiteralPath $Runner) -or (Get-FileHash -LiteralPath $Source).Hash -ne (Get-FileHash -LiteralPath $Runner).Hash){",
                       "    if(Test-Path -LiteralPath $Runner){$BackupDir=Join-Path $Root 'data/autostart/backups';[void][IO.Directory]::CreateDirectory($BackupDir);Copy-Item -LiteralPath $Runner -Destination (Join-Path $BackupDir ('manager_'+[guid]::NewGuid().ToString('N')+'.py'))}",
                       "    Copy-Item -LiteralPath $Source -Destination $Runner -Force",
                       "  }", "}"]
                if operation=='install':lines.append("& $Python $Runner import-definition --project $Root --file (Join-Path $PSScriptRoot 'job.json') --replace")
                elif operation=='remove':lines.append("& $Python $Runner import-definition --project $Root --file (Join-Path $PSScriptRoot 'job.json') --cleanup")
                elif operation=='run':lines.append("& $Python $Runner import-definition --project $Root --file (Join-Path $PSScriptRoot 'job.json')")
                lines.append("if($LASTEXITCODE -and $LASTEXITCODE -ne 0){exit $LASTEXITCODE}")
                lines.append('& $Python $Runner '+operation+' --project $Root --id '+ps_quote(identity)+(' --manual' if operation=='run' else ''))
                lines.append('exit $LASTEXITCODE')
                files[name+'.ps1']=b'\xef\xbb\xbf'+('\r\n'.join(lines)+'\r\n').encode('utf-8')
            else:
                lines=['#!/bin/sh','set -eu','ROOT='+shlex.quote(root),'PYTHON='+shlex.quote(python),'RUNNER='+shlex.quote(runner),'HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)','test -d "$ROOT" || { echo "Project directory does not exist" >&2; exit 1; }','if [ ! -f "$RUNNER" ]; then cp -- "$HERE/algobond_autostart.py" "$RUNNER"; fi']
                if operation in ('install','remove'):lines.append('"$PYTHON" "$RUNNER" import-definition --project "$ROOT" --file "$HERE/job.json"'+(' --cleanup' if operation=='remove' else ' --replace'))
                elif operation=='run':lines.append('"$PYTHON" "$RUNNER" import-definition --project "$ROOT" --file "$HERE/job.json"')
                lines.append('exec "$PYTHON" "$RUNNER" '+operation+' --project "$ROOT" --id '+shlex.quote(identity)+(' --manual' if operation=='run' else ''))
                files[name+'.sh']=('\n'.join(lines)+'\n').encode('utf-8')
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,content in files.items():
                item=zipfile.ZipInfo(name);item.external_attr=(0o100755 if name.endswith('.sh') else 0o100644)<<16;item.compress_type=zipfile.ZIP_DEFLATED;archive.writestr(item,content)
        return buffer.getvalue()

    def instructions(self, job):
        launch=self.launch(job,require_exists=False);spec=ACTIONS[job['action']];command=self.command(job,require_exists=False)
        return '\n'.join([job['title'],spec['description'],'',f"Программа: {launch['script']}; код действия: {launch['code'] if launch['code'] is not None else 'не передаётся'}",
            'Исполнение: '+launch['kind']+'; файл: '+launch['script']+'; вариант: '+(launch['edition'] or 'отдельный Python-сценарий'),
            'DEMO: видимая консоль с вопросами; требуется пользователь.' if launch['interactive'] else 'Запуск без интерактивной консоли менеджера.',
            'Фильтры: '+json.dumps(job['filters'],ensure_ascii=False),'INI: '+(job['config_name'] or 'все подходящие активные профили'),
            'Расписание: '+schedule_description(job),'Папка: '+job['target']['project'],'Python: '+job['target']['python'],
            'Команда процесса: '+(subprocess.list2cmdline(command) if job['target']['os']=='windows' else shlex.join(command)),
            '', 'install — импортировать конфигурацию и добавить/обновить задание текущего пользователя ОС. Прежняя JSON-версия сохраняется в backups.',
            'run — ручной запуск; disable — запретить следующие пуски; enable — включить; remove — удалить наше задание; status — проверить.',
            'Запустите .ps1 в Windows PowerShell или .sh через sh на целевой Linux. Python нужен менеджеру заданий; EXE запускается отдельным процессом вместо торгового .py. Windows-пакет обновляет файл менеджера с резервной копией прежней версии.',
            'Windows: Interactive/Limited, только пока пользователь вошёл в систему. Пакет не повышает права автоматически.',
            'Linux: используется crontab текущего пользователя; cron должен быть установлен. @reboot требует поддержки cron.',
            'Отключение не прерывает уже запущенный процесс. Повтор одного задания пропускается, пока предыдущий работает.',
            'Пакет не содержит брокерских токенов или паролей; INI читаются из проекта на целевой машине.',
            'Если доступ к планировщику запрещён, выполните пакет с разрешённой для этой операции учётной записью.', ''])

    def start_manual(self, identity, revision=None):
        job=self.read(identity)
        if revision is not None and revision!=job['revision']:raise JobError('Задание изменено. Обновите список.',409)
        self.normalize(job);self.ensure_local_paths(job)
        if self.running(identity):raise JobError('Это задание уже выполняется.',409)
        launch=self.safe('logs/'+identity+'_launcher.log');launch.parent.mkdir(parents=True,exist_ok=True)
        with launch.open('ab') as stream:
            process=subprocess.Popen(self.runner_command(job,manual=True),cwd=self.project,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),start_new_session=os.name!='nt')
        return dict(requested=True,pid=process.pid,id=identity)

    def run(self, identity, manual=False):
        lease=FileLease(self.safe('locks/'+job_id(identity)+'.lock'))
        try:
            lease.__enter__()
        except JobError as error:
            if error.status==409:return dict(skipped=True,reason='already_running')
            raise
        try:
            job=self.read(identity)
            if not manual and not job['enabled']:return dict(skipped=True,reason='disabled')
            self.normalize(job);self.ensure_local_paths(job);launch=self.launch(job)
            started=now();name=identity+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:8]+'.log';log=self.safe('logs/'+name);log.parent.mkdir(parents=True,exist_ok=True)
            self.write_status(identity,running=True,started_at=started,finished_at=None,exit_code=None,log=name,manual=manual,error=None)
            try:
                with log.open('wb') as stream:
                    if launch['interactive']:
                        stream.write('DEMO запускается в отдельной консоли. Ответы вводятся в её окне; вывод программы остаётся в консоли.\n'.encode('utf-8'));stream.flush()
                    process=subprocess.Popen(self.command(job,target=False),cwd=self.project,
                        stdin=None if launch['interactive'] else subprocess.DEVNULL,
                        stdout=None if launch['interactive'] else stream,stderr=None if launch['interactive'] else subprocess.STDOUT,
                        creationflags=getattr(subprocess,'CREATE_NEW_CONSOLE',0) if launch['interactive'] else getattr(subprocess,'CREATE_NO_WINDOW',0),
                        start_new_session=os.name!='nt',env=dict(os.environ,PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1'))
                    # A transient status-file error must not release the job lock
                    # while its trading process is still running.
                    with contextlib.suppress(OSError,JobError):
                        self.write_status(identity,pid=process.pid,runner_pid=os.getpid())
                    code=process.wait()
                self.write_status(identity,running=False,finished_at=now(),exit_code=code)
                return dict(exit_code=code,log=name)
            except OSError as error:
                self.write_status(identity,running=False,finished_at=now(),exit_code=None,error=type(error).__name__)
                raise JobError('Не удалось запустить выбранный процесс EXE/Python.',503) from None
        finally:
            lease.__exit__()

    def log_tail(self, identity):
        job=self.read(identity,include_deleted=True);name=self.status(identity).get('log','')
        if not re.fullmatch(identity+r'_\d{8}T\d{6}_[a-f0-9]{8}\.log',name):return dict(text='',log=None)
        path=self.safe('logs/'+name)
        try:
            with path.open('rb') as stream:stream.seek(max(0,path.stat().st_size-16000));value=stream.read().decode('utf-8','replace')
        except OSError:return dict(text='',log=name)
        for folder in ('monitoring','trading'):
            for config in (self.project/'data/configs/accounts'/folder).glob('*.ini'):
                parser=configparser.ConfigParser(interpolation=None)
                try:parser.read(config,encoding='utf-8-sig')
                except (configparser.Error,UnicodeError,OSError):continue
                for section in parser.sections():
                    for key,secret in parser.items(section):
                        if any(word in key for word in ('token','password','license_key','secret')) and len(secret)>=8:value=value.replace(secret,'••••••••')
        return dict(text=value,log=name)


def main(argv=None):
    parser=argparse.ArgumentParser(description='AlgoBond — управление регламентными заданиями')
    parser.add_argument('operation',choices=('list','preview','import-definition','install','inspect','disable','enable','remove','run','export'))
    parser.add_argument('--project',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--id');parser.add_argument('--file',type=Path);parser.add_argument('--output',type=Path)
    parser.add_argument('--manual',action='store_true');parser.add_argument('--cleanup',action='store_true');parser.add_argument('--replace',action='store_true')
    args=parser.parse_args(argv);manager=JobManager(args.project)
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    try:
        if args.operation=='list':result=manager.list()
        elif args.operation=='preview':result=manager.preview(json.loads(args.file.read_text(encoding='utf-8')))
        elif args.operation=='import-definition':result=manager.import_definition(json.loads(args.file.read_text(encoding='utf-8')),args.cleanup,args.replace)
        elif args.operation=='run':result=manager.run(job_id(args.id),args.manual)
        elif args.operation=='export':
            if not args.output:raise JobError('Укажите --output для ZIP.')
            args.output.write_bytes(manager.package(job_id(args.id)));result=dict(file=str(args.output))
        else:result=manager.control(job_id(args.id),args.operation)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return result.get('exit_code',0) if isinstance(result,dict) and isinstance(result.get('exit_code'),int) else 0
    except (JobError,OSError,ValueError) as error:
        print(json.dumps(dict(error=str(error)),ensure_ascii=False));return 1


if __name__=='__main__':
    raise SystemExit(main())
