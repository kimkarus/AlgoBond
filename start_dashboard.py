#!/usr/bin/env python3
"""AlgoBond home, portfolio monitor, config editor and autostart manager."""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from markdown_it import MarkdownIt

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import start_config_editor as editor
from libs.algobond_logging import LOG_FILES, configure_logging, log_failure, read_log_tail
from algobond_monitor import MonitorService
from algobond_autostart import JobManager, JobError, host_os, INTERVALS


def render_about_guide(source):
    # The web interface already provides a title and logo.
    _, heading, sections = source.partition("\n## ")
    markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
    html = markdown.render(heading + sections if heading else source)
    # Restore the README's named anchors without allowing other raw HTML.
    return re.sub(r'<p>&lt;a id=&quot;([a-z][a-z0-9-]*)&quot;&gt;&lt;/a&gt;</p>',
                  r'<a id="\1"></a>', html)


class DashboardHandler(editor.EditorHandler):
    server_version = "AlgoBondWorkspace/1.0"

    def do_GET(self):
        route = urlsplit(self.path).path
        if route == '/autostart' or route.startswith('/api/autostart/'):
            return self.autostart_get()
        if route not in ("/", "/monitor", "/logs", "/about", "/editor", "/api/profiles", "/api/logs"):
            return super().do_GET()
        try:
            self.trusted(api=route.startswith("/api/"))
            if route in ("/", "/monitor", "/logs", "/about"):
                page = (HERE / "dashboard.html").read_text(encoding="utf-8")
                page = page.replace("__NONCE__", self.server.nonce)
                if route == "/about":
                    guide = (HERE / "README_GITHUB.md").read_text(encoding="utf-8")
                    page = page.replace("<!-- ABOUT_GUIDE -->", render_about_guide(guide), 1)
                self.respond(page, kind="text/html; charset=utf-8", support_chat=route not in ("/logs", "/about"))
            elif route == "/api/logs":
                kind = parse_qs(urlsplit(self.path).query).get("kind", [""])[0]
                if kind not in LOG_FILES:
                    raise editor.EditorError("Неизвестный вид лога.")
                self.respond({"path": "logs/" + LOG_FILES[kind], **read_log_tail(self.server.store.project, kind)})
            elif route == "/editor":
                page = editor.HTML.replace("__NONCE__", self.server.nonce)
                page = page.replace('<div class="topbar-right">', '<div class="topbar-right"><a class="btn secondary" style="text-decoration:none" href="/">Главная</a>', 1)
                self.respond(page, kind="text/html; charset=utf-8")
            else:
                profiles = []
                for mode in editor.MODES:
                    for row in self.server.store.files(mode):
                        item = dict(row, mode=mode, database_type="", has_database=False)
                        try:
                            _, text = self.server.store.read(mode, row["name"])
                            config = editor.parse_ini(text)
                            item["has_database"] = config.has_section("database")
                            if item["has_database"]:
                                item["database_type"] = config.get("database", "type", fallback="clickhouse")
                        except editor.EditorError:
                            item["error"] = True
                        profiles.append(item)
                self.respond(dict(profiles=profiles))
        except editor.EditorError as exc:
            self.respond(dict(error=str(exc)), exc.status)
        except OSError as error:
            log_failure(logging.getLogger(__name__), "Не удалось прочитать файлы приложения", error)
            self.respond(dict(error="Не удалось прочитать файлы приложения."), 500)

    def do_POST(self):
        if self.path.startswith('/api/autostart/'):
            return self.autostart_post()
        if self.path != "/api/monitor":
            return super().do_POST()
        try:
            self.trusted(api=True)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise editor.EditorError("Ожидается JSON.", 415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192:
                    raise ValueError
            except ValueError:
                raise editor.EditorError("Некорректный размер запроса.", 413) from None
            self.connection.settimeout(10)
            try:
                payload = json.loads(self.rfile.read(size))
            except (ValueError, UnicodeDecodeError):
                raise editor.EditorError("Некорректный JSON.") from None
            self.respond(self.server.monitor.run(payload))
        except editor.EditorError as exc:
            self.respond(dict(error=str(exc)), exc.status)
        except OSError:
            self.respond(dict(error="Не удалось запустить процесс чтения базы."), 500)

    def autostart_get(self):
        try:
            route=urlsplit(self.path).path;self.trusted(api=route.startswith('/api/'))
            manager=self.server.autostart
            if route=='/autostart':
                self.respond((HERE/'autostart.html').read_text(encoding='utf-8').replace('__NONCE__',self.server.nonce),kind='text/html; charset=utf-8')
            elif route=='/api/autostart/boot':
                profiles=[]
                for mode in editor.MODES:
                    profiles.extend(dict(row,mode=mode) for row in self.server.store.files(mode))
                self.respond(dict(actions=manager.actions(),executables=manager.executables(),profiles=profiles,host_os=host_os(),project=str(manager.project),intervals=INTERVALS,
                                  defaults=dict(windows_python=str(manager.project/'.venv/Scripts/python.exe'),linux_project='/home/jupyter/dev/jupyterlab_ilya/kimkarus_ea_monitor_py'),
                                  notes=['Windows: текущий пользователь, Interactive/Limited; требуется вход пользователя в систему.', 'Linux: crontab текущего пользователя; cron должен быть установлен.', 'Пакет ZIP содержит install, run, enable, disable, remove и status для выбранной ОС.']))
            elif route=='/api/autostart/jobs':self.respond(dict(jobs=manager.list()))
            elif route=='/api/autostart/package':
                identity=parse_qs(urlsplit(self.path).query).get('id',[''])[0]
                self.respond(manager.package(identity),kind='application/zip')
            elif route=='/api/autostart/log':
                self.respond(manager.log_tail(parse_qs(urlsplit(self.path).query).get('id',[''])[0]))
            else:raise JobError('Не найдено.',404)
        except (JobError,editor.EditorError) as error:self.respond(dict(error=str(error)),error.status)
        except OSError:self.respond(dict(error='Нет доступа к файлам автозапусков.'),500)

    def autostart_post(self):
        try:
            self.trusted(api=True)
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise JobError('Ожидается JSON.',415)
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=16384:raise ValueError
                self.connection.settimeout(10);payload=json.loads(self.rfile.read(size))
            except (ValueError,UnicodeError):raise JobError('Некорректный JSON или размер запроса.') from None
            if not isinstance(payload,dict):raise JobError('Ожидается объект запроса.')
            manager=self.server.autostart;route=urlsplit(self.path).path
            if route=='/api/autostart/preview':result=manager.preview(payload.get('job'))
            elif route=='/api/autostart/save':result=manager.save(payload.get('job'),payload.get('revision'))
            elif route=='/api/autostart/run':result=manager.start_manual(payload.get('id'),payload.get('revision'))
            elif route=='/api/autostart/control':result=manager.control(payload.get('id'),payload.get('operation'),payload.get('revision'))
            elif route=='/api/autostart/archive':result=manager.archive_removed(payload.get('id'),payload.get('confirmed_remote') is True)
            else:raise JobError('Не найдено.',404)
            self.respond(result)
        except (JobError,editor.EditorError) as error:self.respond(dict(error=str(error),export_available=True),error.status)
        except OSError:self.respond(dict(error='Нет доступа к файлам или процессу. Скачайте пакет управления.',export_available=True),500)


class DashboardServer(editor.EditorServer):
    def __init__(self, address, store):
        super().__init__(address, store)
        self.RequestHandlerClass = DashboardHandler
        self.monitor = MonitorService(store)
        self.autostart = JobManager(store.project)


def main(argv=None, initial_view="home"):
    parser = argparse.ArgumentParser(description="AlgoBond: портфель, конфигурации, автозапуски, логи и руководство")
    parser.add_argument("--project", type=Path, default=HERE)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--view", choices=("home", "monitor", "autostart", "logs", "about"), default=initial_view,
                        help="Начальный раздел веб-интерфейса")
    args = parser.parse_args(argv)
    project = args.project.resolve()
    if not (project / "config_demo.ini").is_file():
        parser.error("Не найден config_demo.ini. Укажите корень проекта через --project.")
    if not 0 <= args.port <= 65535:
        parser.error("Порт должен быть от 0 до 65535.")
    if not (HERE / "dashboard.html").is_file():
        parser.error("dashboard.html должен находиться рядом со start_dashboard.py.")
    if not (HERE / "autostart.html").is_file():
        parser.error("autostart.html должен находиться рядом со start_dashboard.py.")
    configure_logging(project, startup=True)
    with DashboardServer(("127.0.0.1", args.port), editor.ConfigStore(project)) as server:
        route = "/" if args.view == "home" else "/" + args.view
        url = server.origin + route + "#token=" + server.token
        print("AlgoBond — портфель, конфигурации и автозапуски", flush=True)
        print(url, flush=True)
        print("Остановка веб-формы: Ctrl+C. Задания ОС управляются в разделе «Автозапуски».", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.4)
        except KeyboardInterrupt:
            print("\nAlgoBond остановлен.", flush=True)


if __name__ == "__main__":
    main()
