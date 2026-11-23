#!/usr/bin/env python3
"""AlgoBond configuration editor. Python 3.10+, standard library only.

Run beside config_demo.ini, or pass --project PATH. This application never
imports or runs AlgoBond trading code. Files change only on explicit Save.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import webbrowser
from codecs import BOM_UTF8
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


MAX_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 6 * MAX_BYTES + 4096  # Allow for JSON escaping around an INI file.
MODES = {"monitoring": "Мониторинг", "trading": "Торговля"}
SECRET_WORDS = ("token", "password", "license_key", "api_key", "secret")
SUPPORT_WIDGET_URL = (
    "https://pyrus.com/js/webwidget?id="
    "9Hb21Vmj~roFSl8P~PU7rdyF4Ss5XPbtLnEHpnhE4qB2reil~qKhgWOVtu~~d6TxKn4QBTvMQbPOqYcaapYYcWTpGT7TCiCJH7oF2a4GrGxnv7bKyxkwbq44t7qG7gQ7mwnkBA%3D%3D"
)


def with_support_chat(page, nonce):
    """Load the vendor widget after the app has consumed its URL token."""
    if 'id="algobond-support-chat"' in page:
        return page
    script = (
        '<script id="algobond-support-chat" type="text/javascript" '
        f'nonce="{nonce}" src="{SUPPORT_WIDGET_URL}" async></script>'
    )
    return page.replace("</body>", script + "</body>", 1)


def content_security_policy(nonce, support_chat=False):
    pyrus = " https://pyrus.com https://*.pyrus.com" if support_chat else ""
    sockets = " wss://pyrus.com wss://*.pyrus.com" if support_chat else ""
    return (
        f"default-src 'self'; script-src 'nonce-{nonce}'{pyrus}; "
        f"style-src 'self' 'unsafe-inline'{pyrus}; img-src 'self' data:{pyrus}; "
        f"connect-src 'self'{pyrus}{sockets}; frame-src 'self'{pyrus}; "
        f"font-src 'self' data:{pyrus}; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )


def field(section, key, label, kind="text", default="", **kw):
    return dict(section=section, key=key, id=f"{section}.{key}", label=label,
                kind=kind, default=str(default), **kw)


def setting(key, label, kind="number", default="", **kw):
    return field("settings", key, label, kind, default, **kw)


GROUPS = [
    dict(id="profile", title="Профиль и подключение", subtitle="Счет, брокер и доступ к API", icon="user", groups=[
        dict(title="Основные сведения", open=True, fields=[
            field("client", "active", "Профиль активен", "active", "0", help="Редактор сам не запускает торговлю. Активность используется скриптами AlgoBond."),
            field("client", "client_name", "Название портфеля"),
            field("client", "broker_name", "Брокер", "select", "Finam", options=["Finam", "Alor", "Tinkoff", "BCS"]),
            field("client", "broker_type_api", "Подключение", "select", "grpc", options=["grpc", "rest"]),
            field("client", "login", "Внутренний логин", help="Также используется в имени персональной папки параметров."),
            field("client", "client_id", "ID клиента"),
            field("client", "type_account", "Тип инструментов", "select", "Bonds", options=["Bonds"]),
            field("client", "broker_company", "Брокерская компания"),
            field("client", "broker_server", "Сервер / обозначение подключения"),
            field("client", "account_id", "Номер счета"),
            field("client", "account_id_grpc", "Номер счета gRPC"),
        ]),
        dict(title="Ключи и авторизация", fields=[
            field("program", "license_key", "Лицензионный ключ", "secret"),
            field("client", "token", "API-токен", "secret"),
            field("client", "token_grpc", "API-токен gRPC", "secret"),
            field("client", "enable_x_app_name", "Передавать имя приложения", "bool", "False"),
        ]),
        dict(title="Автоследование", fields=[
            field("client", "enable_autofollow_strategy", "Автоследование Т-Банка", "bool", "False"),
            field("client", "strategy_id", "ID стратегии"),
        ]),
    ]),
    dict(id="strategy", title="Стратегия и риск", subtitle="Отбор облигаций и размер позиции", icon="chart", groups=[
        dict(title="Распределение капитала", open=True, fields=[
            setting("portfolio_max_percent_bonds", "Доля облигаций", "range", "100", min=0, max=100, step=1, unit="%"),
            setting("p_risk", "Доля одной позиции", "range", "2.9", min=0, max=100, step=0.1, unit="%", help="Параметр размера позиции, а не гарантированный предел убытка."),
            setting("k_count", "Коэффициент количества", default="1", min=0, step=0.1),
            setting("k_share_balance", "Используемая доля баланса", "range", "1", min=0, max=1, step=0.01),
            setting("untouchable_balance_of_funds", "Резерв средств", default="0", min=0, step=1000, unit="₽"),
            setting("max_count_lot", "Максимальное количество лотов", "integer", "100", min=1, step=1),
            setting("boards", "Доски облигаций", "boards", "TQCB,TQIR,TQOB", options=["TQCB", "TQIR", "TQOB"]),
            setting("boards_synthetic", "Другие доски", "text", ""),
            setting("portfolio_max_percent_syntheticbonds", "Лимит других инструментов", default="0", min=0, max=100, unit="%", help="В текущем парсере этот параметр обрабатывается как логическое значение. Проверьте поддержку перед применением."),
        ]),
        dict(title="Фильтры инструментов", open=True, fields=[
            setting("days_planning_min", "До погашения от", default="0", min=0, step=1, unit="дней"),
            setting("days_planning_max", "До погашения до", default="30", min=0, step=1, unit="дней"),
            setting("low_price", "Минимальная цена", default="65", min=0, step=0.1, unit="% ном."),
            setting("low_couponpercent", "Минимальный купон", default="0", min=0, step=0.1, unit="%"),
            setting("enable_profitability", "Фильтр доходности", "bool", "False"),
            setting("profitability", "Минимальная доходность", default="15", min=0, step=0.1, unit="%"),
            setting("target_percent_peryear", "Целевая годовая доходность", default="30", min=0, step=0.1, unit="%"),
            setting("tax", "Ставка налога", "range", "0.13", min=0, max=1, step=0.01, help="Доля: 0.13 означает 13%."),
            setting("broker_comission_perdeal", "Расчетная комиссия сделки", default="50", min=0, step=1, unit="₽"),
        ]),
        dict(title="Списки и подготовленные данные", fields=[
            setting("enable_symbols_whitelist", "Белый список инструментов", "bool", "False"),
            setting("enable_symbols_whitelist_for_login", "Персональный белый список", "bool", "False", help="В существующем парсере значение читается из общего флага белого списка. Разные значения потребуют проверки."),
            setting("enable_global_path_symbols_degradating", "Общий список деградации", "bool", "False"),
            setting("enable_download_data_exchange", "Получать данные сервиса AlgoBond", "download", "", help="Выключение удаляет параметр: строка False в текущем парсере тоже включает загрузку."),
            setting("target_max_date", "Целевая дата и время", "datetime", ""),
            setting("max_date_for_close", "Дата завершения стратегии", "date", ""),
            setting("max_date_for_close_memory", "Память даты завершения", default="", min=0),
            setting("days_to_get_money", "Дней для получения средств", "integer", "0", min=0),
        ]),
    ]),
    dict(id="allocation", title="Сроки и этапы", subtitle="Визуальный конструктор распределения", icon="layers", groups=[
        dict(title="Порядок обработки", open=True, fields=[
            setting("stages_days_planning", "Интервалы этапов", "text", "", help="Например: 5-90|90-180|180-365. Разделитель этапов — |."),
            setting("bonds_config", "Базовое распределение", "textarea", "", help="Например: 5-90:25;90-180:25;180-365:50. Доля задается в процентах."),
            setting("stages_bonds_config", "Расширенное распределение", "allocation", "", help="Внутри этапа записи разделяет ;, этапы разделяет |. Шестое поле — дни до продажи, а не просадка."),
        ]),
    ]),
    dict(id="orders", title="Сделки и расписание", subtitle="Покупки, продажи и рабочие часы", icon="clock", groups=[
        dict(title="Условия размещения", open=True, fields=[
            setting("enable_bonds_limit_order", "Лимитная покупка облигаций", "bool", "False", help="При включении AlgoBond предпочитает лимитную заявку. При подтверждённой недоступности лимитного типа используется рыночная заявка."),
            setting("execution_bonds_time_limit_order", "Срок лимитной покупки облигаций", "duration", "0", min=0, max=2147483647, help="0 — без автоматической отмены AlgoBond. Биржа может завершить заявку раньше указанного срока. В INI сохраняются целые секунды."),
            setting("order_open_percent", "Часть объема для покупки", "range", "100", min=0, max=100, step=1, unit="%"),
            setting("order_close_percent", "Часть объема для продажи", "range", "100", min=0, max=100, step=1, unit="%"),
            setting("percent_lot_buy", "Коэффициент покупки", default="100", min=0, step=1, unit="%"),
            setting("percent_lot_sell", "Коэффициент продажи", default="100", min=0, step=1, unit="%"),
            setting("use_getting_current_price", "Обновлять цену перед покупкой", "bool", "False"),
            setting("use_checking_order_book", "Учитывать стакан", "bool", "False"),
            setting("enable_corrention_lots_by_order_book", "Корректировать объем по стакану", "bool", "False"),
            setting("percent_diff_prevprice", "Допустимое изменение цены", default="2", min=0, step=0.1, unit="%"),
            setting("percent_price_diff_stop", "Предел изменения цены", default="5", min=0, step=0.1, unit="%"),
        ]),
        dict(title="Ребалансировка и сокращение", fields=[
            setting("enable_rebalancing_by_config", "Ребалансировка", "bool", "False"),
            setting("enable_sell_degradating_position", "Продажа деградирующих позиций", "bool", "False"),
            setting("percent_degradating_position_for_sell", "Порог деградации", default="5", min=0, step=0.1, unit="%"),
            setting("enable_target_minimum_profit_diff_sell", "Продажа по цели прибыли", "bool", "False"),
            setting("percent_target_minimum_profit_diff_sell", "Минимальный рост цены", default="5", min=0, step=0.1, unit="%"),
            setting("percent_target_minimum_profit_price_sell", "Минимальная цена продажи", default="99", min=0, step=0.1),
            setting("close_all", "Закрывать все позиции в торговом режиме", "danger", "False", help="Эта настройка включает ветку закрытия в start_trading / режиме 1. Редактор ничего не исполняет."),
        ]),
        dict(title="Рабочие часы MOEX", open=True, fields=[
            *[setting(f"schedule_hour_{edge}_{action}", f"{label}: {'с' if edge == 'start' else 'до'}", "hour", str(default), min=0, max=23)
              for action, label in [("trading", "Покупки"), ("rebalancing", "Ребалансировка"), ("degradating", "Деградация")]
              for edge, default in [("start", 10), ("end", 17)]],
        ]),
    ]),
    dict(id="liquidity", title="Фонд ликвидности", subtitle="Свободные деньги и финансовое обеспечение", icon="wallet", groups=[
        dict(title="Фонд и сценарии", open=True, fields=[
            setting("liquidity_fund", "Тикер фонда", "fund", "", options=["FMMM", "TMON", "LQDT", "BCSD"]),
            setting("enable_liquidity_fund_weekend", "Размещение на выходные", "bool", "False"),
            setting("enable_liquidity_fund_everyday", "Ежедневное размещение", "bool", "False"),
            setting("enable_liquidity_fund_profitable", "Проверять прибыльность", "bool", "True"),
            setting("liquidity_fund_effective_rate", "Эффективная ставка", default="0", min=0, step=0.1, unit="%"),
            setting("liquidity_fund_target_profit", "Целевая прибыль", default="0", min=0, step=0.1),
            setting("count_for_open_liquidity_fund_per", "Параметр объема размещения", "integer", "0", min=0),
            setting("broker_comission_percent_perdeal_liquidity_fund", "Комиссия фонда", default="0", min=0, step=0.01, unit="%"),
        ]),
        dict(title="Обеспечение", fields=[
            setting("enable_liquidity_fund_leverage", "Обеспечение для фонда", "bool", "False"),
            setting("financial_support_for_liquidity_fund", "Сумма обеспечения фонда", default="0", min=0, step=1000, unit="₽"),
            setting("enable_leverage", "Использовать дополнительное обеспечение", "bool", "False", help="Применимость зависит от варианта запуска AlgoBond."),
            setting("financial_support_for_leverage", "Дополнительное обеспечение", default="0", min=0, step=1000, unit="₽"),
            setting("broker_leverage_percent_perday", "Стоимость обеспечения в день", default="0", min=0, step=0.01, unit="%"),
        ]),
        dict(title="Расписание фонда", fields=[
            setting("schedule_day_start_liquidity_weekend", "День размещения", "weekday", "5", min=1, max=7),
            setting("schedule_day_end_liquidity_weekend", "День освобождения", "weekday", "1", min=1, max=7),
            *[setting(f"schedule_hour_{edge}_liquidity_{period}", f"{label}: {'размещение' if edge == 'start' else 'освобождение'}", "hour", str(default), min=0, max=23)
              for period, label, a, b in [("weekend", "Выходные", 19, 10), ("everyday", "Каждый день", 19, 10), ("leverage", "С обеспечением", 18, 9)]
              for edge, default in [("start", a), ("end", b)]],
        ]),
    ]),
    dict(id="logs", title="Логи", subtitle="Журналы ошибок, диагностики и заявок", icon="files", groups=[
        dict(title="Запись логов", open=True, fields=[
            setting("enable_logs_errors", "Записывать ошибки и предупреждения", "bool", "True",
                    help="Файл logs/errors.log в папке AlgoBond."),
            setting("enable_logs_debugs", "Включить диагностику", "bool", "False",
                    help="Файл logs/debug.log и вывод диагностических сообщений в консоль."),
            setting("enable_logs_transactions", "Записывать отправку заявок", "bool", "False",
                    help="Рыночные и лимитные заявки в logs/transactions.log."),
        ]),
    ]),
    dict(id="database", title="Хранение и уведомления", subtitle="База данных и параметры сообщений", icon="database", groups=[
        dict(title="База данных", open=True, fields=[
            field("database", "type", "Тип базы", "select", "sqlite3", options=["sqlite3", "clickhouse", "postgresql"]),
            field("database", "address", "Адрес сервера", default="localhost"),
            field("database", "port", "Порт", "integer", "8123", min=0, max=65535),
            field("database", "name", "Имя базы", default="securities_analytics"),
            field("database", "login", "Пользователь"),
            field("database", "password", "Пароль", "secret"),
            field("database", "storage_depth_days", "Глубина хранения", "integer", "0", min=0, unit="дней", help="0 отключает очистку по сроку в поддерживающих ее провайдерах."),
        ]),
        dict(title="Уведомления", fields=[
            setting("use_send_email", "Отправка уведомлений", "bool", "False"),
            setting("mail_subject", "Тема сообщения", "text", "AlgoBond"),
            *[setting(f"count_email_perday_{key}", label, "integer", str(default), min=0)
              for key, label, default in [("debug", "Отладочных сообщений в день", 10), ("error", "Ошибок в день", 100), ("warning", "Предупреждений в день", 10), ("money", "Сообщений о средствах в день", 2)]],
        ]),
    ]),
]
FIELDS = {f["id"]: f for tab in GROUPS for group in tab["groups"] for f in group["fields"]}


class EditorError(Exception):
    def __init__(self, message, status=400, details=None):
        super().__init__(message)
        self.status = status
        self.details = details or []


def digest(data):
    return hashlib.sha256(data).hexdigest()


def is_secret(key):
    return any(word in key.casefold() for word in SECRET_WORDS)


def parse_ini(text):
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string(text)
        # Resolve interpolation now, exactly as the application does on access.
        for section in cfg.sections():
            dict(cfg.items(section))
    except configparser.Error as exc:
        line = getattr(exc, "lineno", None)
        raise EditorError("Некорректный INI" + (f", строка {line}" if line else "") +
                          ". Проверьте секции, повторы ключей и символы %.") from None
    return cfg


def values_of(cfg):
    return {section: dict(cfg.items(section, raw=True)) for section in cfg.sections()}


def patch_ini(text, patches):
    """Patch only touched keys; retain unrelated lines, comments, BOM and CRLF."""
    if not isinstance(patches, dict) or len(patches) > 500:
        raise EditorError("Слишком много изменений или неверный формат полей.")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    for identifier, value in patches.items():
        if not isinstance(identifier, str) or "." not in identifier:
            raise EditorError("Неверное имя параметра.")
        section, key = identifier.split(".", 1)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", section) or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise EditorError("Недопустимое имя секции или ключа.")
        if value is not None and (not isinstance(value, str) or any(c in value for c in "\r\n\x00")):
            raise EditorError(f"{identifier}: значение должно занимать одну строку.")
        start = end = None
        current = ""
        matches = []
        for i, line in enumerate(lines):
            header = re.match(r"\s*\[([^\]]+)\]", line)
            if header:
                if current == section and end is None:
                    end = i
                current = header.group(1)
                if current == section:
                    start = i
            if current == section:
                item = re.match(r"^(\s*)([^;#\s][^:=]*?)(\s*[:=]\s*)(.*?)(\r?\n)?$", line)
                if item and item.group(2).strip().casefold() == key.casefold():
                    matches.append((i, item))
        if len(matches) > 1:
            raise EditorError(f"Повтор параметра {identifier}. Исправьте его в INI.")
        if matches:
            i, match = matches[0]
            if value is None:
                lines.pop(i)
                # A multiline value belongs to the removed option.
                while i < len(lines) and lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) > len(match.group(1)) and not re.match(r"\s*[;#\[]", lines[i]):
                    lines.pop(i)
            else:
                # Inline comments are values for ConfigParser's default settings.
                lines[i] = match.group(1) + match.group(2) + match.group(3) + value + (match.group(5) or "")
                if i + 1 < len(lines) and lines[i + 1].strip() and len(lines[i + 1]) - len(lines[i + 1].lstrip()) > len(match.group(1)) and not re.match(r"\s*[;#\[]", lines[i + 1]):
                    raise EditorError(f"{identifier}: многострочное значение редактируется во вкладке INI.")
        elif value is not None:
            insert_at_end = start is None or end is None
            if insert_at_end and lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += newline
            if start is None:
                if lines and lines[-1].strip():
                    lines.append(newline)
                lines.extend([f"[{section}]{newline}", f"{key} = {value}{newline}"])
            else:
                lines.insert(end if end is not None else len(lines), f"{key} = {value}{newline}")
    return "".join(lines)


def validation(text):
    cfg = parse_ini(text)
    values = values_of(cfg)
    errors, warnings = [], []
    if not cfg.has_section("client"):
        errors.append("Отсутствует секция [client].")
    required = ["token", "client_id", "login", "client_name", "broker_name", "broker_company", "broker_server", "type_account"]
    for key in required:
        if not cfg.has_option("client", key):
            errors.append(f"Отсутствует обязательный ключ client.{key}.")
    for section, opts in values.items():
        for key, raw in opts.items():
            spec = FIELDS.get(f"{section}.{key}")
            if not spec:
                continue
            kind = spec["kind"]
            if kind in ("number", "integer", "range", "hour", "weekday", "duration"):
                if raw == "":
                    errors.append(f"{section}.{key}: нужно число или удаление параметра.")
                    continue
                try:
                    number = float(cfg.get(section, key))
                    if not math.isfinite(number) or (kind in ("integer", "hour", "weekday", "duration") and not number.is_integer()) or (kind=="duration" and not re.fullmatch(r"\+?\d+",raw)):
                        raise ValueError
                    if "min" in spec and number < spec["min"] or "max" in spec and number > spec["max"]:
                        raise ValueError
                except (ValueError, OverflowError):
                    errors.append(f"{section}.{key}: недопустимое число или значение вне диапазона.")
            elif kind in ("bool", "active", "danger"):
                allowed = ("0", "1") if kind == "active" else ("true", "false", "yes", "no", "t", "f", "y", "n", "0", "1")
                if raw.casefold() not in allowed:
                    errors.append(f"{section}.{key}: ожидается {'0 или 1' if kind == 'active' else 'True или False'}.")
            elif kind in ("date", "datetime") and raw:
                try:
                    datetime.strptime(raw, "%Y-%m-%d" if kind == "date" else "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    errors.append(f"{section}.{key}: неверный формат даты.")
    settings = values.get("settings", {})
    def number(key, default):
        try:
            return float(settings.get(key, default))
        except ValueError:
            return default
    if number("days_planning_min", 0) > number("days_planning_max", 30):
        errors.append("Нижняя граница срока погашения больше верхней.")
    for key in ("bonds_config", "stages_days_planning", "stages_bonds_config"):
        value = settings.get(key, "")
        if value:
            errors.extend(validate_intervals(key, value))
            stages = value.split("|") if key != "bonds_config" else [value]
            if key != "stages_days_planning":
                for index, stage in enumerate(stages, 1):
                    try:
                        total = sum(float(row.split(":")[1]) for row in stage.split(";") if row.strip() not in ("", "null"))
                        if total > 100.00001:
                            warnings.append(f"{key}, этап {index}: сумма долей {total:g}%. Исходный парсер допускает это значение; проверьте распределение перед применением.")
                    except (ValueError, IndexError):
                        pass  # Syntax errors are already reported above.
    if "enable_download_data_exchange" in settings and settings["enable_download_data_exchange"].casefold() in ("false", "0", "no"):
        warnings.append("Строка False у enable_download_data_exchange включает загрузку в текущем AlgoBond. Выключите переключатель в форме, чтобы удалить этот ключ.")
    if settings.get("enable_symbols_whitelist_for_login", "False").lower() != settings.get("enable_symbols_whitelist", "False").lower():
        warnings.append("Флаги общего и персонального белого списка различаются. Текущий парсер читает персональный флаг из общего ключа.")
    for key in ("order_open_percent", "order_close_percent"):
        if key in settings and number(key, 100) == 0:
            warnings.append(f"{key} = 0 не отключает сделку в текущем коде. Для отключения профиля используйте active = 0.")
    if "portfolio_percent_bonds" in settings:
        warnings.append("В файле есть portfolio_percent_bonds. Для задания предела текущий парсер использует portfolio_max_percent_bonds; старый ключ сохранен.")
    if "portfolio_max_percent_syntheticbonds" in settings:
        warnings.append("portfolio_max_percent_syntheticbonds читается текущим парсером как логическое значение, а не процент.")
    if settings.get("close_all", "False").casefold() in ("true", "1", "yes", "y", "t"):
        warnings.append("Включено close_all: торговый запуск может закрывать позиции счета.")
    if cfg.has_section("database"):
        for key in ("type", "name", "address", "port", "login", "password"):
            if not cfg.has_option("database", key):
                errors.append(f"Секция [database] включена, но отсутствует database.{key}.")
        if cfg.get("database", "type", fallback="") not in ("sqlite3", "clickhouse", "postgresql"):
            errors.append("Неизвестный тип базы данных.")
    broker = cfg.get("client", "broker_name", fallback="")
    if broker and broker.casefold() not in ("alor", "finam", "tinkoff", "bcs"):
        warnings.append("Название брокера не входит в список штатных адаптеров; значение будет сохранено без замены.")
    if cfg.get("client", "active", fallback="0") == "1":
        token_key = "token_grpc" if broker.casefold() == "finam" and cfg.get("client", "broker_type_api", fallback="rest") == "grpc" else "token"
        token_value = cfg.get("client", token_key, fallback="")
        if not token_value or any(part in token_value.casefold() for part in ("<", "replace_with", "your_token")):
            warnings.append(f"Профиль активен, но client.{token_key} пуст или содержит заполнитель.")
    return values, errors, warnings


def validate_intervals(key, value):
    errors = []
    for stage in value.split("|") if key != "bonds_config" else [value]:
        if not stage.strip():
            errors.append(f"{key}: пустой этап.")
            continue
        total = 0
        for row in stage.split(";") if key != "stages_days_planning" else [stage]:
            if row in ("", "null") and key != "stages_days_planning":
                continue
            parts = row.split(":")
            try:
                ends = parts[0].split("-")
                if len(ends) != 2:
                    raise ValueError
                low, high = map(float, ends)
                if not all(math.isfinite(n) for n in (low, high)) or low < 0 or high <= low:
                    raise ValueError
                if key == "stages_days_planning":
                    if len(parts) != 1:
                        raise ValueError
                else:
                    if not 2 <= len(parts) <= (2 if key == "bonds_config" else 7):
                        raise ValueError
                    numbers = list(map(float, parts[1:]))
                    if any(not math.isfinite(n) or n < 0 for n in numbers) or numbers[0] > 100:
                        raise ValueError
                    if len(numbers) > 1 and numbers[1] > 100:
                        raise ValueError
                    total += numbers[0]
            except ValueError:
                errors.append(f"{key}: неверная запись интервала (проверьте границы и числовые поля).")
    return errors


class ConfigStore:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.accounts = self.project / "data" / "configs" / "accounts"
        self.backups = self.project / "data" / "configs" / "editor_backups"
        self.staging = self.project / "data" / "configs" / "editor_staging"
        self.lock = threading.RLock()

    def folder(self, mode):
        if not isinstance(mode, str) or mode not in MODES:
            raise EditorError("Неизвестный режим.")
        folder = self.accounts / mode
        if folder.is_symlink() or folder.resolve() != self.accounts.resolve() / mode or not folder.resolve().is_relative_to(self.project):
            raise EditorError("Перенаправленная папка конфигураций не поддерживается.", 403)
        return folder

    def target(self, mode, name):
        if not isinstance(name, str) or len(name) > 160 or name != name.strip() or not name.lower().endswith(".ini"):
            raise EditorError("Введите имя файла с расширением .ini, без пробелов по краям.")
        if any(c in name for c in '<>:"/\\|?*\x00') or any(ord(c) < 32 for c in name) or name.startswith("."):
            raise EditorError("Недопустимые символы в имени файла.")
        if name.rsplit(".", 1)[0].split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
            raise EditorError("Зарезервированное имя файла Windows.")
        folder = self.folder(mode)
        target = folder / name
        if target.is_symlink() or target.resolve().parent != folder.resolve():
            raise EditorError("Ссылка на файл за пределами папки запрещена.", 403)
        if target.exists() and not target.is_file():
            raise EditorError("Выбранный путь не является обычным файлом.")
        return target

    def read(self, mode, name):
        target = self.target(mode, name)
        try:
            if target.stat().st_size > MAX_BYTES:
                raise EditorError("Файл больше 1 МиБ.")
            raw = target.read_bytes()
        except FileNotFoundError:
            raise EditorError("Файл уже удален или переименован. Обновите список.", 404) from None
        if len(raw) > MAX_BYTES:
            raise EditorError("Файл больше 1 МиБ.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise EditorError("Ожидается INI в UTF-8. Сначала преобразуйте кодировку файла.") from None
        return raw, text

    def document(self, mode, name):
        raw, text = self.read(mode, name)
        try:
            values, errors, warnings = validation(text)
        except EditorError as exc:
            values, errors, warnings = {}, [str(exc)], []
        return dict(name=name, mode=mode, text=text, values=values, version=digest(raw),
                    errors=errors, warnings=warnings, bytes=len(raw), bom=raw.startswith(BOM_UTF8))

    def files(self, mode):
        folder = self.folder(mode)
        if not folder.exists():
            return []
        result = []
        for file in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
            if not file.name.lower().endswith(".ini") or not file.is_file() or file.is_symlink():
                continue
            row = dict(name=file.name, label=file.stem, broker="", login="", active=False, error=False)
            try:
                _, text = self.read(mode, file.name)
                cfg = parse_ini(text)
                row.update(label=cfg.get("client", "client_name", fallback=file.stem),
                           broker=cfg.get("client", "broker_name", fallback=""),
                           login=cfg.get("client", "login", fallback=""),
                           active=cfg.get("client", "active", fallback="0") == "1")
            except (EditorError, OSError):
                row["error"] = True
            result.append(row)
        return result

    def template(self):
        file = self.project / "config_demo.ini"
        if not file.is_file() or file.stat().st_size > MAX_BYTES:
            raise EditorError("Не найден config_demo.ini в корне выбранного проекта.", 404)
        try:
            text = file.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise EditorError("config_demo.ini должен быть в UTF-8.") from None
        # Templates may contain real credentials: a new profile starts blank.
        cfg = parse_ini(text)
        patches = {}
        for section in cfg.sections():
            for key in cfg[section]:
                if is_secret(key):
                    patches[f"{section}.{key}"] = ""
        for key in ("client_id", "login", "client_name", "account_id", "account_id_grpc", "strategy_id", "robot_id", "portofolio_name"):
            if cfg.has_option("client", key):
                patches[f"client.{key}"] = ""
        patches["client.active"] = "0"
        # The demo contains placeholders unsuitable for numeric parsing.
        if cfg.has_section("database"):
            for key in cfg["database"]:
                patches[f"database.{key}"] = None
            text = remove_section(text, "database")
        for key in ("enable_download_data_exchange", "portfolio_percent_bonds", "max_date_for_close"):
            patches[f"settings.{key}"] = None
        text = patch_ini(text, patches)
        values, errors, warnings = validation(text)
        return dict(name="", mode="monitoring", text=text, values=values, version=None,
                    errors=errors, warnings=warnings, bytes=len(text.encode("utf-8")))

    def prepare(self, payload):
        mode, name = payload.get("mode"), payload.get("name")
        target = self.target(mode, name)
        create = payload.get("create") is True
        if create:
            if target.exists():
                raise EditorError("Файл с таким именем уже существует. Выберите другое имя.", 409)
            original, text = b"", payload.get("text", "")
            if payload.get("version") is not None:
                raise EditorError("У нового файла не должно быть версии.")
        else:
            original, text = self.read(mode, name)
            if payload.get("version") != digest(original):
                raise EditorError("Файл изменен другой программой. Обновите его перед сохранением; ваш черновик остается в форме.", 409)
            if "text" in payload:
                text = payload["text"]
        has_bom = payload.get("bom") is True if create else original.startswith(BOM_UTF8)
        encoding = "utf-8-sig" if has_bom else "utf-8"
        if not isinstance(text, str) or len(text.encode(encoding)) > MAX_BYTES or "\x00" in text:
            raise EditorError("Недопустимое содержимое или слишком большой файл.")
        # Normal form editing requires a valid starting document.
        parse_ini(text)
        if payload.get("remove_database"):
            text = remove_section(text, "database")
        text = patch_ini(text, payload.get("patches", {}))
        candidate = text.encode(encoding)
        if len(candidate) > MAX_BYTES:
            raise EditorError("Файл больше 1 МиБ.")
        values, errors, warnings = validation(text)
        try:
            before = values_of(parse_ini(original.decode("utf-8-sig"))) if original else {}
        except EditorError:
            # A corrected raw document must be able to replace malformed INI.
            before = {}
            warnings.append("Исходный INI был некорректен. Полная предыдущая версия останется в резервной копии.")
        changes = []
        for section in sorted(set(before) | set(values)):
            for key in sorted(set(before.get(section, {})) | set(values.get(section, {}))):
                old, new = before.get(section, {}).get(key), values.get(section, {}).get(key)
                if old != new:
                    mask = lambda v: ("••••••••" if v else "(пусто)") if is_secret(key) and v is not None else v
                    changes.append(dict(field=f"{section}.{key}", before=mask(old), after=mask(new)))
        return target, original, candidate, dict(values=values, errors=errors,
            warnings=warnings, changes=changes, text=text, create=create)

    def preview(self, payload):
        with self.lock:
            _, _, _, result = self.prepare(payload)
            return result

    def save(self, payload):
        with self.lock:
            target, original, candidate, result = self.prepare(payload)
            if result["errors"]:
                raise EditorError("Исправьте ошибки перед сохранением.", 422, result["errors"])
            create = result["create"]
            if not create and original == candidate:
                return dict(document=self.document(payload["mode"], payload["name"]), backup=None, unchanged=True)
            self.staging.mkdir(parents=True, exist_ok=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = None
            if not create:
                self.backups.mkdir(parents=True, exist_ok=True)
                dest = self.backups / payload["mode"]
                dest.mkdir(exist_ok=True)
                backup = dest / (target.name + "." + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "." + secrets.token_hex(3) + ".bak")
                with backup.open("xb") as stream:
                    stream.write(original)
                os.chmod(backup, stat.S_IRUSR | stat.S_IWUSR)
            descriptor, temp_name = tempfile.mkstemp(prefix="editor_", suffix=".tmp", dir=self.staging)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(candidate)
                    stream.flush()
                    os.fsync(stream.fileno())
                if not create:
                    os.chmod(temp_name, stat.S_IMODE(target.stat().st_mode))
                # Detect a concurrent edit immediately before committing.
                if not create and target.read_bytes() != original or create and target.exists():
                    raise EditorError("Файл изменен во время сохранения. Обновите его и повторите.", 409)
                if not create:
                    os.replace(temp_name, target)
                else:
                    # Link gives atomic create-without-overwrite semantics.
                    os.link(temp_name, target)
            finally:
                Path(temp_name).unlink(missing_ok=True)
            return dict(document=self.document(payload["mode"], payload["name"]),
                        backup=str(backup.relative_to(self.project)) if backup else None, unchanged=False)


def remove_section(text, section):
    output, dropping = [], False
    for line in text.splitlines(keepends=True):
        header = re.match(r"\s*\[([^\]]+)\]", line)
        if header:
            dropping = header.group(1) == section
        if not dropping:
            output.append(line)
    return "".join(output)


class EditorServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store):
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(18)
        super().__init__(address, EditorHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class EditorHandler(BaseHTTPRequestHandler):
    server_version = "AlgoBondEditor/1.0"
    def log_message(self, *args):
        pass  # Never log paths, tokens, request bodies or configuration values.

    def respond(self, data, status=200, kind="application/json; charset=utf-8", *, support_chat=True):
        is_page = kind.split(";", 1)[0].strip().lower() == "text/html"
        support_chat = is_page and support_chat
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, str):
            if support_chat:
                data = with_support_chat(data, self.server.nonce)
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", content_security_policy(self.server.nonce, support_chat=support_chat))
        self.end_headers()
        self.wfile.write(data)

    def trusted(self, api=False):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            raise EditorError("Неподходящий адрес запроса.", 403)
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            raise EditorError("Запрос с другого сайта запрещен.", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise EditorError("Межсайтовый запрос запрещен.", 403)
        if api and not secrets.compare_digest(self.headers.get("X-Editor-Token", ""), self.server.token):
            raise EditorError("Откройте редактор по ссылке из окна запуска.", 403)

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            self.trusted(api=parsed.path.startswith("/api/"))
            params = parse_qs(parsed.query)
            pick = lambda key: params.get(key, [""])[0]
            if parsed.path == "/":
                self.respond(HTML.replace("__NONCE__", self.server.nonce), kind="text/html; charset=utf-8")
            elif parsed.path == "/favicon.ico":
                file = Path(__file__).resolve().with_name("favicon.ico")
                if not file.is_file():
                    raise EditorError("Значок не найден.", 404)
                data = file.read_bytes()
                kind = "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/vnd.microsoft.icon"
                self.respond(data, kind=kind)
            elif parsed.path == "/logo.png":
                file = self.server.store.project / "algobond-logo-blue.png"
                if not file.is_file() or file.stat().st_size > 8 * MAX_BYTES:
                    raise EditorError("Логотип не найден.", 404)
                self.respond(file.read_bytes(), kind="image/png")
            elif parsed.path == "/api/boot":
                self.respond(dict(groups=GROUPS, project=str(self.server.store.project), max_bytes=MAX_BYTES,
                    folders={m: str(self.server.store.folder(m)) for m in MODES}))
            elif parsed.path == "/api/files":
                self.respond(dict(files=self.server.store.files(pick("mode"))))
            elif parsed.path == "/api/file":
                self.respond(self.server.store.document(pick("mode"), pick("name")))
            elif parsed.path == "/api/template":
                self.respond(self.server.store.template())
            else:
                raise EditorError("Не найдено.", 404)
        except EditorError as exc:
            self.respond(dict(error=str(exc), details=exc.details), exc.status)
        except OSError:
            self.respond(dict(error="Нет доступа к файлу. Проверьте права и занятость файла другой программой."), 500)

    def do_POST(self):
        try:
            self.trusted(api=True)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise EditorError("Ожидается JSON.", 415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise EditorError("Некорректная длина запроса.") from None
            if not 0 < size <= MAX_REQUEST_BYTES:
                raise EditorError("Размер запроса превышен.", 413)
            self.connection.settimeout(10)
            try:
                payload = json.loads(self.rfile.read(size).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise EditorError("Некорректный JSON.") from None
            if not isinstance(payload, dict):
                raise EditorError("Ожидается объект запроса.")
            if self.path == "/api/preview":
                self.respond(self.server.store.preview(payload))
            elif self.path == "/api/save":
                self.respond(self.server.store.save(payload))
            else:
                raise EditorError("Не найдено.", 404)
        except EditorError as exc:
            self.respond(dict(error=str(exc), details=exc.details), exc.status)
        except OSError:
            self.respond(dict(error="Не удалось сохранить файл. Проверьте права и доступность диска."), 500)


HTML = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="/favicon.ico">
<title>AlgoBond — конфигурации</title><style>
:root{--navy:#18324b;--ink:#192c40;--muted:#778597;--line:#e4eaf0;--green:#16ac72;--soft:#f4f7fa;--red:#c04747;--shadow:0 8px 30px #20394d09;font-family:Segoe UI,Arial,sans-serif;color:var(--ink);font-size:14px;color-scheme:light}*{box-sizing:border-box}body{margin:0;background:var(--soft)}button,input,select,textarea{font:inherit}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.45}button,input,select,textarea{outline-offset:3px}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid #77bce4}button{border:0;background:none;color:inherit}svg{width:19px;height:19px;stroke:currentColor;fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}.app{display:grid;grid-template-columns:282px minmax(0,1fr);height:100dvh;min-height:600px}.sidebar{background:#fff;border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}.brand{padding:19px 22px 15px;border-bottom:1px solid var(--line)}.brand-logo{height:80px;background:#18324b url('/logo.png') center/228px auto no-repeat;border-radius:13px}.brand-caption{margin:13px 1px 2px;color:var(--muted);font-size:11px;letter-spacing:1.65px;text-transform:uppercase}.modes{display:grid;grid-template-columns:1fr 1fr;background:#eef3f6;border-radius:10px;margin:19px 16px 14px;padding:4px;gap:3px}.mode{font-size:12px;display:flex;justify-content:center;align-items:center;gap:6px;padding:10px 3px;border-radius:7px;color:#6b7e90;font-weight:600}.mode svg{width:16px;height:16px}.mode.selected{background:#fff;color:var(--navy);box-shadow:0 2px 7px #12263e12}.list-tools{padding:0 17px}.search-wrap{position:relative}.search-wrap>svg{position:absolute;left:11px;top:11px;width:16px;color:#8d9aab}.search-wrap input{width:100%;border:1px solid var(--line);border-radius:8px;padding:10px 11px 10px 35px;background:#fbfcfd;font-size:12px;color:var(--ink)}.list-filters{display:flex;gap:6px;margin-top:9px}.list-filters select{min-width:0;flex:1;border:1px solid var(--line);padding:7px 4px;border-radius:7px;color:#6b7d8f;font-size:11px;background:white}.list-label{display:flex;justify-content:space-between;align-items:center;margin:21px 18px 10px;font-size:10px;color:#94a0ad;letter-spacing:1.4px;font-weight:700}.list-label button{font-size:18px;padding:0 5px;color:#617589}.files{overflow-y:auto;flex:1;padding:0 10px 12px}.file{display:flex;align-items:flex-start;gap:10px;width:100%;text-align:left;margin:3px 0;padding:13px 10px;border:1px solid transparent;border-radius:9px}.file:hover{background:#f7f9fb}.file.selected{background:#edf7f3;border-color:#d3eee2}.file-icon{background:#eef3f7;border-radius:8px;padding:8px;display:flex;color:#7790a2}.file.selected .file-icon{background:#d9eee4;color:#229b6f}.file-body{min-width:0;flex:1}.file-name{font-size:12px;font-weight:600;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}.file-meta{font-size:10px;color:#8594a4;margin-top:5px;display:flex;gap:5px;align-items:center}.dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#b8c2cc}.dot.on{background:var(--green)}.dot.bad{background:var(--red)}.file-sub{font-size:10px;color:#8b9aaa;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.no-files{padding:32px 10px;color:#8c99a6;text-align:center;font-size:12px;line-height:1.7}.sidebar-bottom{padding:15px 17px;border-top:1px solid var(--line)}.folder-title{font-size:10px;font-weight:600;color:#748696;display:flex;gap:7px;align-items:center}.folder-title svg{width:15px}.folder-path{font-size:10px;line-height:1.55;color:#9aa7b4;margin:7px 0 0;overflow-wrap:anywhere}.workspace{min-width:0;display:flex;flex-direction:column;min-height:0}.topbar{height:83px;padding:0 32px;background:#fff;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:14px;flex-shrink:0}.eyebrow{font-size:10px;letter-spacing:1.65px;text-transform:uppercase;font-weight:600;color:#8c9bab;margin:0 0 6px}.topbar h1{font-size:22px;letter-spacing:-.6px;margin:0;font-weight:650}.topbar-right{display:flex;align-items:center;gap:12px}.local-status{font-size:11px;color:#7f8e9b;display:flex;align-items:center;gap:7px;margin-right:9px}.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;border-radius:8px;padding:10px 14px;font-size:12px;font-weight:600;white-space:nowrap;min-height:38px}.btn svg{width:16px;height:16px}.btn.primary{background:var(--green);color:white;box-shadow:0 3px 8px #16ac7217}.btn.primary:hover{background:#109763}.btn.secondary{background:#fff;border:1px solid #dae3eb;color:#5c7083}.btn.secondary:hover{background:#f4f7fa}.btn.ghost{color:#73869a}.btn.danger{color:var(--red);border:1px solid #edd5d5;background:white}.scroll{flex:1;overflow:auto;padding:25px 32px 22px}.breadcrumb{font-size:11px;color:#92a0af;display:flex;align-items:center;gap:8px;margin-bottom:18px}.breadcrumb strong{font-weight:500;color:#60788d}.crumb-sep{color:#bdc7d0}.editor-heading{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px;gap:15px}.editor-heading h2{margin:0 0 7px;font-size:25px;line-height:1.25;letter-spacing:-.6px;font-weight:650}.editor-description{color:#8a98a6;font-size:12px;line-height:1.5}.heading-actions{display:flex;gap:5px;align-items:center}.badge{font-size:10px;font-weight:600;background:#eaf6f0;color:#2d946d;padding:6px 9px;border-radius:20px;white-space:nowrap}.badge.off{background:#edf1f5;color:#8493a2}.badge.unsaved{background:#fff2d7;color:#a27c28}.profile-summary{display:flex;gap:20px;align-items:center;justify-content:space-between;background:var(--navy);color:#fff;border-radius:13px;padding:22px 25px;margin-bottom:22px;box-shadow:0 5px 18px #17324b0c}.summary-left{display:flex;align-items:center;gap:14px;min-width:0}.summary-mark{width:45px;height:45px;border-radius:11px;background:#ffffff0e;display:grid;place-items:center;color:#8bd6b9}.summary-mark svg{width:23px;height:23px}.summary-name{font-size:15px;font-weight:600;margin-bottom:6px;white-space:nowrap;text-overflow:ellipsis;overflow:hidden;max-width:330px}.summary-note{font-size:10px;color:#9bb2c6}.summary-metrics{display:flex;gap:32px;align-items:center}.metric-label{color:#94afc6;font-size:10px;margin-bottom:8px}.metric-value{font-size:24px;line-height:1;font-weight:600;letter-spacing:-.7px}.metric-value span{font-size:13px;color:#a9bed1;margin-left:4px;font-weight:400}.metric-rule{width:1px;height:38px;background:#ffffff10}.tabs{display:flex;gap:3px;overflow:auto;border-bottom:1px solid #dce5ed;margin-bottom:22px;scrollbar-width:thin}.tab{padding:12px 12px 14px;border-bottom:2px solid transparent;display:flex;align-items:center;gap:6px;color:#8a99a8;font-size:11px;white-space:nowrap;font-weight:600}.tab svg{width:15px;height:15px}.tab.selected{color:#1b8e68;border-bottom-color:var(--green)}.field-search{margin-bottom:17px;max-width:340px}.field-search input{background:transparent}.panel-title{font-size:17px;font-weight:600;letter-spacing:-.2px;margin-bottom:7px}.panel-subtitle{color:#8e9ba9;font-size:12px;line-height:1.6;margin-bottom:19px}.section{background:white;border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);margin-bottom:16px;overflow:hidden}.section>summary{display:flex;align-items:center;justify-content:space-between;padding:19px 21px;cursor:pointer;list-style:none;font-size:13px;font-weight:600}.section>summary::-webkit-details-marker{display:none}.section>summary .chevron{color:#92a0af;transition:transform .2s}.section[open]>summary .chevron{transform:rotate(180deg)}.section>summary .section-icon{display:inline-flex;align-items:center;gap:10px}.section-number{display:inline-grid;place-items:center;width:23px;height:23px;background:#f2f6f8;border-radius:6px;font-size:10px;color:#8a9eae;font-weight:500}.section-content{border-top:1px solid #edf1f5;padding:19px 21px 23px}.field-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px 23px}.field{min-width:0}.field.full{grid-column:1/-1}.label-row{display:flex;align-items:center;gap:5px;justify-content:space-between;margin-bottom:9px;min-height:16px}.label{font-size:12px;font-weight:500;color:#51687b}.field-key{font-size:9px;color:#a0adba;overflow-wrap:anywhere;margin-top:5px;font-family:Consolas,monospace}.default-badge{font-size:9px;color:#a8b4be;margin-left:6px;font-weight:400}.reset-field{color:#a8b7c4;font-size:15px;padding:0 3px;line-height:1}.reset-field:hover{color:#298467}.input-wrap{position:relative;display:flex;align-items:center;gap:5px}.control{width:100%;height:39px;background:#fff;border:1px solid #dce5ed;border-radius:7px;padding:8px 11px;color:#324c63;font-size:12px;min-width:0}.control:hover{border-color:#b9cbd8}.control:focus{border-color:#43b28b;outline:2px solid #16ac7212;outline-offset:0}.control::placeholder{color:#b5c0ca}textarea.control{min-height:77px;height:auto;resize:vertical;line-height:1.65;font-family:Consolas,monospace}.control.with-unit{padding-right:50px}.unit{position:absolute;right:11px;color:#a0aebb;font-size:10px;pointer-events:none}.help{font-size:10px;line-height:1.55;color:#9aa7b4;margin-top:7px}.help.important{color:#b08c42}.switch-row{display:flex;justify-content:space-between;align-items:center;min-height:39px;gap:13px;padding:5px 0}.switch-text{font-size:12px;color:#526c80}.switch{position:relative;width:35px;height:21px;flex-shrink:0;display:inline-block}.switch input{position:absolute;opacity:0;inset:0;cursor:pointer;z-index:1;width:100%;height:100%;margin:0}.switch-track{position:absolute;inset:0;background:#dce4eb;border-radius:20px;transition:background .2s}.switch-track:before{content:'';position:absolute;left:3px;top:3px;width:15px;height:15px;background:white;border-radius:50%;box-shadow:0 1px 3px #283d5020;transition:transform .2s}.switch input:checked+.switch-track{background:var(--green)}.switch input:checked+.switch-track:before{transform:translateX(14px)}.switch input:focus-visible+.switch-track{outline:3px solid #80c0e0;outline-offset:3px}.switch.danger input:checked+.switch-track{background:#d26b61}.range-row{display:flex;align-items:center;gap:14px}.range-row input[type=range]{flex:1;min-width:40px;accent-color:var(--green);height:5px;cursor:pointer}.range-value{width:82px;flex-shrink:0;position:relative}.range-value .control{padding-right:25px;text-align:right;height:35px}.range-value .unit{right:8px;font-size:10px}.range-scale{display:flex;justify-content:space-between;margin:7px 97px 0 0;font-size:9px;color:#acb7c2}.chips{display:flex;gap:7px;flex-wrap:wrap}.chip{border:1px solid #dfe7ed;border-radius:7px;padding:8px 13px;font-size:11px;color:#8b9bab;background:#fff}.chip.selected{border-color:#99d9be;background:#eef9f3;color:#208561}.chip-add{width:100%;margin-top:8px}.secret-btn{position:absolute;right:5px;top:4px;width:31px;height:31px;color:#91a3b4;display:grid;place-items:center}.secret-btn svg{width:16px}.secret-control{padding-right:40px}.footer{min-height:76px;flex-shrink:0;padding:14px 32px;background:#fff;border-top:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:16px}.footer-note{font-size:11px;color:#94a2af;display:flex;align-items:center;gap:8px}.footer-note svg{width:15px;height:15px;color:#6faa92}.footer-actions{display:flex;align-items:center;gap:8px}.empty{height:100%;min-height:360px;display:flex;align-items:center;justify-content:center;text-align:center;padding:35px}.empty-inner{max-width:480px}.empty-art{width:84px;height:84px;border-radius:24px;background:#e9f1f3;margin:0 auto 23px;display:grid;place-items:center;color:#66a28e;transform:rotate(-5deg)}.empty-art svg{width:37px;height:37px;transform:rotate(5deg)}.empty h2{font-size:25px;font-weight:600;letter-spacing:-.6px;margin-bottom:12px}.empty p{font-size:13px;line-height:1.8;color:#8b9ba9;margin-bottom:24px}.empty small{display:block;margin-top:17px;color:#9eaab6;font-size:10px}.alert{border:1px solid #eedfc2;background:#fffaf0;color:#9b7c3d;padding:12px 15px;border-radius:8px;margin-bottom:14px;font-size:11px;line-height:1.65}.alert.error{background:#fff5f5;border-color:#eed0d0;color:#ae5454}.alert ul{margin:0;padding-left:16px}.database-switch{display:flex;align-items:center;justify-content:space-between;border:1px solid var(--line);border-radius:10px;padding:15px 18px;background:white;margin-bottom:16px}.database-switch strong{font-weight:500;font-size:13px}.database-switch p{margin:5px 0 0;color:#95a3b0;font-size:11px}.raw-area{width:100%;min-height:400px;resize:vertical;background:#172e43;color:#d5e4ec;border:0;border-radius:10px;padding:20px;font:12px/1.75 Consolas,monospace;tab-size:4;white-space:pre;overflow:auto}.raw-warning{font-size:11px;color:#889aaa;line-height:1.7;margin-bottom:13px}.extra-row{display:grid;grid-template-columns:minmax(130px,1fr) 2fr;gap:18px;padding:14px 0;border-bottom:1px solid #eff3f6}.extra-row:last-child{border:0}.extra-row code{font-size:11px;color:#7990a3;word-break:break-word}.allocation-box{border:1px solid #dfE8ef;border-radius:9px;overflow:hidden;margin:10px 0}.allocation-header{padding:11px 13px;display:flex;justify-content:space-between;align-items:center;background:#f7fafb}.allocation-header strong{font-size:11px;font-weight:600;color:#5d778c}.allocation-table-wrap{overflow:auto;padding:10px 12px}.allocation-table{border-collapse:collapse;min-width:670px;width:100%;font-size:10px}.allocation-table th{font-size:9px;color:#96a5b2;font-weight:500;text-align:left;padding:5px 3px}.allocation-table td{padding:4px 3px}.allocation-table input{width:68px;height:31px;padding:5px 6px;font-size:10px}.allocation-table .remove{color:#bd8a8a}.allocation-bars{display:flex;gap:2px;height:6px;background:#eff3f5;overflow:hidden;border-radius:3px;margin:10px 12px 4px}.allocation-bars span{background:var(--green)}.allocation-total{font-size:10px;color:#99a8b5;padding:0 12px 12px}.allocation-toolbar{display:flex;justify-content:space-between;gap:5px;align-items:center}.allocation-toolbar .btn{font-size:10px;padding:7px 9px;min-height:30px}.allocation-table .btn{min-height:25px;padding:4px}.raw-allocation{margin-top:12px}.allocation-error{font-size:11px;color:#b08a50;padding:13px;line-height:1.6}dialog{border:1px solid #dfe7ed;border-radius:15px;padding:0;box-shadow:0 25px 100px #102d4730;max-width:690px;width:calc(100% - 32px);color:var(--ink);max-height:85dvh}dialog::backdrop{background:#0d213c60;backdrop-filter:blur(3px)}.dialog-header{display:flex;justify-content:space-between;gap:16px;padding:22px 24px 18px;border-bottom:1px solid var(--line)}.dialog-header h3{font-size:19px;letter-spacing:-.4px;margin:0 0 7px;font-weight:600}.dialog-header p{font-size:11px;color:#8c9ead;line-height:1.5;margin:0}.close-dialog{font-size:24px;color:#9fadb9;align-self:flex-start;line-height:1}.dialog-body{padding:22px 24px;overflow:auto;max-height:55dvh}.dialog-footer{padding:16px 24px;border-top:1px solid var(--line);display:flex;justify-content:flex-end;gap:8px}.diff{width:100%;border-collapse:collapse;font-size:11px}.diff th{font-weight:500;text-align:left;color:#90a1af;background:#f6f9fb;padding:10px;border-bottom:1px solid #e1e8ed}.diff td{padding:10px;border-bottom:1px solid #edf1f4;overflow-wrap:anywhere;max-width:250px;vertical-align:top}.diff td:first-child{font-family:Consolas,monospace;color:#6d8498;font-size:10px}.diff-before{color:#a3aeb9}.diff-after{color:#269471}.toast{position:fixed;bottom:94px;right:30px;z-index:15;max-width:450px;background:#17394d;color:white;padding:15px 19px;border-radius:10px;box-shadow:0 7px 24px #122c4930;font-size:12px;line-height:1.6;display:none}.toast.show{display:block}.toast.error{background:#94514e}.hidden{display:none!important}.loading{opacity:.55;pointer-events:none}.small-note{font-size:10px;line-height:1.6;color:#91a1ae}.empty-list-link{color:#24956f;text-decoration:underline;font-size:11px}.mobile-toggle{display:none}.section:has(.field[data-filtered]:not(.hidden)){} .full-heading{grid-column:1/-1;font-size:12px;color:#94a2af}.summary-active{text-transform:uppercase;letter-spacing:1px;font-size:9px;color:#8eb8ac;margin-left:10px}.modal-field{margin-bottom:18px}.modal-field label{display:block;margin-bottom:8px;font-size:12px;color:#70869a}.no-matches{padding:24px;color:#8d9ba9;font-size:12px;text-align:center}
.search-wrap > [data-icon]{position:absolute;left:11px;top:10px;width:16px;height:17px;color:#8d9aab;pointer-events:none}.search-wrap > [data-icon] svg{width:16px;height:16px}@media(min-width:1550px){.scroll{padding-right:50px;padding-left:42px}.topbar,.footer{padding-left:42px;padding-right:50px}.field-grid{gap:24px 35px}.section-content{padding:22px 25px}.section>summary{padding:20px 25px}}
@media(max-width:1150px){.app{grid-template-columns:245px minmax(0,1fr)}.topbar{padding:0 22px}.scroll{padding:22px}.footer{padding:13px 22px}.local-status{display:none}.brand{padding:16px}.summary-metrics{gap:20px}.summary-name{max-width:200px}.tab{font-size:10px;padding-left:9px;padding-right:9px}.field-grid{column-gap:17px}.footer-note{max-width:220px;line-height:1.5}}
@media(max-width:850px){.app{grid-template-columns:205px minmax(0,1fr)}.brand-logo{background-size:200px}.brand-caption{font-size:9px}.modes{margin:16px 10px}.mode{font-size:10px;gap:3px}.mode svg{width:13px}.list-tools{padding:0 11px}.list-filters{flex-direction:column}.topbar h1{font-size:19px}.topbar .btn{font-size:0;padding:10px}.topbar .btn svg{width:19px}.field-grid{grid-template-columns:1fr}.field.full{grid-column:auto}.profile-summary{padding:18px;align-items:flex-start}.summary-metrics{gap:15px;flex-direction:column;align-items:flex-end}.metric-rule{display:none}.metric-value{font-size:19px}.metric-label{font-size:9px;margin-bottom:4px}.summary-name{max-width:170px;font-size:13px}.summary-mark{display:none}.footer{flex-direction:column;align-items:stretch;gap:10px}.footer-actions{justify-content:flex-end}.footer-note{max-width:none;font-size:10px}.editor-heading h2{font-size:21px}.heading-actions .btn{font-size:0;padding:8px}.heading-actions .btn svg{width:17px}.editor-heading .badge{font-size:9px}.extra-row{grid-template-columns:1fr;gap:6px}}
@media(max-width:620px){.app{grid-template-columns:1fr;height:100dvh}.sidebar{position:fixed;inset:0 auto 0 0;width:260px;z-index:10;transform:translateX(-100%);box-shadow:8px 0 50px #12304520;transition:transform .2s}.sidebar.open{transform:none}.mobile-toggle{display:inline-flex;font-size:20px;color:#758a9b;padding-right:12px}.topbar{height:72px;padding:0 15px}.topbar-heading{display:flex;align-items:center}.eyebrow{font-size:8px}.topbar h1{font-size:18px}.topbar-right{gap:3px}.scroll{padding:18px 15px}.footer{padding:12px 15px}.profile-summary{gap:9px}.summary-metrics{gap:10px}.summary-name{max-width:180px}.breadcrumb{font-size:10px}.section>summary{padding:16px}.section-content{padding:17px 16px}.toast{right:15px;left:15px;bottom:124px;max-width:none}.diff{font-size:10px}.dialog-header,.dialog-body,.dialog-footer{padding:17px}.file-name{font-size:13px}}
</style></head><body>
<div class="app"><aside class="sidebar" id="sidebar"><div class="brand"><div class="brand-logo" role="img" aria-label="AlgoBond"></div><p class="brand-caption">Редактор конфигураций</p></div>
<div class="modes" role="tablist" aria-label="Режим"><button class="mode selected" data-mode="monitoring" role="tab" aria-selected="true"> <span data-icon="activity"></span>Мониторинг</button><button class="mode" data-mode="trading" role="tab" aria-selected="false"><span data-icon="chart"></span>Торговля</button></div>
<div class="list-tools"><div class="search-wrap"><span data-icon="search"></span><input id="fileSearch" placeholder="Найти файл или портфель…" aria-label="Поиск конфигураций"></div><div class="list-filters"><select id="brokerFilter" aria-label="Фильтр брокера"><option value="">Все брокеры</option></select><select id="activeFilter" aria-label="Фильтр активности"><option value="">Все профили</option><option value="on">Активные</option><option value="off">Неактивные</option></select></div></div>
<div class="list-label"><span id="listCount">КОНФИГУРАЦИИ</span><button id="sidebarNew" title="Создать профиль" aria-label="Создать профиль">+</button></div><div id="files" class="files"></div>
<div class="sidebar-bottom"><div class="folder-title"><span data-icon="folder"></span><span id="folderTitle">Папка мониторинга</span></div><div class="folder-path" id="folderPath"></div></div></aside>
<main class="workspace"><header class="topbar"><div class="topbar-heading"><button class="mobile-toggle" id="mobileToggle" aria-label="Открыть список файлов">☰</button><div><p class="eyebrow">Рабочее пространство</p><h1>Конфигурации</h1></div></div><div class="topbar-right"><span class="local-status"><i class="dot on"></i>Локальный редактор</span><button class="btn ghost" id="refresh"><span data-icon="refresh"></span>Обновить</button><button class="btn secondary" id="importIni" title="Импорт INI" disabled><span data-icon="upload"></span>Импорт INI</button><input type="file" id="importFile" accept=".ini" hidden><button class="btn primary" id="newProfile"><span data-icon="plus"></span>Новый профиль</button></div></header>
<div class="scroll" id="scroll"><div id="empty" class="empty"><div class="empty-inner"><div class="empty-art"><span data-icon="files"></span></div><p class="eyebrow">AL G O B O N D / CONFIGURATION</p><h2 id="emptyTitle">Все настройки в одном месте</h2><p id="emptyText">Выберите профиль слева или создайте новый на основе config_demo.ini. Редактор сохранит ваши комментарии и дополнительные параметры.</p><button class="btn primary" id="emptyNew"><span data-icon="plus"></span>Создать из демофайла</button><small>Изменения записываются только по кнопке «Сохранить»</small></div></div>
<div id="editor" class="hidden"><div class="breadcrumb"><span id="crumbMode"></span><span class="crumb-sep">/</span><strong id="crumbFile"></strong></div><div class="editor-heading"><div><h2 id="portfolioTitle"></h2><div class="editor-description" id="portfolioDescription"></div></div><div class="heading-actions"><span class="badge off" id="activeBadge">Неактивен</span><span class="badge unsaved hidden" id="dirtyBadge">Изменен</span><button class="btn ghost" id="duplicate"><span data-icon="copy"></span>Дублировать</button><button class="btn secondary" id="exportIni" title="Экспорт текущего INI"><span data-icon="download"></span>Экспорт INI</button></div></div>
<div class="profile-summary"><div class="summary-left"><div class="summary-mark"><span data-icon="wallet"></span></div><div><div class="summary-name" id="summaryBroker"></div><div class="summary-note" id="summaryLogin"></div></div></div><div class="summary-metrics"><div><div class="metric-label">Доля облигаций</div><div class="metric-value" id="summaryBonds"></div></div><div class="metric-rule"></div><div><div class="metric-label">Доля позиции</div><div class="metric-value" id="summaryRisk"></div></div><div class="metric-rule"></div><div><div class="metric-label">До погашения</div><div class="metric-value" id="summaryDays"></div></div></div></div>
<div class="tabs" id="tabs" role="tablist" aria-label="Разделы конфигурации"></div><div id="issues"></div><div class="search-wrap field-search" id="fieldSearchWrap"><span data-icon="search"></span><input id="fieldSearch" placeholder="Найти параметр в разделе…" aria-label="Поиск параметров"></div><div id="formPanel"></div></div></div>
<footer class="footer"><div class="footer-note" id="footerNote"><span data-icon="shield"></span><span>Конфигурации остаются на этом компьютере</span></div><div class="footer-actions"><button class="btn ghost" id="discard" disabled>Отменить изменения</button><button class="btn secondary" id="validate" disabled><span data-icon="check"></span>Проверить</button><button class="btn primary" id="save" disabled><span data-icon="save"></span>Сохранить</button></div></footer></main></div>
<dialog id="newDialog"><div class="dialog-header"><div><h3 id="newTitle">Новый профиль</h3><p id="newSubtitle">Основа — config_demo.ini. Ключи будут очищены, профиль выключен.</p></div><button class="close-dialog" data-close="newDialog" aria-label="Закрыть">×</button></div><div class="dialog-body"><div class="modal-field"><label for="newMode">Папка профиля</label><select id="newMode" class="control"><option value="monitoring">Мониторинг</option><option value="trading">Торговля</option></select></div><div class="modal-field"><label for="newName">Имя файла</label><input class="control" id="newName" placeholder="finam_main.ini" autocomplete="off"><p class="help">Можно использовать кириллицу, латиницу, цифры, пробелы внутри имени, дефис и подчеркивание.</p></div><div class="alert error hidden" id="newError"></div></div><div class="dialog-footer"><button class="btn secondary" data-close="newDialog">Отмена</button><button class="btn primary" id="createDraft">Создать черновик</button></div></dialog>
<dialog id="reviewDialog"><div class="dialog-header"><div><h3>Проверка перед сохранением</h3><p id="reviewSubtitle"></p></div><button class="close-dialog" data-close="reviewDialog" aria-label="Закрыть">×</button></div><div class="dialog-body"><div id="reviewIssues"></div><div id="diff"></div><p class="small-note" id="backupNote"></p></div><div class="dialog-footer"><button class="btn secondary" data-close="reviewDialog">Вернуться к форме</button><button class="btn primary" id="confirmSave"><span data-icon="save"></span>Сохранить изменения</button></div></dialog>
<dialog id="leaveDialog"><div class="dialog-header"><div><h3>Есть несохраненные изменения</h3><p>При продолжении текущий черновик будет закрыт.</p></div><button class="close-dialog" data-close="leaveDialog" aria-label="Закрыть">×</button></div><div class="dialog-footer"><button class="btn secondary" data-close="leaveDialog">Остаться</button><button class="btn primary" id="leaveConfirm">Продолжить</button></div></dialog>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script nonce="__NONCE__">
'use strict';
const $=id=>document.getElementById(id);
const svgPaths={user:'<circle cx="12" cy="8" r="3"/><path d="M5 21v-3a7 7 0 0 1 14 0v3"/>',chart:'<path d="M4 20V5m0 15h17M8 16l4-6 4 3 5-8"/>',activity:'<path d="M2 12h4l3-8 5 16 3-8h5"/>',layers:'<path d="m12 3 10 5-10 5L2 8l10-5Zm-9 10 9 5 9-5M3 18l9 5 9-5"/>',clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',wallet:'<path d="M20 7V4H5a2 2 0 0 0 0 4h16v12H5a3 3 0 0 1-3-3V6m19 6h-5v4h5"/>',database:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',folder:'<path d="M3 6V4h6l3 3h9v13H3V6Z"/>',files:'<path d="M8 3h8l4 4v14H8V3Zm8 0v5h4M4 7v14m0 0h11"/>',file:'<path d="M5 2h9l5 5v15H5V2Zm9 0v6h5M8 13h8m-8 4h6"/>',plus:'<path d="M12 4v16M4 12h16"/>',refresh:'<path d="M20 7a9 9 0 1 0 1 8M20 3v5h-5"/>',copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 5V3H3v14h2"/>',check:'<path d="m4 12 5 5L20 6"/>',shield:'<path d="m12 2 8 3v6c0 6-8 11-8 11S4 17 4 11V5l8-3Z"/><path d="m8 11 3 3 5-6"/>',save:'<path d="M4 3h13l4 4v14H3V3h1Zm3 0v6h9V3M7 21v-8h10v8"/>',chevron:'<path d="m6 9 6 6 6-6"/>',eye:'<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12Z"/><circle cx="12" cy="12" r="3"/>',code:'<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-14-3 16"/>',trash:'<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>'};
svgPaths.upload='<path d="M4 16v4h16v-4M12 16V4m-5 5 5-5 5 5"/>';
svgPaths.download='<path d="M4 16v4h16v-4M12 4v12m-5-5 5 5 5-5"/>';
function icon(name){return '<svg viewBox="0 0 24 24" aria-hidden="true">'+(svgPaths[name]||svgPaths.file)+'</svg>';}
function icons(root=document){root.querySelectorAll('[data-icon]').forEach(e=>e.innerHTML=icon(e.dataset.icon));}
const fragment=new URLSearchParams(location.hash.slice(1));
if(fragment.get('token')){sessionStorage.setItem('algobond.editor.token',fragment.get('token'));history.replaceState(null,'',location.pathname);}
const token=sessionStorage.getItem('algobond.editor.token')||'';
const state={mode:'monitoring',files:[],doc:null,patches:{},tab:'profile',boot:null,dirty:false,raw:false,rawText:'',removeDatabase:false,limit:150,newSource:null,reviewPayload:null,busy:false,loadSerial:0};
let leaveResolve=null,toastTimer;
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;}
function toast(message,error=false){const t=$('toast');t.textContent=message;t.className='toast show'+(error?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.remove('show'),6500);}
async function api(url,body){const options={headers:{'X-Editor-Token':token}};if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}const response=await fetch(url,options);let data;try{data=await response.json();}catch{throw Error('Сервер вернул неожиданный ответ. Проверьте окно запуска.');}if(!response.ok){const error=Error(data.error||'Не удалось выполнить запрос.');error.details=data.details||[];error.status=response.status;throw error;}return data;}
function setBusy(b){state.busy=b;document.querySelectorAll('#save,#confirmSave,#validate,#createDraft').forEach(e=>e.disabled=b||(!state.doc&&e.id!=='createDraft')||(e.id==='confirmSave'&&!!document.querySelector('#reviewIssues .error')));$('importIni').disabled=b||!state.boot;$('exportIni').disabled=b;}
function value(id){if(Object.hasOwn(state.patches,id))return state.patches[id];const dot=id.indexOf('.');return state.doc?.values?.[id.slice(0,dot)]?.[id.slice(dot+1)];}
function effective(id,fallback=''){const v=value(id);return v==null?String(fallback):String(v);}
function bool(v){return ['true','yes','1','t','y'].includes(String(v).toLowerCase());}
function allFields(){return state.boot.groups.flatMap(t=>t.groups.flatMap(g=>g.fields));}
function fieldSpec(id){return allFields().find(f=>f.id===id);}
function prettyNumber(v){const n=Number(v);return Number.isFinite(n)?new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(n):v;}
function setValue(id,v){const base=state.doc?.values?.[id.split('.')[0]]?.[id.split('.')[1]];if((v===null&&base===undefined)||v===base)delete state.patches[id];else state.patches[id]=v;state.dirty=state.doc.create||state.doc.formText||state.raw||state.removeDatabase||Object.keys(state.patches).length>0;updateTop();}
function updateTop(){const d=state.doc;if(!d)return;const name=effective('client.client_name',d.name.replace(/\.ini$/i,''))||d.name;$('portfolioTitle').textContent=name;$('portfolioDescription').textContent=d.name+(d.create?' · новый файл':' · параметры подключения и стратегии');$('crumbFile').textContent=d.name;$('crumbMode').textContent=state.mode==='trading'?'Торговля':'Мониторинг';const active=effective('client.active','0')==='1';$('activeBadge').textContent=active?'Активен':'Неактивен';$('activeBadge').className='badge'+(active?'':' off');$('dirtyBadge').classList.toggle('hidden',!state.dirty);$('summaryBroker').textContent=(effective('client.broker_name','Не выбран брокер')||'Не выбран брокер')+' · '+(effective('client.type_account','Bonds')==='Bonds'?'Облигации':effective('client.type_account'));$('summaryLogin').textContent=effective('client.login','Логин не задан')||'Логин не задан';$('summaryBonds').replaceChildren(document.createTextNode(prettyNumber(effective('settings.portfolio_max_percent_bonds','100'))),el('span','','%'));$('summaryRisk').replaceChildren(document.createTextNode(prettyNumber(effective('settings.p_risk','2.9'))),el('span','','%'));$('summaryDays').replaceChildren(document.createTextNode(prettyNumber(effective('settings.days_planning_max','30'))),el('span','','дн.'));$('discard').disabled=!state.dirty;$('save').disabled=state.busy;$('validate').disabled=state.busy;const note=state.dirty?'Есть несохраненные изменения · Ctrl+S — сохранить':'Изменений нет · при сохранении создается резервная копия';$('footerNote').lastElementChild.textContent=note;}
function renderFiles(){const q=$('fileSearch').value.trim().toLowerCase(),b=$('brokerFilter').value,a=$('activeFilter').value;const filtered=state.files.filter(f=>(!q||[f.name,f.label,f.login,f.broker].join(' ').toLowerCase().includes(q))&&(!b||f.broker===b)&&(!a||(a==='on')===f.active));$('listCount').textContent='КОНФИГУРАЦИИ · '+filtered.length;$('files').replaceChildren();if(!filtered.length){const e=el('div','no-files',state.files.length?'Нет подходящих профилей':'В этой папке пока нет INI-файлов');if(!state.files.length){e.append(el('br'));const btn=el('button','empty-list-link','Создать первый профиль');btn.onclick=()=>newDialog(false);e.append(btn);}$('files').append(e);}for(const f of filtered.slice(0,state.limit)){const btn=el('button','file'+(!state.doc?.create&&state.doc?.name===f.name?' selected':''));const mark=el('span','file-icon');mark.innerHTML=icon('file');const body=el('div','file-body');body.append(el('div','file-name',f.label||f.name));const meta=el('div','file-meta');meta.append(el('i','dot'+(f.error?' bad':f.active?' on':'')),document.createTextNode(f.error?'Проверьте INI':(f.broker||'Брокер не задан')+' · '+(f.active?'активен':'неактивен')));body.append(meta,el('div','file-sub',f.name));btn.append(mark,body);btn.title=f.name;btn.onclick=()=>loadFile(f.name);$('files').append(btn);}if(filtered.length>state.limit){const more=el('button','btn ghost','Показать еще 150');more.onclick=()=>{state.limit+=150;renderFiles();};$('files').append(more);}}
async function listFiles(){const mode=state.mode;const result=await api('/api/files?mode='+encodeURIComponent(mode));if(state.mode!==mode)return;state.files=result.files;const selected=$('brokerFilter').value;$('brokerFilter').replaceChildren(new Option('Все брокеры',''));[...new Set(state.files.map(f=>f.broker).filter(Boolean))].sort().forEach(b=>$('brokerFilter').add(new Option(b,b)));$('brokerFilter').value=selected;renderFiles();$('emptyTitle').textContent=state.files.length?'Выберите профиль':'Конфигураций пока нет';$('emptyText').textContent=state.files.length?'Выберите файл в списке слева, чтобы открыть его настройки.':'Создайте первый профиль на основе config_demo.ini. Для мониторинга и торговли используются отдельные папки.';}
function confirmLeave(){if(!state.dirty)return Promise.resolve(true);return new Promise(resolve=>{leaveResolve=resolve;$('leaveDialog').showModal();});}
async function changeMode(mode){if(mode===state.mode||!await confirmLeave())return;state.loadSerial++;state.mode=mode;state.doc=null;state.patches={};state.dirty=false;state.raw=false;state.limit=150;$('fileSearch').value='';$('brokerFilter').value='';$('activeFilter').value='';document.querySelectorAll('[data-mode]').forEach(b=>{b.classList.toggle('selected',b.dataset.mode===mode);b.setAttribute('aria-selected',String(b.dataset.mode===mode));});$('folderTitle').textContent=mode==='trading'?'Папка торговли':'Папка мониторинга';$('folderPath').textContent=state.boot.folders[mode];$('editor').classList.add('hidden');$('empty').classList.remove('hidden');['save','validate','discard'].forEach(id=>$(id).disabled=true);await listFiles();}
function acceptDoc(doc){state.doc=doc;state.patches={};state.removeDatabase=false;state.raw=false;state.rawText=doc.text;state.dirty=!!doc.create;state.tab=doc.errors?.some(e=>e.startsWith('Некорректный INI'))?'raw':'profile';$('empty').classList.add('hidden');$('editor').classList.remove('hidden');$('fieldSearch').value='';$('sidebar').classList.remove('open');renderTabs();renderPanel();updateTop();renderFiles();$('scroll').scrollTop=0;}
async function loadFile(name){if(!await confirmLeave())return;const serial=++state.loadSerial;try{const doc=await api('/api/file?mode='+state.mode+'&name='+encodeURIComponent(name));if(serial!==state.loadSerial)return;acceptDoc(doc);}catch(e){toast(e.message,true);}}
function renderTabs(){$('tabs').replaceChildren();for(const tab of [...state.boot.groups,{id:'extra',title:'Дополнительно',icon:'layers'},{id:'raw',title:'INI',icon:'code'}]){const btn=el('button','tab'+(state.tab===tab.id?' selected':''));btn.innerHTML=icon(tab.icon);btn.append(document.createTextNode(tab.title));btn.setAttribute('role','tab');btn.setAttribute('aria-selected',String(state.tab===tab.id));btn.onclick=()=>switchTab(tab.id);$('tabs').append(btn);}}
function payload(){const d=state.doc;const patches={...state.patches};if(state.removeDatabase)Object.keys(patches).filter(k=>k.startsWith('database.')).forEach(k=>delete patches[k]);const p={mode:state.mode,name:d.name,version:d.version,create:!!d.create,patches,remove_database:state.removeDatabase};if(d.create&&d.bom)p.bom=true;if(state.raw)p.text=state.rawText;else if(d.create||d.formText)p.text=d.text;return p;}
async function switchTab(tab){if(tab===state.tab)return;try{if(tab==='raw'&&!state.raw){if(!Object.keys(state.doc.values||{}).length)state.rawText=state.doc.text;else{const result=await api('/api/preview',payload());state.rawText=result.text;}state.raw=true;state.patches={};state.removeDatabase=false;}else if(state.tab==='raw'&&tab!=='raw'){const result=await api('/api/preview',payload());state.doc.text=result.text;state.doc.values=result.values;state.doc.errors=result.errors;state.doc.warnings=result.warnings;state.patches={};state.removeDatabase=false;state.raw=false;state.doc.formText=true;}state.tab=tab;renderTabs();renderPanel();updateTop();}catch(e){showIssues([e.message,...(e.details||[])],[]);toast('Исправьте INI перед возвратом к форме.',true);}}
function showIssues(errors=[],warnings=[],target=$('issues')){target.replaceChildren();for(const [items,cls] of [[errors,'alert error'],[warnings,'alert']]){if(!items.length)continue;const box=el('div',cls),list=el('ul');items.forEach(x=>list.append(el('li','',x)));box.append(list);target.append(box);}}
function sectionBox(title,index,open){const d=el('details','section');d.open=!!open;const s=el('summary');const left=el('span','section-icon');left.append(el('span','section-number',String(index).padStart(2,'0')),document.createTextNode(title));const arrow=el('span','chevron');arrow.innerHTML=icon('chevron');s.append(left,arrow);d.append(s);const body=el('div','section-content');d.append(body);return {d,body};}
function renderPanel(){const panel=$('formPanel');panel.replaceChildren();$('fieldSearchWrap').classList.toggle('hidden',state.tab==='raw');showIssues(state.doc.errors||[],state.doc.warnings||[]);if(state.tab==='raw'){panel.append(el('p','raw-warning','Режим точного редактирования. Здесь видны в том числе токены и пароли. Секции, комментарии и неизвестные параметры сохраняются.'));const area=el('textarea','raw-area');area.id='rawIni';area.setAttribute('aria-label','Исходный INI');area.spellcheck=false;area.value=state.rawText;area.oninput=()=>{state.rawText=area.value;state.raw=true;state.dirty=true;updateTop();};area.addEventListener('keydown',e=>{if(e.key==='Tab'){e.preventDefault();const a=area.selectionStart,b=area.selectionEnd;area.setRangeText('    ',a,b,'end');area.dispatchEvent(new Event('input'));}});panel.append(area);return;}if(state.tab==='extra'){renderExtra(panel);return;}const tab=state.boot.groups.find(t=>t.id===state.tab);panel.append(el('div','panel-title',tab.title),el('div','panel-subtitle',tab.subtitle));if(tab.id==='database'){const row=el('div','database-switch'),copy=el('div');copy.append(el('strong','','Сохранять сведения в базу данных'),el('p','','Выключение удалит секцию [database] целиком.'));const enabled=!state.removeDatabase&&(!!state.doc.values.database||Object.keys(state.patches).some(k=>k.startsWith('database.')&&state.patches[k]!==null));row.append(copy,switchControl(enabled,checked=>{state.removeDatabase=!checked;if(checked&&!state.doc.values.database){for(const f of tab.groups[0].fields)state.patches[f.id]=f.key==='port'?'0':f.default;}state.dirty=true;renderPanel();updateTop();}));panel.append(row);}
tab.groups.forEach((g,i)=>{if(tab.id==='database'&&i===0&&(state.removeDatabase||(!state.doc.values.database&&!Object.keys(state.patches).some(k=>k.startsWith('database.')&&state.patches[k]!==null))))return;const {d,body}=sectionBox(g.title,i+1,g.open);const grid=el('div','field-grid');g.fields.forEach(f=>grid.append(renderField(f)));body.append(grid);panel.append(d);});icons(panel);filterFields();}
function switchControl(checked,change,danger=false){const label=el('label','switch'+(danger?' danger':''));const input=document.createElement('input');input.type='checkbox';input.checked=checked;input.onchange=()=>change(input.checked);label.append(input,el('span','switch-track'));return label;}
function durationSeconds(amount,unit){if(![1,60,3600,86400].includes(Number(unit)))return 'NaN';if(String(amount).trim()==='')return '';const total=Number(amount)*Number(unit),rounded=Math.round(total);return Number.isFinite(total)&&total>=0&&Math.abs(total-rounded)<1e-6&&rounded<=2147483647?String(rounded):'NaN';}
function durationUnit(seconds){const value=Number(seconds);for(const unit of [86400,3600,60])if(value>0&&value%unit===0)return unit;return 1;}
function renderField(f){const wrap=el('div','field'+(['textarea','allocation','boards'].includes(f.kind)?' full':''));wrap.dataset.search=(f.label+' '+f.id).toLowerCase();wrap.dataset.field=f.id;const labelRow=el('div','label-row'),label=el('label','label',f.label);label.htmlFor='field-'+f.id;const absent=value(f.id)==null;if(absent){const badge=el('span','default-badge','по умолчанию');badge.setAttribute('aria-hidden','true');label.append(badge);}const reset=el('button','reset-field','↶');reset.title='Убрать параметр из INI и использовать значение по умолчанию';reset.setAttribute('aria-label','По умолчанию: '+f.label);reset.onclick=()=>{setValue(f.id,null);const replacement=renderField(f);wrap.replaceWith(replacement);};labelRow.append(label,reset);wrap.append(labelRow);const current=effective(f.id,f.default);const inputChanged=v=>setValue(f.id,v);const kind=f.kind;
if(['bool','active','danger','download'].includes(kind)){const row=el('div','switch-row');const checked=kind==='download'?value(f.id)!=null&&value(f.id)!=='':kind==='active'?current==='1':bool(current);row.append(el('span','switch-text',checked?'Включено':'Выключено'));const sw=switchControl(checked,c=>{row.firstChild.textContent=c?'Включено':'Выключено';inputChanged(kind==='active'?(c?'1':'0'):kind==='download'?(c?'True':null):(c?'True':'False'));},kind==='danger');sw.querySelector('input').id='field-'+f.id;sw.querySelector('input').setAttribute('aria-label',f.label);row.append(sw);wrap.append(row);
}else if(kind==='duration'){const row=el('div','range-row'),input=el('input','control'),unit=el('select','control'),hint=el('p','field-help');input.type='number';input.min='0';input.step='any';input.id='field-'+f.id;input.setAttribute('aria-label',f.label);for(const [v,t] of [[1,'Секунды'],[60,'Минуты'],[3600,'Часы'],[86400,'Дни']])unit.append(new Option(t,String(v)));unit.value=String(durationUnit(current));input.value=String(Number(current)/Number(unit.value));unit.setAttribute('aria-label','Единицы срока заявки');const show=seconds=>{hint.textContent=seconds==='0'?'Без автоматической отмены':seconds==='NaN'?'Укажите срок в целых секундах':'В INI: '+seconds+' с';};const sync=()=>{const seconds=durationSeconds(input.value,unit.value);inputChanged(seconds);show(seconds);};input.oninput=sync;unit.onchange=sync;row.append(input,unit);wrap.append(row,hint);show(String(current));
}else if(kind==='range'){const row=el('div','range-row'),range=document.createElement('input');range.type='range';range.min=f.min;range.max=f.max;range.step=f.step||1;range.value=current;range.setAttribute('aria-label',f.label+' — ползунок');const numWrap=el('div','range-value'),num=document.createElement('input');num.type='number';num.className='control';num.id='field-'+f.id;num.min=f.min;num.max=f.max;num.step=f.step||1;num.value=current;num.setAttribute('aria-label',f.label);const units=el('span','unit',f.unit||'');numWrap.append(num,units);range.oninput=()=>{num.value=range.value;inputChanged(range.value);};num.oninput=()=>{range.value=num.value;inputChanged(num.value);};row.append(range,numWrap);const scale=el('div','range-scale');scale.append(el('span','',String(f.min)+(f.unit||'')),el('span','',String(f.max)+(f.unit||'')));wrap.append(row,scale);
}else if(kind==='boards'){const chips=el('div','chips'),entry=el('input','control chip-add');entry.id='field-'+f.id;entry.value=current;entry.setAttribute('aria-label',f.label);function chipsRender(){chips.replaceChildren();const selected=entry.value.split(',').map(s=>s.trim());for(const option of f.options){const btn=el('button','chip'+(selected.includes(option)?' selected':''),option);btn.onclick=()=>{const s=entry.value.split(',').map(x=>x.trim()).filter(Boolean);entry.value=s.includes(option)?s.filter(x=>x!==option).join(','):[...s,option].join(',');inputChanged(entry.value);chipsRender();};chips.append(btn);}}entry.oninput=()=>{inputChanged(entry.value);chipsRender();};chipsRender();wrap.append(chips,entry);
}else if(kind==='allocation'){renderAllocation(wrap,f,current);
}else if(['select','weekday','hour'].includes(kind)){const select=el('select','control');select.id='field-'+f.id;let options=kind==='weekday'?['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'].map((n,i)=>[String(i+1),n]):kind==='hour'?Array.from({length:24},(_,i)=>[String(i),String(i).padStart(2,'0')+':00']):f.options.map(x=>[x,x]);if(current&&!options.some(([v])=>v.toLowerCase()===current.toLowerCase())&&!['fkviking','flviking'].includes(current.toLowerCase()))options=[[current,current+' (из файла)'],...options];for(const [v,l] of options)select.add(new Option(l,v));const actual=options.find(([v])=>v.toLowerCase()===current.toLowerCase());select.value=actual?actual[0]:current;select.onchange=()=>inputChanged(select.value);wrap.append(select);
}else{const shell=el('div','input-wrap'),input=kind==='textarea'?el('textarea','control'):el('input','control');input.id='field-'+f.id;input.autocomplete='off';input.value=kind==='datetime'?current.replace(' ','T'):current;input.placeholder=kind==='secret'?'Введите ключ…':absent?'По умолчанию: '+f.default:'';if(kind!=='textarea'){input.type=kind==='secret'?'password':kind==='date'?'date':kind==='datetime'?'datetime-local':['number','integer'].includes(kind)?'number':'text';if('min'in f)input.min=f.min;if('max'in f)input.max=f.max;if(input.type==='number')input.step=f.step|| (kind==='integer'?1:'any');if(kind==='datetime')input.step=1;}input.oninput=()=>{let v=input.value;if(kind==='datetime'&&v){v=v.replace('T',' ');if(v.length===16)v+=':00';}inputChanged(v);};if(kind==='secret'){input.classList.add('secret-control');const reveal=el('button','secret-btn');reveal.innerHTML=icon('eye');reveal.title='Показать / скрыть ключ';reveal.setAttribute('aria-label',reveal.title);reveal.onclick=()=>input.type=input.type==='password'?'text':'password';shell.append(input,reveal);}else{if(f.unit){input.classList.add('with-unit');shell.append(input,el('span','unit',f.unit));}else shell.append(input);}if(kind==='fund'){const id='fund-options-'+f.key;input.setAttribute('list',id);const dl=el('datalist');dl.id=id;f.options.forEach(x=>dl.append(new Option(x,x)));shell.append(dl);}wrap.append(shell);}
wrap.append(el('div','field-key',f.id));if(f.help)wrap.append(el('div','help',f.help));return wrap;}
function renderAllocation(wrap,f,current){let stages;try{stages=current?current.split('|').map(s=>s.split(';').filter(x=>x&&x!=='null').map(row=>{const p=row.split(':'),r=p[0].split('-');if(r.length!==2||p.length<2||p.length>7||[...r,...p.slice(1)].some(v=>v.trim()===''||!Number.isFinite(Number(v))))throw Error();return [r[0],r[1],...p.slice(1),...Array(7).fill('')].slice(0,8);})):[];}catch{const e=el('div','allocation-error','Не удалось построить таблицу из текущего значения. Его можно отредактировать в строке ниже, затем нажать «Построить таблицу».');wrap.append(e);stages=null;}const host=el('div');wrap.append(host);function emit(){const value=stages.map(rows=>rows.map(r=>{let p=r.slice(2);while(p.length>1&&p[p.length-1]==='')p.pop();return r[0]+'-'+r[1]+':'+p.map((v,i)=>i>0&&v===''?'0':v).join(':');}).join(';')).join('|');raw.value=value;setValue(f.id,value);}
function draw(){host.replaceChildren();if(!stages)return;stages.forEach((rows,i)=>{const box=el('div','allocation-box'),header=el('div','allocation-header');header.append(el('strong','','Этап '+(i+1)));const del=el('button','btn ghost','Удалить этап');del.onclick=()=>{stages.splice(i,1);emit();draw();};header.append(del);const tableWrap=el('div','allocation-table-wrap'),table=el('table','allocation-table'),head=el('tr');['От, дней','До, дней','Доля, %','Позиция, %','Разница покупки','Рост продажи, %','Дни продажи','Доходность, %',''].forEach(x=>head.append(el('th','',x)));const thead=el('thead');thead.append(head);table.append(thead);const tbody=el('tbody');rows.forEach((row,j)=>{const tr=el('tr');row.forEach((v,k)=>{const td=el('td'),input=el('input','control');input.type='number';input.min='0';input.step='any';if(k===2||k===3)input.max='100';input.value=v;input.setAttribute('aria-label','Этап '+(i+1)+', строка '+(j+1)+': '+head.children[k].textContent);input.oninput=()=>{row[k]=input.value;emit();updateBar();};td.append(input);tr.append(td);});const td=el('td'),remove=el('button','btn ghost remove');remove.innerHTML=icon('trash');remove.title='Удалить интервал';remove.setAttribute('aria-label',remove.title);remove.onclick=()=>{rows.splice(j,1);emit();draw();};td.append(remove);tr.append(td);tbody.append(tr);});table.append(tbody);tableWrap.append(table);const bars=el('div','allocation-bars'),total=el('div','allocation-total');function updateBar(){bars.replaceChildren();rows.forEach((r,j)=>{const span=el('span');span.style.width=Math.max(0,Math.min(100,Number(r[2])||0))+'%';span.style.opacity=String(1-j*.12);bars.append(span);});const sum=rows.reduce((s,r)=>s+(Number(r[2])||0),0);total.textContent='Распределено '+prettyNumber(sum)+'% из 100%';total.style.color=sum>100?'#b55757':'';}updateBar();const add=el('button','btn ghost','+ Добавить интервал');add.onclick=()=>{rows.push(['5','90','0','2','','','','']);emit();draw();};box.append(header,tableWrap,bars,total,add);host.append(box);});const addStage=el('button','btn secondary','+ Добавить этап');addStage.onclick=()=>{stages.push([['5','365','100','2','','','','']]);emit();draw();};host.append(addStage);}
const raw=el('textarea','control raw-allocation');raw.id='field-'+f.id;raw.value=current;raw.rows=2;raw.setAttribute('aria-label',f.label+' — строка INI');raw.oninput=()=>setValue(f.id,raw.value);const rebuild=el('button','btn ghost','Построить таблицу из строки');rebuild.onclick=()=>{setValue(f.id,raw.value);wrap.replaceWith(renderField(f));};draw();wrap.append(raw,rebuild);}
function renderExtra(panel){panel.append(el('div','panel-title','Дополнительные параметры'),el('div','panel-subtitle','Поля, не вошедшие в основные разделы. Неизвестные ключи сохраняются без удаления.'));const known=new Set(allFields().map(f=>f.id));let count=0;for(const [section,opts] of Object.entries(state.doc.values)){const unknown=Object.entries(opts).filter(([k])=>!known.has(section+'.'+k));if(!unknown.length)continue;const box=sectionBox('['+section+']',++count,true);for(const [key] of unknown){const f={section,key,id:section+'.'+key,label:key,kind:['token','password','license_key','api_key','secret'].some(s=>key.toLowerCase().includes(s))?'secret':'text',default:''};box.body.append(renderField(f));}panel.append(box.d);}if(!count)panel.append(el('div','no-matches','Дополнительных параметров нет. Для добавления произвольного ключа используйте вкладку INI.'));}
function filterFields(){const q=$('fieldSearch').value.trim().toLowerCase();document.querySelectorAll('#formPanel .field').forEach(e=>e.classList.toggle('hidden',!!q&&!e.dataset.search?.includes(q)));document.querySelectorAll('#formPanel details.section').forEach(e=>{const fields=[...e.querySelectorAll('.field')];const visible=fields.some(f=>!f.classList.contains('hidden'));e.classList.toggle('hidden',!!q&&!visible);if(q&&visible)e.open=true;});}
function openDraftDialog(title, subtitle, action, name, source=null){
    state.newSource=source;
    $('newTitle').textContent=title;
    $('newSubtitle').textContent=subtitle;
    $('createDraft').textContent=action;
    $('newMode').value=state.mode;
    $('newName').value=name;
    $('newError').classList.add('hidden');
    $('newDialog').showModal();
    $('newName').focus();
}
async function newDialog(duplicate){
    if(!await confirmLeave())return;
    if(!duplicate){
        openDraftDialog('Новый профиль','Основа — config_demo.ini. Ключи будут очищены, профиль выключен.','Создать черновик','');
        return;
    }
    try{
        const source=payload();
        const result=await api('/api/preview',source);
        openDraftDialog('Копия профиля','Копия сохранит реквизиты подключения. Новый профиль будет выключен.',
            'Создать черновик',source.name.replace(/\.ini$/i,'_copy.ini'),{text:result.text});
    }catch(e){toast('Сначала исправьте текущий файл: '+e.message,true);}
}
async function importFile(){
    const input=$('importFile');
    const file=input.files[0];
    input.value='';
    if(!file||state.busy)return;
    if(!/\.ini$/i.test(file.name)){toast('Выберите файл с расширением .ini.',true);return;}
    if(file.size>state.boot.max_bytes){toast('Файл больше 1 МиБ.',true);return;}
    let text;
    try{
        const bytes=await file.arrayBuffer();
        text=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(bytes);
    }catch{
        toast('Не удалось прочитать файл. Выберите INI в кодировке UTF-8.',true);
        return;
    }
    if(state.busy||!await confirmLeave())return;
    const bom=text.startsWith('\uFEFF');
    openDraftDialog('Импорт конфигурации','Файл откроется как черновик. Настройки, активность профиля и ключи сохранятся.',
        'Открыть черновик',file.name,{text:bom?text.slice(1):text,bom,imported:true});
}
async function createDraft(){
    let name=$('newName').value.trim();
    const error=$('newError');
    if(!name){error.textContent='Введите имя файла.';error.classList.remove('hidden');return;}
    if(!/\.ini$/i.test(name))name+='.ini';
    const mode=$('newMode').value;
    const newSource=state.newSource;
    const dialog=$('newDialog');
    setBusy(true);
    try{
        const source=newSource||await api('/api/template');
        const {text,bom=false,imported=false}=source;
        const patches={};
        if(!imported)patches['client.active']='0';
        if(!newSource)patches['client.client_name']=name.replace(/\.ini$/i,'');
        const result=await api('/api/preview',{mode,name,version:null,create:true,text,bom,patches});
        if(!dialog.open)return;
        if(mode!==state.mode){state.dirty=false;await changeMode(mode);}
        state.loadSerial++;
        acceptDoc({mode,name,version:null,create:true,bom,text:result.text,values:result.values,
            errors:result.errors,warnings:result.warnings});
        dialog.close();
        toast('Черновик открыт. Файл появится в папке после сохранения.');
    }catch(e){error.textContent=e.message;error.classList.remove('hidden');}
    finally{setBusy(false);}
}
async function exportIni(){
    if(!state.doc||state.busy)return;
    const {name,bom}=state.doc;
    const request=payload();
    setBusy(true);
    try{
        let text=state.raw?state.rawText:state.doc.text;
        if(request.remove_database||Object.keys(request.patches).length){
            const result=await api('/api/preview',request);
            text=result.text;
        }
        const url=URL.createObjectURL(new Blob([bom?'\uFEFF':'',text],{type:'application/octet-stream'}));
        const link=el('a');
        link.href=url;
        link.download=name;
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(e){toast(e.message,true);}
    finally{setBusy(false);}
}
async function validateCurrent(){if(!state.doc)return;setBusy(true);try{const result=await api('/api/preview',payload());showIssues(result.errors,result.warnings);toast(result.errors.length?'Найдены ошибки: '+result.errors.length:result.warnings.length?'Проверка завершена. Есть замечания к настройкам.':'Проверка пройдена — формат и значения корректны.',!!result.errors.length);$('scroll').scrollTop=Math.min($('scroll').scrollTop,280);}catch(e){showIssues([e.message,...(e.details||[])],[]);toast(e.message,true);}finally{setBusy(false);}}
async function reviewSave(){if(!state.doc||state.busy)return;setBusy(true);try{state.reviewPayload=payload();const result=await api('/api/preview',state.reviewPayload);$('reviewSubtitle').textContent=(state.mode==='trading'?'Торговля':'Мониторинг')+' / '+state.doc.name;showIssues(result.errors,result.warnings,$('reviewIssues'));$('diff').replaceChildren();if(result.changes.length){const table=el('table','diff'),head=el('tr');['Параметр','Было','Станет'].forEach(t=>head.append(el('th','',t)));const thead=el('thead');thead.append(head);table.append(thead);const tbody=el('tbody');result.changes.forEach(c=>{const tr=el('tr');tr.append(el('td','',c.field),el('td','diff-before',c.before===null?'—':c.before||'(пусто)'),el('td','diff-after',c.after===null?'Удален':c.after||'(пусто)'));tbody.append(tr);});table.append(tbody);$('diff').append(table);}else $('diff').append(el('p','small-note','Значения полей не изменились. Могут отличаться комментарии или форматирование.'));$('backupNote').textContent=state.doc.create?'Будет создан новый INI-файл. Торговые скрипты не запускаются.':'Перед записью предыдущая версия будет сохранена в data/configs/editor_backups/'+state.mode+'/. Ключи в сравнении скрыты.';$('confirmSave').disabled=!!result.errors.length;$('reviewDialog').showModal();}catch(e){showIssues([e.message,...(e.details||[])],[]);toast(e.message,true);}finally{setBusy(false);if($('reviewDialog').open&&$('reviewIssues .error'))$('confirmSave').disabled=true;}}
async function commitSave(){if(!state.reviewPayload)return;setBusy(true);try{const result=await api('/api/save',state.reviewPayload);$('reviewDialog').close();acceptDoc(result.document);await listFiles();toast(result.unchanged?'Файл уже содержит эти значения.':result.backup?'Сохранено. Предыдущая версия помещена в резервные копии.':'Новый профиль сохранен.');}catch(e){showIssues([e.message,...(e.details||[])],[],$('reviewIssues'));$('confirmSave').disabled=true;toast(e.message,true);}finally{setBusy(false);}}
async function refresh(){if(!await confirmLeave())return;const name=state.doc&&!state.doc.create?state.doc.name:null;state.dirty=false;await listFiles();if(name){const doc=await api('/api/file?mode='+state.mode+'&name='+encodeURIComponent(name));acceptDoc(doc);}else if(state.doc?.create){state.doc=null;$('editor').classList.add('hidden');$('empty').classList.remove('hidden');['save','validate','discard'].forEach(id=>$(id).disabled=true);}}
document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>changeMode(b.dataset.mode).catch(e=>toast(e.message,true)));
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).close());
$('leaveConfirm').onclick=()=>{const resolve=leaveResolve;leaveResolve=null;$('leaveDialog').close();if(resolve)resolve(true);};$('leaveDialog').addEventListener('close',()=>{if(leaveResolve){leaveResolve(false);leaveResolve=null;}});
$('fileSearch').oninput=()=>{state.limit=150;renderFiles();};$('brokerFilter').onchange=renderFiles;$('activeFilter').onchange=renderFiles;$('fieldSearch').oninput=filterFields;
['newProfile','sidebarNew','emptyNew'].forEach(id=>$(id).onclick=()=>newDialog(false));$('duplicate').onclick=()=>newDialog(true);$('createDraft').onclick=createDraft;$('validate').onclick=validateCurrent;$('save').onclick=reviewSave;$('confirmSave').onclick=commitSave;$('refresh').onclick=()=>refresh().catch(e=>toast(e.message,true));$('discard').onclick=()=>refresh().catch(e=>toast(e.message,true));$('mobileToggle').onclick=()=>$('sidebar').classList.toggle('open');
$('importIni').onclick=()=>$('importFile').click();
$('importFile').onchange=importFile;
$('exportIni').onclick=exportIni;
$('newDialog').addEventListener('close',()=>{if(!$('newDialog').open)state.newSource=null;});
window.addEventListener('beforeunload',e=>{if(state.dirty){e.preventDefault();e.returnValue='';}});document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();if(!$('reviewDialog').open&&!$('newDialog').open)reviewSave();}});
async function init(){icons();try{state.boot=await api('/api/boot');$('folderPath').textContent=state.boot.folders[state.mode];await listFiles();if(state.files.length)await loadFile(state.files[0].name);setBusy(false);}catch(e){$('emptyTitle').textContent='Не удалось открыть проект';$('emptyText').textContent=e.message;$('emptyNew').disabled=true;toast(e.message,true);}}init();
</script></body></html>'''


def main(argv=None):
    parser = argparse.ArgumentParser(description="Локальный редактор конфигураций AlgoBond")
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent,
                        help="Корень AlgoBond с config_demo.ini и логотипом")
    parser.add_argument("--port", type=int, default=0, help="Локальный порт; 0 — выбрать свободный")
    parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер автоматически")
    args = parser.parse_args(argv)
    root = args.project.resolve()
    if not (root / "config_demo.ini").is_file():
        parser.error("Не найден config_demo.ini. Передайте --project с папкой AlgoBond.")
    if not 0 <= args.port <= 65535:
        parser.error("Порт должен быть от 0 до 65535.")
    with EditorServer(("127.0.0.1", args.port), ConfigStore(root)) as server:
        url = server.origin + "/#token=" + server.token
        print("AlgoBond — редактор конфигураций", flush=True)
        print("Откройте эту личную ссылку в браузере:", flush=True)
        print(url, flush=True)
        print("Остановка: Ctrl+C в этом окне. Сам редактор не запускает торговлю.", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.4)
        except KeyboardInterrupt:
            print("\nРедактор остановлен.", flush=True)


if __name__ == "__main__":
    main()
