"""
VK Warehouse Bot — единый файл.
"""
import io
import csv
import json
import time
import shutil
import sqlite3
import tempfile
import os
import re
import calendar
import urllib.request
import traceback
import threading
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from contextlib import contextmanager
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict

import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.dates import DateFormatter
plt.rcParams['font.family'] = 'DejaVu Sans'


# ════════════════════════════ НАСТРОЙКИ ════════════════════════════

TOKEN = os.getenv('VK_TOKEN', '').strip()
GROUP_ID = int(os.getenv('VK_GROUP_ID', '0'))

ADMIN_IDS = []
TRUCK_CAPACITY = 19.0
TIMEZONE_OFFSET_HOURS = 5
MORNING_REPORT_HOUR = 8
MORNING_REPORT_MINUTE = 0
BACKUP_HOUR = 23
BACKUP_MINUTE = 0
BACKUP_KEEP = 60

DATA_DIR = '/app/data'
DB_PATH = os.path.join(DATA_DIR, 'shipments.db')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))

A4_PORTRAIT = (8.27, 11.69)
A4_LANDSCAPE = (11.69, 8.27)

COLOR_DARK = '#1a3d63'
COLOR_ACCENT = '#4a76a8'
COLOR_LIGHT = '#e8eef6'
COLOR_GRID = '#d0d9e4'


def tz_now(): return datetime.now(TZ)
def tz_today(): return tz_now().date()
def now_iso(): return tz_now().strftime('%Y-%m-%d %H:%M:%S')


_states = {}
def set_state(user_id, **kw): _states[user_id] = kw
def update_state(user_id, **kw):
    s = _states.setdefault(user_id, {}); s.update(kw); return s
def get_state(user_id): return dict(_states.get(user_id, {}))
def clear_state(user_id): _states.pop(user_id, None)


# ════════════════════════════ БАЗА ДАННЫХ ════════════════════════════

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        yield conn; conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS shipments (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                client_name   TEXT NOT NULL,
                tonnage       REAL NOT NULL,
                trucks        REAL NOT NULL,
                shipment_date TEXT NOT NULL,
                comment       TEXT DEFAULT '',
                created_at    TEXT NOT NULL,
                created_by    INTEGER,
                updated_at    TEXT,
                updated_by    INTEGER
            );
            CREATE TABLE IF NOT EXISTS clients (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT UNIQUE NOT NULL COLLATE NOCASE,
                last_used TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id        INTEGER PRIMARY KEY,
                first_name     TEXT,
                role           TEXT DEFAULT 'manager',
                morning_report INTEGER DEFAULT 1,
                last_seen      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ship_date ON shipments(shipment_date);
            CREATE INDEX IF NOT EXISTS idx_ship_client ON shipments(client_name);
        """)


def calc_trucks(tonnage):
    if tonnage <= 0: return 0.0
    return round(float(tonnage) / TRUCK_CAPACITY, 2)


def upsert_user(user_id, first_name=None):
    with get_db() as conn:
        conn.execute("""INSERT INTO users (user_id, first_name, last_seen)
            VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
            last_seen=excluded.last_seen,
            first_name=COALESCE(excluded.first_name, users.first_name)""",
            (user_id, first_name, now_iso()))


def get_all_users():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users").fetchall()]


def toggle_morning_report(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT morning_report FROM users WHERE user_id=?",
                           (user_id,)).fetchone()
        if not row: return 1
        new_val = 0 if row['morning_report'] else 1
        conn.execute("UPDATE users SET morning_report=? WHERE user_id=?",
                     (new_val, user_id))
        return new_val


def get_clients(limit=200):
    with get_db() as conn:
        rows = conn.execute("""SELECT id, name FROM clients
            ORDER BY last_used DESC, name ASC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_client(client_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        return dict(row) if row else None


def touch_client(name, conn=None):
    sql = """INSERT INTO clients (name, last_used) VALUES (?, ?)
             ON CONFLICT(name) DO UPDATE SET last_used=excluded.last_used"""
    params = (name.strip(), now_iso())
    if conn is not None:
        conn.execute(sql, params)
    else:
        with get_db() as c: c.execute(sql, params)


def add_shipment(client_name, tonnage, shipment_date, user_id, comment=''):
    trucks = calc_trucks(tonnage)
    with get_db() as conn:
        cur = conn.execute("""INSERT INTO shipments
            (client_name, tonnage, trucks, shipment_date, comment, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (client_name.strip(), float(tonnage), trucks,
             shipment_date, comment, now_iso(), user_id))
        touch_client(client_name, conn=conn)
        return cur.lastrowid


def get_shipment(shipment_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM shipments WHERE id=?",
                           (shipment_id,)).fetchone()
        return dict(row) if row else None


def update_shipment(shipment_id, client_name=None, tonnage=None,
                    shipment_date=None, comment=None, user_id=None):
    fields, values = [], []
    touched = None
    if client_name is not None:
        fields.append("client_name=?"); values.append(client_name.strip())
        touched = client_name.strip()
    if tonnage is not None:
        fields += ["tonnage=?", "trucks=?"]
        values += [float(tonnage), calc_trucks(float(tonnage))]
    if shipment_date is not None:
        fields.append("shipment_date=?"); values.append(shipment_date)
    if comment is not None:
        fields.append("comment=?"); values.append(comment)
    if not fields: return
    fields += ["updated_at=?", "updated_by=?"]
    values += [now_iso(), user_id]; values.append(shipment_id)
    with get_db() as conn:
        conn.execute(f"UPDATE shipments SET {', '.join(fields)} WHERE id=?", values)
        if touched: touch_client(touched, conn=conn)


def delete_shipment(shipment_id):
    with get_db() as conn:
        conn.execute("DELETE FROM shipments WHERE id=?", (shipment_id,))


def delete_shipments_period(start_iso, end_iso):
    with get_db() as conn:
        cur = conn.execute("""DELETE FROM shipments
            WHERE shipment_date BETWEEN ? AND ?""",
            (start_iso, end_iso))
        return cur.rowcount


def count_shipments_period(start_iso, end_iso):
    with get_db() as conn:
        row = conn.execute("""SELECT COUNT(*) as c FROM shipments
            WHERE shipment_date BETWEEN ? AND ?""",
            (start_iso, end_iso)).fetchone()
        return row['c'] if row else 0


def get_shipments_by_date(d):
    if isinstance(d, date): d = d.strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM shipments WHERE shipment_date=? ORDER BY id",
                            (d,)).fetchall()
        return [dict(r) for r in rows]


def get_shipments_by_period(start, end):
    if isinstance(start, date): start = start.strftime("%Y-%m-%d")
    if isinstance(end, date): end = end.strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute("""SELECT * FROM shipments
            WHERE shipment_date BETWEEN ? AND ?
            ORDER BY shipment_date, id""", (start, end)).fetchall()
        return [dict(r) for r in rows]


def get_shipments_by_client(client_name, limit=1000):
    with get_db() as conn:
        rows = conn.execute("""SELECT * FROM shipments
            WHERE client_name=? COLLATE NOCASE
            ORDER BY shipment_date DESC, id DESC LIMIT ?""",
            (client_name, limit)).fetchall()
        return [dict(r) for r in rows]


def stats_for_period(start, end):
    shipments = get_shipments_by_period(start, end)
    by_client, by_day = {}, {}
    total_t = 0.0
    for s in shipments:
        total_t += s['tonnage']
        c = by_client.setdefault(s['client_name'],
                                 {'tonnage': 0.0, 'trucks': 0.0, 'count': 0})
        c['tonnage'] += s['tonnage']
        c['count'] += 1
        d = by_day.setdefault(s['shipment_date'],
                              {'tonnage': 0.0, 'trucks': 0.0, 'count': 0})
        d['tonnage'] += s['tonnage']
        d['count'] += 1
    total_trucks = round(total_t / TRUCK_CAPACITY, 2)
    for c in by_client.values():
        c['trucks'] = round(c['tonnage'] / TRUCK_CAPACITY, 2)
    for d in by_day.values():
        d['trucks'] = round(d['tonnage'] / TRUCK_CAPACITY, 2)
    return {'count': len(shipments), 'total_tonnage': total_t,
            'total_trucks': total_trucks, 'by_client': by_client, 'by_day': by_day}


def client_summary(name):
    shipments = get_shipments_by_client(name, limit=10000)
    if not shipments: return None
    total_t = sum(s['tonnage'] for s in shipments)
    return {'name': name, 'count': len(shipments), 'total_tonnage': total_t,
            'total_trucks': round(total_t / TRUCK_CAPACITY, 2),
            'avg_tonnage': total_t / len(shipments),
            'last_date': shipments[0]['shipment_date'],
            'first_date': shipments[-1]['shipment_date']}


def week_comparison():
    today = tz_today()
    this_start = today - timedelta(days=today.weekday())
    this_end = today
    last_start = this_start - timedelta(days=7)
    last_end = this_start - timedelta(days=1)
    return {'this': stats_for_period(this_start, this_end),
            'last': stats_for_period(last_start, last_end),
            'this_range': (this_start, this_end),
            'last_range': (last_start, last_end)}


# ─────────── БЭКАПЫ ───────────

def make_backup(reason='auto'):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if not os.path.exists(DB_PATH): return None
    stamp = tz_now().strftime('%Y-%m-%d_%H-%M-%S')
    safe_reason = re.sub(r'[^a-zA-Z0-9_-]', '_', reason)[:20]
    dst = os.path.join(BACKUP_DIR, f'shipments_{stamp}_{safe_reason}.db')
    shutil.copy2(DB_PATH, dst)
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith('shipments_'))
    for old in files[:-BACKUP_KEEP]:
        try: os.remove(os.path.join(BACKUP_DIR, old))
        except OSError: pass
    return dst


def list_backups(limit=200):
    if not os.path.isdir(BACKUP_DIR): return []
    files = sorted((f for f in os.listdir(BACKUP_DIR) if f.startswith('shipments_')),
                   reverse=True)
    result = []
    for f in files[:limit]:
        path = os.path.join(BACKUP_DIR, f)
        try: size = os.path.getsize(path)
        except OSError: size = 0
        result.append({'name': f, 'path': path, 'size': size})
    return result


def restore_backup(name):
    src = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(src):
        return False, "Файл бэкапа не найден"
    try:
        if os.path.exists(DB_PATH):
            make_backup('before-restore')
        shutil.copy2(src, DB_PATH)
        return True, None
    except Exception as e:
        return False, str(e)


def get_db_bytes():
    with open(DB_PATH, 'rb') as f: return f.read()


def _p(**kw):
    return json.dumps(kw, ensure_ascii=False)


# ════════════════════════════ КЛАВИАТУРЫ ════════════════════════════

def main_menu(is_admin=False, morning_on=True):
    kb = VkKeyboard(one_time=False)
    kb.add_button('➕ Добавить погрузку', color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button('📋 Сегодня', color=VkKeyboardColor.PRIMARY)
    kb.add_button('🖼 Картинка дня', color=VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button('📋 Завтра', color=VkKeyboardColor.PRIMARY)
    kb.add_button('📅 Другая дата', color=VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button('📊 Статистика', color=VkKeyboardColor.SECONDARY)
    kb.add_button('📈 Графики', color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button('📁 Клиенты', color=VkKeyboardColor.SECONDARY)
    kb.add_button('❓ Помощь', color=VkKeyboardColor.SECONDARY)
    kb.add_line()
    label = '🔔 Сводка: вкл' if morning_on else '🔕 Сводка: выкл'
    kb.add_button(label, color=VkKeyboardColor.SECONDARY)
    if is_admin:
        kb.add_button('⚙️ Настройки', color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def charts_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('📊 По дням', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='chart_pick', chart='by_day'))
    kb.add_button('🥇 Топ клиентов', color=VkKeyboardColor.POSITIVE,
                  payload=_p(cmd='chart_pick', chart='top_clients'))
    kb.add_line()
    kb.add_button('🍰 Доли клиентов', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='chart_pick', chart='pie_clients'))
    kb.add_button('🚛 Топ по фурам', color=VkKeyboardColor.POSITIVE,
                  payload=_p(cmd='chart_pick', chart='top_trucks'))
    kb.add_line()
    kb.add_button('📅 Календарь месяца', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='chart_pick', chart='calendar'))
    kb.add_button('📈 Сравнение недель', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='chart_pick', chart='week_compare'))
    kb.add_line()
    kb.add_button('📈 Тренд клиента', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='chart_pick', chart='client_trend'))
    kb.add_button('📊 Дни недели', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='chart_pick', chart='weekday_avg'))
    kb.add_line()
    kb.add_button('🥧 Будни/выходные', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='chart_pick', chart='weekend_pie'))
    kb.add_line()
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


def chart_period_menu(chart):
    kb = VkKeyboard(inline=True)
    kb.add_button('Неделя', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='chart_draw', chart=chart, period='week'))
    kb.add_button('Месяц', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='chart_draw', chart=chart, period='month'))
    kb.add_line()
    kb.add_button('Год', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='chart_draw', chart=chart, period='year'))
    kb.add_button('📅 Произвольно', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='chart_draw_custom', chart=chart))
    kb.add_line()
    kb.add_button('⬅️ К графикам', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='charts_menu'))
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


def chart_calendar_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('Этот месяц', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='chart_calendar_month', month='this'))
    kb.add_button('Прошлый месяц', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='chart_calendar_month', month='last'))
    kb.add_line()
    kb.add_button('📅 Другой месяц', color=VkKeyboardColor.POSITIVE,
                  payload=_p(cmd='chart_calendar_month', month='custom'))
    kb.add_line()
    kb.add_button('⬅️ К графикам', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='charts_menu'))
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


def clients_menu_chart(clients, page=0, per_page=6):
    kb = VkKeyboard(inline=True)
    start = page * per_page
    chunk = clients[start:start + per_page]
    for i, c in enumerate(chunk):
        kb.add_button(c['name'][:40], color=VkKeyboardColor.SECONDARY,
                      payload=_p(cmd='chart_client_pick', cid=c['id']))
        if i % 2 == 1 and i != len(chunk) - 1:
            kb.add_line()
    nav = []
    if page > 0:
        nav.append(('⬅️', _p(cmd='chart_clients_page', page=page - 1)))
    if start + per_page < len(clients):
        nav.append(('➡️', _p(cmd='chart_clients_page', page=page + 1)))
    if nav:
        kb.add_line()
        for label, pl in nav:
            kb.add_button(label, color=VkKeyboardColor.SECONDARY, payload=pl)
    kb.add_line()
    kb.add_button('⬅️ К графикам', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='charts_menu'))
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


def cancel_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    kb.add_button('❌ Отмена', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='cancel'))
    return kb.get_keyboard()


def date_choice_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('📅 Сегодня', color=VkKeyboardColor.PRIMARY, payload=_p(cmd='pick_date', date='today'))
    kb.add_button('📅 Завтра', color=VkKeyboardColor.PRIMARY, payload=_p(cmd='pick_date', date='tomorrow'))
    kb.add_line()
    kb.add_button('📅 Другая дата', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='pick_date', date='custom'))
    kb.add_line()
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    kb.add_button('❌ Отмена', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='cancel'))
    return kb.get_keyboard()


def clients_menu(clients, action='add', sid=None, page=0, per_page=4):
    kb = VkKeyboard(inline=True)
    start = page * per_page
    chunk = clients[start:start + per_page]
    for i, c in enumerate(chunk):
        if action == 'view':
            p = {'cmd': 'open_client', 'cid': c['id']}
        else:
            p = {'cmd': 'pick_client', 'action': action, 'cid': c['id']}
            if sid is not None: p['sid'] = sid
        kb.add_button(c['name'][:40], color=VkKeyboardColor.SECONDARY,
                      payload=json.dumps(p, ensure_ascii=False))
        kb.add_line()
    nav = []
    if page > 0:
        p = {'cmd': 'clients_page', 'action': action, 'page': page - 1}
        if sid is not None: p['sid'] = sid
        nav.append(('⬅️ Назад', json.dumps(p, ensure_ascii=False)))
    if start + per_page < len(clients):
        p = {'cmd': 'clients_page', 'action': action, 'page': page + 1}
        if sid is not None: p['sid'] = sid
        nav.append(('➡️ Далее', json.dumps(p, ensure_ascii=False)))
    if nav:
        for label, pl in nav:
            kb.add_button(label, color=VkKeyboardColor.SECONDARY, payload=pl)
        kb.add_line()
    if action in ('add', 'edit_client'):
        p = {'cmd': 'new_client', 'action': action}
        if sid is not None: p['sid'] = sid
        kb.add_button('➕ Новый клиент', color=VkKeyboardColor.POSITIVE,
                      payload=json.dumps(p, ensure_ascii=False))
    elif action == 'view':
        kb.add_button('📊 Сводка по всем', color=VkKeyboardColor.PRIMARY,
                      payload=_p(cmd='clients_summary'))
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    kb.add_button('❌ Отмена', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='cancel'))
    return kb.get_keyboard()


def client_card_menu(cid):
    kb = VkKeyboard(inline=True)
    kb.add_button('📜 Все отгрузки', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='client_history', cid=cid))
    kb.add_button('📈 График', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='client_chart', cid=cid))
    kb.add_line()
    kb.add_button('⬅️ К списку', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='clients_page', action='view', page=0))
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


def tonnage_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('19 т', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='pick_tonnage', t=19))
    kb.add_button('✏️ Другое', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='pick_tonnage', t='custom'))
    kb.add_line()
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    kb.add_button('❌ Отмена', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='cancel'))
    return kb.get_keyboard()


def confirm_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('✅ Сохранить', color=VkKeyboardColor.POSITIVE, payload=_p(cmd='save_shipment'))
    kb.add_button('💬 Коммент.', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='add_comment'))
    kb.add_line()
    kb.add_button('✏️ Заново', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='restart_add'))
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    kb.add_line()
    kb.add_button('❌ Отмена', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='cancel'))
    return kb.get_keyboard()


def after_save_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('➕ Ещё погрузку', color=VkKeyboardColor.POSITIVE, payload=_p(cmd='add_again'))
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


def stats_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('Сегодня', color=VkKeyboardColor.PRIMARY, payload=_p(cmd='stats', period='today'))
    kb.add_button('Вчера', color=VkKeyboardColor.PRIMARY, payload=_p(cmd='stats', period='yesterday'))
    kb.add_line()
    kb.add_button('Эта неделя', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='stats', period='week'))
    kb.add_button('Этот месяц', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='stats', period='month'))
    kb.add_line()
    kb.add_button('Этот год', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='stats', period='year'))
    kb.add_button('📅 Произвольно', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='stats', period='custom'))
    kb.add_line()
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    kb.add_button('❌ Отмена', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='cancel'))
    return kb.get_keyboard()


def shipment_item_menu(sid, d_iso=None, allow_delete=True):
    kb = VkKeyboard(inline=True)
    kb.add_button('✏️ Изменить', color=VkKeyboardColor.PRIMARY, payload=_p(cmd='edit_shipment', sid=sid))
    if allow_delete:
        kb.add_button('🗑 Удалить', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='del_shipment', sid=sid))
    kb.add_line()
    if d_iso:
        kb.add_button('⬅️ Назад к дню', color=VkKeyboardColor.SECONDARY,
                      payload=_p(cmd='back_to_day', date=d_iso))
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


def edit_menu(sid):
    kb = VkKeyboard(inline=True)
    kb.add_button('Клиент', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='edit_field', sid=sid, field='client'))
    kb.add_button('Тоннаж', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='edit_field', sid=sid, field='tonnage'))
    kb.add_line()
    kb.add_button('Дата', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='edit_field', sid=sid, field='date'))
    kb.add_button('Коммент.', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='edit_field', sid=sid, field='comment'))
    kb.add_line()
    kb.add_button('✅ Готово', color=VkKeyboardColor.POSITIVE, payload=_p(cmd='to_menu'))
    kb.add_button('⬅️ К карточке', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='open_shipment', sid=sid))
    return kb.get_keyboard()


def day_actions(shipments, d_iso):
    kb = VkKeyboard(inline=True)
    shipments = shipments[:20]
    for i, s in enumerate(shipments):
        kb.add_button(f'#{i+1}', color=VkKeyboardColor.SECONDARY,
                      payload=_p(cmd='open_shipment', sid=s['id']))
        if i % 5 == 4 and i != len(shipments) - 1:
            kb.add_line()
    if shipments: kb.add_line()
    kb.add_button('🖼 Картинкой', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='day_image', date=d_iso))
    kb.add_line()
    kb.add_button('➕ Добавить', color=VkKeyboardColor.POSITIVE,
                  payload=_p(cmd='add_again_for', date=d_iso))
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


def admin_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('✍️ Импорт текстом', color=VkKeyboardColor.POSITIVE,
                  payload=_p(cmd='import_text'))
    kb.add_button('📥 Импорт Excel (1С)', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='import_csv'))
    kb.add_line()
    kb.add_button('↩️ Откатить базу', color=VkKeyboardColor.NEGATIVE,
                  payload=_p(cmd='rollback_menu'))
    kb.add_button('🗑 Очистить период', color=VkKeyboardColor.NEGATIVE,
                  payload=_p(cmd='clear_period'))
    kb.add_line()
    kb.add_button('💾 Скачать базу', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='download_db'))
    kb.add_button('📤 CSV (всё)', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='export_csv_all'))
    kb.add_line()
    kb.add_button('📅 Произвольно', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='stats', period='custom'))
    kb.add_button('⚖️ Сравнить недели', color=VkKeyboardColor.POSITIVE,
                  payload=_p(cmd='week_compare'))
    kb.add_line()
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


def rollback_menu(backups, page=0, per_page=6):
    kb = VkKeyboard(inline=True)
    start = page * per_page
    chunk = backups[start:start + per_page]
    for b in chunk:
        name = b['name']
        m = re.match(r'shipments_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})_(.+)\.db', name)
        if m:
            label = f"{m.group(1)} {m.group(2).replace('-',':')} {m.group(3)[:8]}"
        else:
            label = name[-30:]
        kb.add_button(label, color=VkKeyboardColor.SECONDARY,
                      payload=_p(cmd='restore_backup', name=name))
        kb.add_line()
    nav = []
    if page > 0:
        nav.append(('⬅️', _p(cmd='rollback_page', page=page - 1)))
    if start + per_page < len(backups):
        nav.append(('➡️', _p(cmd='rollback_page', page=page + 1)))
    if nav:
        for label, pl in nav:
            kb.add_button(label, color=VkKeyboardColor.SECONDARY, payload=pl)
        kb.add_line()
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    kb.add_button('❌ Отмена', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='cancel'))
    return kb.get_keyboard()


def clear_period_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('Сегодня', color=VkKeyboardColor.NEGATIVE,
                  payload=_p(cmd='clear_period_confirm', period='today'))
    kb.add_button('Вчера', color=VkKeyboardColor.NEGATIVE,
                  payload=_p(cmd='clear_period_confirm', period='yesterday'))
    kb.add_line()
    kb.add_button('Эта неделя', color=VkKeyboardColor.NEGATIVE,
                  payload=_p(cmd='clear_period_confirm', period='week'))
    kb.add_button('Этот месяц', color=VkKeyboardColor.NEGATIVE,
                  payload=_p(cmd='clear_period_confirm', period='month'))
    kb.add_line()
    kb.add_button('Этот год', color=VkKeyboardColor.NEGATIVE,
                  payload=_p(cmd='clear_period_confirm', period='year'))
    kb.add_button('📅 Произвольно', color=VkKeyboardColor.NEGATIVE,
                  payload=_p(cmd='clear_period_confirm', period='custom'))
    kb.add_line()
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    kb.add_button('❌ Отмена', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='cancel'))
    return kb.get_keyboard()


# ════════════════════════════ ИНИЦИАЛИЗАЦИЯ VK ════════════════════════════

init_db()

vk_session = None
vk = None
longpoll = None

if TOKEN:
    try:
        vk_session = vk_api.VkApi(token=TOKEN)
        vk = vk_session.get_api()
        longpoll = VkLongPoll(vk_session)
    except Exception as e:
        print(f"❌ Ошибка инициализации VK: {e}")


# ════════════════════════════ УТИЛИТЫ ════════════════════════════

def is_admin(user_id):
    if not ADMIN_IDS: return True
    return user_id in ADMIN_IDS


def send(user_id, text, keyboard=None, attachment=None):
    if vk is None:
        print("[send] VK не инициализирован"); return
    if len(text) > 4000:
        text = text[:3990] + "\n…(обрезано)"
    params = {'user_id': user_id, 'message': text, 'random_id': get_random_id()}
    if keyboard: params['keyboard'] = keyboard
    if attachment: params['attachment'] = attachment
    try: vk.messages.send(**params)
    except Exception: traceback.print_exc()


def fmt_num(x):
    if x == int(x): return str(int(x))
    return f"{x:.2f}".rstrip('0').rstrip('.')


def parse_number(text):
    text = text.strip().replace(',', '.').replace(' ', '')
    try: return float(text)
    except ValueError: return None


def parse_date(text):
    text = text.strip()
    today = tz_today()
    for fmt in ('%d.%m.%Y', '%d.%m.%y', '%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
        try: return datetime.strptime(text, fmt).date()
        except ValueError: pass
    for fmt in ('%d.%m', '%d/%m'):
        try:
            d = datetime.strptime(text, fmt).date()
            return d.replace(year=today.year)
        except ValueError: pass
    low = text.lower()
    if low in ('сегодня', 'today', 'с'): return today
    if low in ('завтра', 'tomorrow', 'з'): return today + timedelta(days=1)
    if low == 'послезавтра': return today + timedelta(days=2)
    return None


def parse_month_or_date(text):
    text = str(text).strip()
    for fmt in ('%m.%Y', '%m/%Y', '%Y-%m'):
        try: return datetime.strptime(text, fmt).date().replace(day=1)
        except ValueError: pass
    for fmt in ('%d.%m.%Y', '%d.%m.%y', '%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
        try: return datetime.strptime(text, fmt).date()
        except ValueError: pass
    months = {'янв': 1, 'фев': 2, 'мар': 3, 'апр': 4, 'май': 5, 'мая': 5,
              'июн': 6, 'июл': 7, 'авг': 8, 'сен': 9, 'окт': 10, 'ноя': 11, 'дек': 12}
    low = text.lower()
    for name, m in months.items():
        if name in low:
            ym = re.search(r'(20\d{2})', low)
            if ym: return date(int(ym.group(1)), m, 1)
    return None


def ru_date(d):
    if isinstance(d, str): d = datetime.strptime(d, '%Y-%m-%d').date()
    return d.strftime('%d.%m.%Y')


# ════════════════════════════ ФОРМАТИРОВАНИЕ ════════════════════════════

def format_shipment_card(s):
    if not s: return "❌ Отгрузка не найдена."
    d = ru_date(s['shipment_date'])
    lines = [f"#{s['id']} от {d}",
             f"👤 Клиент: {s['client_name']}",
             f"⚖️ Тоннаж: {fmt_num(s['tonnage'])} т",
             f"🚛 Фур: {fmt_num(s['trucks'])} ({fmt_num(s['tonnage'])} ÷ {fmt_num(TRUCK_CAPACITY)})"]
    if s.get('comment'):
        lines.append(f"💬 Комментарий: {s['comment']}")
    return "\n".join(lines)


def format_day_shipments(d, shipments, title=None):
    if title is None: title = f"📋 Погрузки на {ru_date(d)}:"
    lines = [title, ""]
    if not shipments:
        lines.append("— нет —"); return "\n".join(lines)
    total_t = 0.0
    for i, s in enumerate(shipments, 1):
        lines.append(f"{i}. {s['client_name']} — {fmt_num(s['tonnage'])} т "
                     f"({fmt_num(s['trucks'])} фур)")
        total_t += s['tonnage']
    total_trucks = round(total_t / TRUCK_CAPACITY, 2)
    lines += ["", f"Итого: {fmt_num(total_t)} т, {fmt_num(total_trucks)} фур"]
    return "\n".join(lines)


def format_stats(title, stats):
    lines = [f"📊 {title}", ""]
    if stats['count'] == 0:
        lines.append("Нет отгрузок за период."); return "\n".join(lines)
    lines.append(f"Всего отгрузок: {stats['count']}")
    lines.append(f"Общий тоннаж: {fmt_num(stats['total_tonnage'])} т")
    lines.append(f"Фур: {fmt_num(stats['total_trucks'])} "
                 f"({fmt_num(stats['total_tonnage'])} ÷ {fmt_num(TRUCK_CAPACITY)})")
    lines.append("")
    lines.append("👥 По клиентам:")
    for name, c in sorted(stats['by_client'].items(),
                          key=lambda x: -x[1]['tonnage'])[:30]:
        lines.append(f"• {name}: {fmt_num(c['tonnage'])} т / "
                     f"{fmt_num(c['trucks'])} фур / {c['count']} отгр.")
    lines.append("")
    lines.append("📅 По дням:")
    for d in sorted(stats['by_day'].keys(), reverse=True)[:30]:
        c = stats['by_day'][d]
        lines.append(f"• {ru_date(d)}: {fmt_num(c['tonnage'])} т / "
                     f"{fmt_num(c['trucks'])} фур / {c['count']} отгр.")
    return "\n".join(lines)


# ════════════════════════════ МЕНЮ ════════════════════════════

def show_main_menu(user_id):
    u = next((x for x in get_all_users() if x['user_id'] == user_id), None)
    morning_on = bool(u and u.get('morning_report'))
    send(user_id, "Главное меню:", main_menu(is_admin(user_id), morning_on))


def show_help(user_id):
    text = (
        "🤖 Бот учёта отгрузок склада\n\n"
        "• ➕ Добавлять погрузки\n"
        "• 📋 Показывать погрузки на день\n"
        "• 🖼 Картинка дня — сводка в формате А4\n"
        "• 📊 Статистика за любой период\n"
        "• 📈 Графики — 9 видов визуализации (А4)\n"
        "• 📁 Справочник клиентов\n"
        "• 🔔 Утренняя сводка\n"
        "• 📥 Импорт Excel из 1С\n"
        "• ↩️ Откат к резервной копии\n"
        "• 🗑 Очистка периода\n\n"
        f"🚛 Фур = тоннаж ÷ {fmt_num(TRUCK_CAPACITY)}\n"
        f"🕐 Часовой пояс: Уфа (UTC+{TIMEZONE_OFFSET_HOURS})\n\n"
        "💡 Прервать любое действие — кнопка меню внизу или /отмена."
    )
    send(user_id, text, main_menu(is_admin(user_id)))


# ════════════════════════════ ДОБАВЛЕНИЕ ════════════════════════════

def start_add(user_id):
    clear_state(user_id)
    send(user_id, "📅 На какую дату добавить погрузку?", date_choice_menu())


def start_add_with_date(user_id, d_iso):
    clear_state(user_id)
    set_state(user_id, pending={'date': d_iso})
    show_client_picker(user_id, action='add')


def show_client_picker(user_id, action='add', sid=None, page=0):
    clients = get_clients()
    if not clients:
        update_state(user_id, awaiting='new_client_name', action=action, sid=sid)
        send(user_id, "📁 Справочник пуст. Введите имя первого клиента:", cancel_menu())
        return
    send(user_id, "👤 Выберите клиента:",
         clients_menu(clients, action=action, sid=sid, page=page))


def show_confirm(user_id):
    p = get_state(user_id).get('pending', {})
    if not (p.get('date') and p.get('client') and p.get('tonnage')):
        send(user_id, "❌ Недостаточно данных.")
        clear_state(user_id); show_main_menu(user_id); return
    trucks = calc_trucks(p['tonnage'])
    text = ("Проверьте данные:\n\n"
            f"📅 Дата: {ru_date(p['date'])}\n"
            f"👤 Клиент: {p['client']}\n"
            f"⚖️ Тоннаж: {fmt_num(p['tonnage'])} т\n"
            f"🚛 Фур: {fmt_num(trucks)} ({fmt_num(p['tonnage'])} ÷ {fmt_num(TRUCK_CAPACITY)})\n")
    if p.get('comment'):
        text += f"💬 Комментарий: {p['comment']}\n"
    send(user_id, text, confirm_menu())


def on_save_shipment(user_id):
    p = get_state(user_id).get('pending', {})
    if not (p.get('date') and p.get('client') and p.get('tonnage')):
        send(user_id, "❌ Недостаточно данных.")
        clear_state(user_id); show_main_menu(user_id); return
    sid = add_shipment(p['client'], p['tonnage'], p['date'], user_id,
                       comment=p.get('comment', ''))
    s = get_shipment(sid)
    d_iso = p['date']
    clear_state(user_id)
    set_state(user_id, pending={'date': d_iso})
    send(user_id, f"✅ Погрузка добавлена!\n\n{format_shipment_card(s)}",
         after_save_menu())


# ════════════════════════════ ПРОСМОТР ДНЯ ════════════════════════════

def show_day_shipments(user_id, d):
    shipments = get_shipments_by_date(d)
    text = format_day_shipments(d, shipments)
    send(user_id, text, day_actions(shipments, d.isoformat()))


def on_open_shipment(user_id, payload):
    sid = payload.get('sid')
    s = get_shipment(sid)
    if not s:
        send(user_id, "❌ Отгрузка не найдена.", main_menu(is_admin(user_id))); return
    send(user_id, format_shipment_card(s),
         shipment_item_menu(sid, s['shipment_date'], allow_delete=is_admin(user_id)))


# ════════════════════════════ СТАТИСТИКА ════════════════════════════

def show_stats(user_id, period=None, start=None, end=None):
    today = tz_today()
    if period == 'today':
        s, e = today, today; title = f"Сегодня ({ru_date(today)})"
    elif period == 'yesterday':
        y = today - timedelta(days=1); s, e = y, y; title = f"Вчера ({ru_date(y)})"
    elif period == 'week':
        s = today - timedelta(days=today.weekday()); e = today
        title = f"Эта неделя ({ru_date(s)}–{ru_date(e)})"
    elif period == 'last_week':
        s = today - timedelta(days=today.weekday() + 7); e = s + timedelta(days=6)
        title = f"Прошлая неделя ({ru_date(s)}–{ru_date(e)})"
    elif period == 'month':
        s = today.replace(day=1); e = today
        title = f"Этот месяц ({ru_date(s)}–{ru_date(e)})"
    elif period == 'last_month':
        first = today.replace(day=1); e = first - timedelta(days=1)
        s = e.replace(day=1); title = f"Прошлый месяц ({ru_date(s)}–{ru_date(e)})"
    elif period == 'year':
        s = today.replace(month=1, day=1); e = today
        title = f"Этот год ({ru_date(s)}–{ru_date(e)})"
    elif period == 'custom':
        try:
            s = datetime.strptime(start, '%Y-%m-%d').date()
            e = datetime.strptime(end, '%Y-%m-%d').date()
        except Exception:
            send(user_id, "Ошибка в датах."); return
        title = f"Период {ru_date(s)}–{ru_date(e)}"
    else:
        send(user_id, "Выберите период:", stats_menu()); return
    stats = stats_for_period(s, e)
    send(user_id, format_stats(title, stats), main_menu(is_admin(user_id)))


def _chart_period_bounds(period):
    today = tz_today()
    if period == 'week':
        s = today - timedelta(days=today.weekday()); e = today
        return s, e, f"эта неделя ({ru_date(s)}–{ru_date(e)})"
    if period == 'month':
        s = today.replace(day=1); e = today
        return s, e, f"этот месяц ({ru_date(s)}–{ru_date(e)})"
    if period == 'last_month':
        first = today.replace(day=1); e = first - timedelta(days=1)
        s = e.replace(day=1)
        return s, e, f"прошлый месяц ({ru_date(s)}–{ru_date(e)})"
    if period == 'year':
        s = today.replace(month=1, day=1); e = today
        return s, e, f"этот год ({ru_date(s)}–{ru_date(e)})"
    return None, None, None


# ════════════════════════════ КАРТИНКА ДНЯ (А4) ════════════════════════════

def make_day_image_png(d, shipments):
    fig = plt.figure(figsize=A4_PORTRAIT, dpi=110)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis('off')

    date_str = ru_date(d)
    total_t = sum(s['tonnage'] for s in shipments)
    total_trucks = round(total_t / TRUCK_CAPACITY, 2)
    cnt = len(shipments)

    ax.add_patch(Rectangle((0, 0.94), 1, 0.06, facecolor=COLOR_DARK, edgecolor='none'))
    ax.text(0.5, 0.97, 'ВЕЛЕС · Отгрузки склада',
            ha='center', va='center', fontsize=14, color='white', fontweight='bold')

    ax.text(0.5, 0.905, f'Погрузки на {date_str}',
            ha='center', va='center', fontsize=22, color=COLOR_DARK, fontweight='bold')

    summary_y = 0.875
    ax.add_patch(Rectangle((0.05, summary_y - 0.018), 0.9, 0.036,
                            facecolor=COLOR_LIGHT, edgecolor='none'))
    ax.text(0.5, summary_y,
            f'Всего: {fmt_num(total_t)} т    ·    {fmt_num(total_trucks)} фур    ·    {cnt} отгрузок',
            ha='center', va='center', fontsize=12, color=COLOR_ACCENT, fontweight='bold')

    if not shipments:
        ax.text(0.5, 0.5, '— нет погрузок —',
                ha='center', va='center', fontsize=24, color='#aaa')
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=110, facecolor='white')
        plt.close(fig); buf.seek(0)
        return buf.read()

    area_top = 0.845; area_bottom = 0.105
    area_h = area_top - area_bottom
    pad_left = 0.05; pad_right = 0.05
    content_w = 1 - pad_left - pad_right
    right_edge = 1 - pad_right

    row_count = len(shipments)
    row_h = min(0.055, area_h / row_count)
    font_main = max(9, min(14, int(row_h * 260)))
    font_weight = max(9, min(13, int(row_h * 240)))
    font_cmt = max(7, min(10, int(row_h * 200)))

    y_cursor = area_top
    for i, s in enumerate(shipments):
        row_top = y_cursor
        row_bottom = y_cursor - row_h
        y_center = (row_top + row_bottom) / 2

        if i % 2 == 0:
            ax.add_patch(Rectangle((pad_left, row_bottom), content_w, row_h,
                                    facecolor='#f0f4f9', edgecolor='none'))
        ax.plot([pad_left, right_edge], [row_bottom, row_bottom],
                color=COLOR_GRID, linewidth=0.8)

        ax.text(pad_left + 0.005, y_center, f'{i+1}.',
                ha='left', va='center', fontsize=font_main, color='#9aa7b5')

        name = s['client_name']
        max_chars = 44
        if len(name) > max_chars: name = name[:max_chars - 1] + '…'
        ax.text(pad_left + 0.04, y_center, name,
                ha='left', va='center', fontsize=font_main, color=COLOR_DARK)

        w_str = f"{fmt_num(s['tonnage'])} т · {fmt_num(s['trucks'])} фур"
        ax.text(right_edge - 0.005, y_center, w_str,
                ha='right', va='center', fontsize=font_weight,
                fontweight='bold', color=COLOR_ACCENT)

        if s.get('comment') and row_h > 0.035:
            cmt = s['comment']
            if len(cmt) > 60: cmt = cmt[:59] + '…'
            ax.text(pad_left + 0.04, row_bottom + row_h * 0.22, f'— {cmt}',
                    ha='left', va='center', fontsize=font_cmt,
                    color='#8b96a4', style='italic')

        y_cursor = row_bottom

    ax.add_patch(Rectangle((0, 0.03), 1, 0.06, facecolor=COLOR_ACCENT, edgecolor='none'))
    ax.text(0.5, 0.06,
            f'ИТОГО:  {fmt_num(total_t)} т   ·   {fmt_num(total_trucks)} фур   ·   {cnt} отгрузок',
            ha='center', va='center', fontsize=15, color='white', fontweight='bold')

    ax.text(0.5, 0.012, f'Сформировано ботом · {tz_now().strftime("%d.%m.%Y %H:%M")}',
            ha='center', va='center', fontsize=8, color='#b0b8c2')

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=110, facecolor='white',
                bbox_inches=None, pad_inches=0)
    plt.close(fig); buf.seek(0)
    return buf.read()


def show_day_image(user_id, d):
    shipments = get_shipments_by_date(d)
    png = make_day_image_png(d, shipments)
    try:
        attach = upload_photo_bytes(user_id, png)
        total_t = sum(s['tonnage'] for s in shipments)
        total_trucks = round(total_t / TRUCK_CAPACITY, 2)
        caption = (f"🖼 Погрузки на {ru_date(d)} (А4)\n\n"
                   f"Итого: {fmt_num(total_t)} т, {fmt_num(total_trucks)} фур, "
                   f"{len(shipments)} отгрузок")
        send(user_id, caption, main_menu(is_admin(user_id)), attachment=attach)
    except Exception as e:
        traceback.print_exc()
        send(user_id, f"❌ Не удалось отправить картинку: {e}",
             main_menu(is_admin(user_id)))


# ════════════════════════════ ГРАФИКИ ════════════════════════════

def _save_fig_to_png(fig):
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, facecolor='white', bbox_inches=None)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def make_chart_by_day_png(start, end, title):
    shipments = get_shipments_by_period(start, end)
    if not shipments: return None
    by_day = defaultdict(float)
    d = start
    while d <= end:
        by_day[d] = 0.0; d += timedelta(days=1)
    for s in shipments:
        day = datetime.strptime(s['shipment_date'], '%Y-%m-%d').date()
        by_day[day] += s['tonnage']
    dates = sorted(by_day.keys())
    values = [by_day[d] for d in dates]

    fig = plt.figure(figsize=A4_LANDSCAPE, dpi=100)
    ax = fig.add_axes([0.08, 0.15, 0.86, 0.72])

    bars = ax.bar(dates, values, color=COLOR_ACCENT, edgecolor='#2c4a70', width=0.7)
    ax.set_title(f'Отгрузки — {title}', fontsize=16, fontweight='bold',
                 color=COLOR_DARK, pad=15)
    ax.set_ylabel('Тонны', fontsize=12)
    ax.grid(axis='y', linestyle='--', alpha=0.5)
    ax.set_axisbelow(True)

    for bar, v in zip(bars, values):
        if v > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                    f'{fmt_num(v)}', ha='center', va='bottom', fontsize=9)

    if len(dates) > 10:
        ax.xaxis.set_major_formatter(DateFormatter('%d.%m'))
        plt.setp(ax.get_xticklabels(), rotation=60, ha='right')
    else:
        ax.set_xticks(dates)
        ax.set_xticklabels([d.strftime('%d.%m') for d in dates])

    total = sum(values)
    fig.text(0.5, 0.04,
             f'Итого: {fmt_num(total)} т  ·  {fmt_num(round(total / TRUCK_CAPACITY, 2))} фур',
             ha='center', fontsize=12, color=COLOR_ACCENT, fontweight='bold')
    return _save_fig_to_png(fig)


def make_top_clients_png(start, end, title):
    stats = stats_for_period(start, end)
    if not stats['by_client']: return None
    items = sorted(stats['by_client'].items(),
                   key=lambda x: -x[1]['tonnage'])[:15]
    items = items[::-1]
    names = [n[:34] for n, _ in items]
    values = [c['tonnage'] for _, c in items]
    trucks = [c['trucks'] for _, c in items]

    fig = plt.figure(figsize=A4_PORTRAIT, dpi=100)
    ax = fig.add_axes([0.28, 0.08, 0.65, 0.83])

    bars = ax.barh(names, values, color=COLOR_ACCENT, edgecolor='#2c4a70')
    for bar, v, tr in zip(bars, values, trucks):
        ax.text(bar.get_width() * 1.01,
                bar.get_y() + bar.get_height() / 2,
                f'{fmt_num(v)} т · {fmt_num(tr)} фур',
                va='center', fontsize=9, color=COLOR_DARK)

    ax.set_xlabel('Тонны', fontsize=12)
    ax.set_title(f'🥇 Топ клиентов — {title}',
                 fontsize=15, fontweight='bold', color=COLOR_DARK, pad=15)
    ax.grid(axis='x', linestyle='--', alpha=0.4)
    ax.set_axisbelow(True)

    total = sum(values)
    fig.text(0.5, 0.025,
             f'Итого (топ-{len(items)}): {fmt_num(total)} т  ·  '
             f'{fmt_num(round(total / TRUCK_CAPACITY, 2))} фур',
             ha='center', fontsize=11, color=COLOR_ACCENT, fontweight='bold')
    return _save_fig_to_png(fig)


def make_top_trucks_png(start, end, title):
    stats = stats_for_period(start, end)
    if not stats['by_client']: return None
    items = sorted(stats['by_client'].items(),
                   key=lambda x: -x[1]['trucks'])[:15]
    items = items[::-1]
    names = [n[:34] for n, _ in items]
    trucks = [c['trucks'] for _, c in items]
    tons = [c['tonnage'] for _, c in items]

    fig = plt.figure(figsize=A4_PORTRAIT, dpi=100)
    ax = fig.add_axes([0.28, 0.08, 0.65, 0.83])

    bars = ax.barh(names, trucks, color='#7a9cc6', edgecolor='#2c4a70')
    for bar, tr, v in zip(bars, trucks, tons):
        ax.text(bar.get_width() * 1.01,
                bar.get_y() + bar.get_height() / 2,
                f'{fmt_num(tr)} фур · {fmt_num(v)} т',
                va='center', fontsize=9, color=COLOR_DARK)

    ax.set_xlabel('Фуры (расчёт: тоннаж ÷ 19)', fontsize=11)
    ax.set_title(f'🚛 Топ по фурам — {title}',
                 fontsize=15, fontweight='bold', color=COLOR_DARK, pad=15)
    ax.grid(axis='x', linestyle='--', alpha=0.4)
    ax.set_axisbelow(True)

    total = sum(trucks)
    fig.text(0.5, 0.025,
             f'Всего (топ-{len(items)}): {fmt_num(round(total, 2))} фур',
             ha='center', fontsize=11, color=COLOR_ACCENT, fontweight='bold')
    return _save_fig_to_png(fig)


def make_pie_clients_png(start, end, title):
    stats = stats_for_period(start, end)
    if not stats['by_client']: return None
    items = sorted(stats['by_client'].items(), key=lambda x: -x[1]['tonnage'])
    top = items[:8]
    other_t = sum(c['tonnage'] for _, c in items[8:])
    labels = [n[:24] for n, _ in top]
    values = [c['tonnage'] for _, c in top]
    if other_t > 0:
        labels.append('Прочие')
        values.append(other_t)

    fig = plt.figure(figsize=A4_LANDSCAPE, dpi=100)
    ax = fig.add_axes([0.05, 0.08, 0.9, 0.82])

    colors = plt.cm.tab10.colors[:len(values)]
    wedges, texts, autotexts = ax.pie(
        values, labels=labels, autopct='%1.1f%%',
        colors=colors, startangle=90, textprops={'fontsize': 10})
    for a in autotexts:
        a.set_color('white'); a.set_fontweight('bold')

    total = sum(values)
    ax.set_title(f'🍰 Доли клиентов — {title}\n'
                 f'Всего: {fmt_num(total)} т / {fmt_num(round(total / TRUCK_CAPACITY, 2))} фур',
                 fontsize=14, fontweight='bold', color=COLOR_DARK, pad=15)
    return _save_fig_to_png(fig)


def make_month_heatmap_png(year, month, title):
    start = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = date(year, month, last_day)
    stats = stats_for_period(start, end)
    by_day = {int(d.split('-')[2]): c['tonnage']
              for d, c in stats['by_day'].items()}

    cal = calendar.Calendar(firstweekday=0)
    weeks = cal.monthdayscalendar(year, month)
    max_t = max(by_day.values()) if by_day else 1.0
    n_weeks = len(weeks)

    fig = plt.figure(figsize=A4_PORTRAIT, dpi=100)
    ax = fig.add_axes([0.08, 0.14, 0.84, 0.72])

    days_header = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    for i, h in enumerate(days_header):
        ax.text(i + 0.5, n_weeks + 0.35, h, ha='center',
                fontsize=12, fontweight='bold', color=COLOR_DARK)

    cmap = plt.cm.Blues
    for w, week in enumerate(weeks):
        for d_idx, day in enumerate(week):
            y = n_weeks - 1 - w
            if day == 0:
                ax.add_patch(Rectangle((d_idx, y), 1, 1,
                                       facecolor='#f5f5f5', edgecolor='#ddd'))
                continue
            t = by_day.get(day, 0.0)
            ratio = (t / max_t) if max_t > 0 else 0
            color = cmap(0.15 + 0.75 * ratio) if t > 0 else '#fafafa'
            ax.add_patch(Rectangle((d_idx, y), 1, 1,
                                   facecolor=color, edgecolor='#ddd'))
            ax.text(d_idx + 0.5, y + 0.7, str(day),
                    ha='center', va='center', fontsize=12,
                    color='white' if ratio > 0.5 else COLOR_DARK)
            if t > 0:
                ax.text(d_idx + 0.5, y + 0.28, f'{fmt_num(t)}т',
                        ha='center', va='center', fontsize=9,
                        color='white' if ratio > 0.5 else COLOR_ACCENT)

    ax.set_xlim(0, 7); ax.set_ylim(-0.2, n_weeks + 0.9)
    ax.set_aspect('equal'); ax.axis('off')

    ax.set_title(f'📅 Календарь — {title}',
                 fontsize=15, fontweight='bold', color=COLOR_DARK, pad=15)

    total = sum(by_day.values())
    fig.text(0.5, 0.07,
             f'Итого за месяц: {fmt_num(total)} т  ·  '
             f'{fmt_num(round(total / TRUCK_CAPACITY, 2))} фур  ·  '
             f'{stats["count"]} отгрузок',
             ha='center', fontsize=12, color=COLOR_ACCENT, fontweight='bold')
    return _save_fig_to_png(fig)


def make_week_compare_png():
    cmp = week_comparison()
    this_by_day = {d: c['tonnage'] for d, c in cmp['this']['by_day'].items()}
    last_by_day = {d: c['tonnage'] for d, c in cmp['last']['by_day'].items()}

    this_week = [0.0] * 7
    last_week = [0.0] * 7
    for d_iso, t in this_by_day.items():
        d = datetime.strptime(d_iso, '%Y-%m-%d').date()
        this_week[d.weekday()] += t
    for d_iso, t in last_by_day.items():
        d = datetime.strptime(d_iso, '%Y-%m-%d').date()
        last_week[d.weekday()] += t

    labels = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    x = list(range(7)); w = 0.38

    fig = plt.figure(figsize=A4_LANDSCAPE, dpi=100)
    ax = fig.add_axes([0.08, 0.18, 0.86, 0.68])

    b1 = ax.bar([i - w/2 for i in x], last_week, w,
                label='Прошлая неделя', color='#b8c8db', edgecolor='#8a9cb5')
    b2 = ax.bar([i + w/2 for i in x], this_week, w,
                label='Эта неделя', color=COLOR_ACCENT, edgecolor='#2c4a70')

    for bars in (b1, b2):
        for bar in bars:
            h = bar.get_height()
            if h > 0:
                ax.text(bar.get_x() + bar.get_width() / 2, h,
                        f'{fmt_num(h)}', ha='center', va='bottom', fontsize=9)

    ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=12)
    ax.set_ylabel('Тонны', fontsize=12)
    ax.grid(axis='y', linestyle='--', alpha=0.4)
    ax.set_axisbelow(True)

    t1 = cmp['this']['total_tonnage']
    t0 = cmp['last']['total_tonnage']
    diff = t1 - t0
    sign = '+' if diff >= 0 else ''
    ax.set_title(f'📈 Сравнение недель', fontsize=15, fontweight='bold',
                 color=COLOR_DARK, pad=12)
    ax.legend(fontsize=11)

    fig.text(0.5, 0.06,
             f'Прошлая ({ru_date(cmp["last_range"][0])}–{ru_date(cmp["last_range"][1])}): '
             f'{fmt_num(t0)} т  ·  '
             f'Эта: {fmt_num(t1)} т  ·  '
             f'Разница: {sign}{fmt_num(diff)} т',
             ha='center', fontsize=11, color=COLOR_ACCENT, fontweight='bold')
    return _save_fig_to_png(fig)


def make_client_trend_png(client_name):
    shipments = get_shipments_by_client(client_name, limit=5000)
    if not shipments: return None
    by_date = defaultdict(float)
    for s in shipments:
        by_date[s['shipment_date']] += s['tonnage']
    dates = sorted(by_date.keys())
    d_objs = [datetime.strptime(d, '%Y-%m-%d').date() for d in dates]
    values = [by_date[d] for d in dates]

    fig = plt.figure(figsize=A4_LANDSCAPE, dpi=100)
    ax = fig.add_axes([0.08, 0.18, 0.86, 0.66])

    ax.plot(d_objs, values, marker='o', color=COLOR_ACCENT,
            linewidth=2, markersize=5)
    ax.fill_between(d_objs, values, alpha=0.15, color=COLOR_ACCENT)

    ax.set_title(f'📈 Тренд: {client_name[:60]}',
                 fontsize=14, fontweight='bold', color=COLOR_DARK, pad=12)
    ax.set_ylabel('Тонны', fontsize=11)
    ax.grid(True, linestyle='--', alpha=0.4)
    ax.set_axisbelow(True)

    total = sum(values)
    avg = total / len(values) if values else 0
    fig.text(0.5, 0.06,
             f'Всего: {fmt_num(total)} т  ·  {fmt_num(round(total / TRUCK_CAPACITY, 2))} фур  ·  '
             f'{len(values)} дней  ·  Среднее: {fmt_num(round(avg, 1))} т/день',
             ha='center', fontsize=11, color=COLOR_ACCENT, fontweight='bold')
    return _save_fig_to_png(fig)


def make_weekday_avg_png(start, end, title):
    stats = stats_for_period(start, end)
    if not stats['by_day']: return None
    totals = [0.0] * 7
    counts = [0] * 7
    for d_iso, c in stats['by_day'].items():
        d = datetime.strptime(d_iso, '%Y-%m-%d').date()
        totals[d.weekday()] += c['tonnage']
        counts[d.weekday()] += 1

    labels = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    x = list(range(7))

    fig = plt.figure(figsize=A4_LANDSCAPE, dpi=100)
    ax = fig.add_axes([0.08, 0.18, 0.86, 0.68])

    bars = ax.bar(x, totals, color=COLOR_ACCENT, edgecolor='#2c4a70', width=0.65)
    for bar, v in zip(bars, totals):
        if v > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                    f'{fmt_num(v)}т', ha='center', va='bottom', fontsize=10)

    ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=12)
    ax.set_ylabel('Тонны', fontsize=12)
    ax.set_title(f'📊 Отгрузки по дням недели — {title}',
                 fontsize=15, fontweight='bold', color=COLOR_DARK, pad=12)
    ax.grid(axis='y', linestyle='--', alpha=0.4)
    ax.set_axisbelow(True)

    total = sum(totals)
    fig.text(0.5, 0.06,
             f'Итого: {fmt_num(total)} т  ·  '
             f'{fmt_num(round(total / TRUCK_CAPACITY, 2))} фур',
             ha='center', fontsize=11, color=COLOR_ACCENT, fontweight='bold')
    return _save_fig_to_png(fig)


def make_weekend_pie_png(start, end, title):
    stats = stats_for_period(start, end)
    if not stats['by_day']: return None
    weekday_t = 0.0
    weekend_t = 0.0
    for d_iso, c in stats['by_day'].items():
        d = datetime.strptime(d_iso, '%Y-%m-%d').date()
        if d.weekday() >= 5:
            weekend_t += c['tonnage']
        else:
            weekday_t += c['tonnage']
    if weekday_t == 0 and weekend_t == 0: return None

    fig = plt.figure(figsize=A4_LANDSCAPE, dpi=100)
    ax = fig.add_axes([0.05, 0.08, 0.9, 0.82])

    values = [weekday_t, weekend_t]
    labels = ['Будни (Пн–Пт)', 'Выходные (Сб–Вс)']
    colors = [COLOR_ACCENT, '#d9a651']

    wedges, texts, autotexts = ax.pie(
        values, labels=labels, autopct='%1.1f%%',
        colors=colors, startangle=90,
        textprops={'fontsize': 13}, explode=(0.02, 0.05))
    for a in autotexts:
        a.set_color('white'); a.set_fontweight('bold'); a.set_fontsize(14)

    total = sum(values)
    ax.set_title(f'🥧 Будни vs выходные — {title}\n'
                 f'Всего: {fmt_num(total)} т',
                 fontsize=15, fontweight='bold', color=COLOR_DARK, pad=15)
    return _save_fig_to_png(fig)


# ════════════════════════════ ОТПРАВКА ГРАФИКОВ ════════════════════════════

def _send_chart(user_id, png, caption):
    if not png:
        send(user_id, "Нет данных за период.", main_menu(is_admin(user_id)))
        return
    try:
        attach = upload_photo_bytes(user_id, png)
        send(user_id, caption, main_menu(is_admin(user_id)), attachment=attach)
    except Exception as e:
        traceback.print_exc()
        send(user_id, f"❌ Не удалось отправить: {e}",
             main_menu(is_admin(user_id)))


def _chart_caption(chart, title):
    emoji_map = {
        'by_day': '📊', 'top_clients': '🥇', 'top_trucks': '🚛',
        'pie_clients': '🍰', 'calendar': '📅', 'week_compare': '📈',
        'client_trend': '📈', 'weekday_avg': '📊', 'weekend_pie': '🥧',
    }
    name_map = {
        'by_day': 'Отгрузки по дням', 'top_clients': 'Топ клиентов',
        'top_trucks': 'Топ по фурам', 'pie_clients': 'Доли клиентов',
        'calendar': 'Календарь', 'week_compare': 'Сравнение недель',
        'client_trend': 'Тренд клиента', 'weekday_avg': 'По дням недели',
        'weekend_pie': 'Будни/выходные',
    }
    return f"{emoji_map.get(chart,'📈')} {name_map.get(chart,'График')} — {title}"


def show_chart(user_id, chart, period):
    s, e, ptitle = _chart_period_bounds(period)
    if not s:
        send(user_id, "Не понял период.", main_menu(is_admin(user_id))); return

    if chart == 'by_day':
        png = make_chart_by_day_png(s, e, ptitle)
    elif chart == 'top_clients':
        png = make_top_clients_png(s, e, ptitle)
    elif chart == 'top_trucks':
        png = make_top_trucks_png(s, e, ptitle)
    elif chart == 'pie_clients':
        png = make_pie_clients_png(s, e, ptitle)
    elif chart == 'weekday_avg':
        png = make_weekday_avg_png(s, e, ptitle)
    elif chart == 'weekend_pie':
        png = make_weekend_pie_png(s, e, ptitle)
    else:
        send(user_id, "Неизвестный график.", main_menu(is_admin(user_id))); return
    _send_chart(user_id, png, _chart_caption(chart, ptitle))


def show_chart_custom(user_id, chart, start_iso, end_iso):
    s = datetime.strptime(start_iso, '%Y-%m-%d').date()
    e = datetime.strptime(end_iso, '%Y-%m-%d').date()
    ptitle = f"{ru_date(s)}–{ru_date(e)}"

    if chart == 'by_day':
        png = make_chart_by_day_png(s, e, ptitle)
    elif chart == 'top_clients':
        png = make_top_clients_png(s, e, ptitle)
    elif chart == 'top_trucks':
        png = make_top_trucks_png(s, e, ptitle)
    elif chart == 'pie_clients':
        png = make_pie_clients_png(s, e, ptitle)
    elif chart == 'weekday_avg':
        png = make_weekday_avg_png(s, e, ptitle)
    elif chart == 'weekend_pie':
        png = make_weekend_pie_png(s, e, ptitle)
    else:
        png = None
    _send_chart(user_id, png, _chart_caption(chart, ptitle))


def show_calendar_chart(user_id, month_key):
    today = tz_today()
    if month_key == 'this':
        y, m = today.year, today.month
    elif month_key == 'last':
        first = today.replace(day=1)
        prev = first - timedelta(days=1)
        y, m = prev.year, prev.month
    else:
        return
    png = make_month_heatmap_png(y, m, f"{calendar.month_name[m]} {y}")
    _send_chart(user_id, png, f"📅 Календарь — {calendar.month_name[m]} {y}")


def show_calendar_chart_custom(user_id, year, month):
    png = make_month_heatmap_png(year, month, f"{calendar.month_name[month]} {year}")
    _send_chart(user_id, png, f"📅 Календарь — {calendar.month_name[month]} {year}")


# ════════════════════════════ ЗАГРУЗКА В ВК (С 3 ПОПЫТКАМИ) ════════════════════════════

def upload_photo_bytes(user_id, photo_bytes):
    """
    Загружает фото в ВК через requests.
    Уменьшение до 800 px, quality=70, с 3 попытками при 5xx.
    """
    # 1. Конвертация и уменьшение
    jpg_bytes = photo_bytes
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(photo_bytes))
        if img.mode != 'RGB':
            img = img.convert('RGB')
        max_dim = 800
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        jpg_buf = io.BytesIO()
        img.save(jpg_buf, 'JPEG', quality=70, optimize=True)
        jpg_bytes = jpg_buf.getvalue()
        print(f"[upload_photo] JPEG: {img.size}, {len(jpg_bytes)} bytes")
    except Exception as e:
        print(f"[upload_photo] PIL failed: {e}")
        traceback.print_exc()

    # 2. Сервер загрузки
    try:
        server_resp = vk.photos.getMessagesUploadServer(peer_id=user_id)
        upload_url = server_resp['upload_url']
        print(f"[upload_photo] upload_url получен")
    except Exception as e:
        print(f"[upload_photo] getMessagesUploadServer failed: {e}")
        traceback.print_exc()
        raise

    # 3. Отправка multipart с 3 попытками
    last_error = None
    for attempt in range(1, 4):
        try:
            files = {'photo': ('photo.jpg', jpg_bytes)}
            r = requests.post(upload_url, files=files, timeout=90,
                              headers={'User-Agent': 'VKAndroidApp/5.52-4543'})
            print(f"[upload_photo] попытка {attempt}: "
                  f"status={r.status_code}, body={r.text[:200]}")

            if r.status_code == 200 and r.text.strip():
                uploaded = r.json()
                if uploaded.get('photo'):
                    # 4. Сохранение
                    photo_list = vk.photos.saveMessagesPhoto(
                        photo=uploaded['photo'],
                        server=uploaded['server'],
                        hash=uploaded['hash'])
                    if photo_list and photo_list[0].get('id'):
                        print(f"[upload_photo] успех с попытки {attempt}")
                        return f"photo{photo_list[0]['owner_id']}_{photo_list[0]['id']}"
                last_error = f"Ответ без photo: {uploaded}"
            else:
                last_error = f"HTTP {r.status_code}: {r.text[:100]}"

            if attempt < 3:
                print(f"[upload_photo] повтор через 3 сек...")
                time.sleep(3)

        except Exception as e:
            last_error = str(e)
            print(f"[upload_photo] попытка {attempt} упала: {e}")
            if attempt < 3:
                time.sleep(3)

    raise RuntimeError(f"Не удалось за 3 попытки. Последняя ошибка: {last_error}")


def upload_csv_doc(user_id, data, filename):
    """Загружает документ в ВК через прямой API с 3 попытками."""
    last_error = None
    for attempt in range(1, 4):
        try:
            server_resp = vk.docs.getMessagesUploadServer(type='doc', peer_id=user_id)
            upload_url = server_resp['upload_url']

            files = {'file': (filename, data, 'application/octet-stream')}
            r = requests.post(upload_url, files=files, timeout=120)
            print(f"[upload_doc] попытка {attempt}: "
                  f"status={r.status_code}, body={r.text[:200]}")

            if r.status_code == 200 and r.text.strip():
                uploaded = r.json()
                if uploaded.get('file'):
                    saved = vk.docs.save(file=uploaded['file'], title=filename)
                    if isinstance(saved, dict) and 'doc' in saved:
                        doc_info = saved['doc']
                    elif isinstance(saved, dict) and saved.get('id'):
                        doc_info = saved
                    else:
                        last_error = f"docs.save unexpected: {saved}"
                        continue
                    return f"doc{doc_info['owner_id']}_{doc_info['id']}"
            last_error = f"HTTP {r.status_code}: {r.text[:100]}"

            if attempt < 3:
                time.sleep(3)

        except Exception as e:
            last_error = str(e)
            if attempt < 3:
                time.sleep(3)

    raise RuntimeError(f"Не удалось за 3 попытки. Последняя ошибка: {last_error}")


def on_export_csv(user_id, days=30):
    end = tz_today()
    if days is None:
        shipments = get_shipments_by_period(date(2000, 1, 1), end)
        title = "Полный экспорт"
        fname = f'shipments_all_{end.isoformat()}.csv'
    else:
        start = end - timedelta(days=days)
        shipments = get_shipments_by_period(start, end)
        title = f"Экспорт за последние {days} дней"
        fname = f'shipments_{start.isoformat()}_{end.isoformat()}.csv'
    if not shipments:
        send(user_id, "Нет данных.", admin_menu()); return
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    w.writerow(['ID', 'Дата', 'Клиент', 'Тоннаж (т)', 'Фур (расчёт)', 'Комментарий', 'Создано'])
    for s in shipments:
        w.writerow([s['id'], s['shipment_date'], s['client_name'],
                    s['tonnage'], s['trucks'], s.get('comment', ''),
                    s.get('created_at', '')])
    data = buf.getvalue().encode('utf-8-sig')
    try:
        attach = upload_csv_doc(user_id, data, fname)
        send(user_id, f"📤 {title} ({len(shipments)} строк):",
             admin_menu() if is_admin(user_id) else main_menu(is_admin(user_id)),
             attachment=attach)
    except Exception as e:
        traceback.print_exc()
        send(user_id, f"❌ Не удалось загрузить файл: {e}", admin_menu())


# ════════════════════════════ ИСТОРИЯ КЛИЕНТА ════════════════════════════

def show_clients_for_history(user_id, page=0):
    clients = get_clients()
    if not clients:
        send(user_id, "📁 Справочник пуст. Добавьте первую погрузку.",
             main_menu(is_admin(user_id))); return
    send(user_id, "👤 Выберите клиента:",
         clients_menu(clients, action='view', page=page))


def show_client_card(user_id, cid):
    c = get_client(cid)
    if not c:
        send(user_id, "❌ Клиент не найден.", main_menu(is_admin(user_id))); return
    summary = client_summary(c['name'])
    if not summary:
        send(user_id, f"👤 {c['name']}\n\nНет отгрузок.", client_card_menu(cid)); return
    lines = [f"👤 {c['name']}", "",
             f"Всего отгрузок: {summary['count']}",
             f"Общий тоннаж: {fmt_num(summary['total_tonnage'])} т",
             f"Фур: {fmt_num(summary['total_trucks'])}",
             f"Средний тоннаж: {fmt_num(round(summary['avg_tonnage'], 2))} т", "",
             f"Первая отгрузка: {ru_date(summary['first_date'])}",
             f"Последняя: {ru_date(summary['last_date'])}"]
    send(user_id, "\n".join(lines), client_card_menu(cid))


def show_client_history(user_id, cid):
    c = get_client(cid)
    if not c:
        send(user_id, "❌ Клиент не найден.", main_menu(is_admin(user_id))); return
    shipments = get_shipments_by_client(c['name'], limit=100)
    lines = [f"📜 {c['name']} — последние отгрузки:", ""]
    if not shipments:
        lines.append("— нет —")
    else:
        total_t = 0.0
        for s in shipments:
            lines.append(f"• {ru_date(s['shipment_date'])} — "
                         f"{fmt_num(s['tonnage'])} т ({fmt_num(s['trucks'])} фур)")
            total_t += s['tonnage']
        lines += ["", f"Всего (последние {len(shipments)}): {fmt_num(total_t)} т"]
    send(user_id, "\n".join(lines), client_card_menu(cid))


# ════════════════════════════ СРАВНЕНИЕ НЕДЕЛЬ (текст) ════════════════════════════

def show_week_comparison(user_id):
    cmp = week_comparison()
    this_s, last_s = cmp['this'], cmp['last']
    t1, t0 = this_s['total_tonnage'], last_s['total_tonnage']
    if t0 > 0:
        diff_pct = (t1 - t0) / t0 * 100
        pct_sign = '📈 +' if diff_pct >= 0 else '📉 '
        pct_line = f"{pct_sign}{diff_pct:.1f}%"
    else:
        pct_line = "—" if t1 == 0 else "новая неделя"
    diff_t = t1 - t0
    diff_trucks = round(diff_t / TRUCK_CAPACITY, 2)
    lines = ["⚖️ Сравнение недель", "",
             f"Прошлая ({ru_date(cmp['last_range'][0])}–{ru_date(cmp['last_range'][1])}):",
             f"  • {fmt_num(t0)} т",
             f"  • {fmt_num(last_s['total_trucks'])} фур",
             f"  • {last_s['count']} отгрузок", "",
             f"Эта ({ru_date(cmp['this_range'][0])}–{ru_date(cmp['this_range'][1])}):",
             f"  • {fmt_num(t1)} т",
             f"  • {fmt_num(this_s['total_trucks'])} фур",
             f"  • {this_s['count']} отгрузок", "",
             f"Разница: {fmt_num(diff_t)} т ({pct_line})",
             f"          {fmt_num(diff_trucks)} фур"]
    send(user_id, "\n".join(lines), main_menu(is_admin(user_id)))


# ════════════════════════════ АДМИН ════════════════════════════

def on_download_db(user_id):
    if not is_admin(user_id):
        send(user_id, "❌ Только для администраторов."); return
    try:
        data = get_db_bytes()
        attach = upload_csv_doc(user_id, data, f'shipments_{tz_today().isoformat()}.db')
        size_kb = len(data) / 1024
        send(user_id, f"💾 Резервная копия базы ({size_kb:.1f} КБ):",
             admin_menu(), attachment=attach)
    except Exception as e:
        traceback.print_exc()
        send(user_id, f"❌ Не удалось отправить базу: {e}", admin_menu())


def show_clients_info(user_id):
    clients = get_clients(limit=200)
    if not clients:
        send(user_id, "📁 Справочник пуст. Добавьте первую погрузку.",
             main_menu(is_admin(user_id))); return
    lines = ["📁 Клиенты:", ""]
    for c in clients[:50]:
        sh = get_shipments_by_client(c['name'])
        total_t = sum(s['tonnage'] for s in sh)
        lines.append(f"• {c['name']}: {len(sh)} отгр., {fmt_num(total_t)} т")
    if len(clients) > 50:
        lines.append(f"\n… и ещё {len(clients) - 50}")
    send(user_id, "\n".join(lines), main_menu(is_admin(user_id)))


# ════════════════════════════ PAYLOAD ════════════════════════════

def handle_payload(user_id, payload):
    cmd = payload.get('cmd')

    if cmd == 'cancel':
        clear_state(user_id)
        send(user_id, "Отменено.", main_menu(is_admin(user_id)))
    elif cmd == 'to_menu':
        clear_state(user_id)
        show_main_menu(user_id)
    elif cmd == 'charts_menu':
        clear_state(user_id)
        send(user_id, "📈 Выберите график (все в формате А4):", charts_menu())

    elif cmd == 'chart_pick':
        chart = payload.get('chart')
        if chart == 'calendar':
            send(user_id, "📅 Выберите месяц:", chart_calendar_menu())
        elif chart == 'week_compare':
            png = make_week_compare_png()
            _send_chart(user_id, png, "📈 Сравнение недель")
        elif chart == 'client_trend':
            clients = get_clients()
            if not clients:
                send(user_id, "📁 Справочник пуст.", main_menu(is_admin(user_id))); return
            send(user_id, "👤 Выберите клиента для тренда:",
                 clients_menu_chart(clients, page=0))
        else:
            send(user_id, "📅 Выберите период:", chart_period_menu(chart))

    elif cmd == 'chart_draw':
        chart = payload.get('chart')
        period = payload.get('period')
        show_chart(user_id, chart, period)

    elif cmd == 'chart_draw_custom':
        chart = payload.get('chart')
        update_state(user_id, awaiting='chart_custom_start', chart_type=chart)
        send(user_id, "Введите начальную дату (ДД.ММ.ГГГГ):", cancel_menu())

    elif cmd == 'chart_calendar_month':
        month = payload.get('month')
        if month == 'custom':
            update_state(user_id, awaiting='chart_calendar_custom')
            send(user_id, "Введите месяц и год, например 09.2026:", cancel_menu())
        else:
            show_calendar_chart(user_id, month)

    elif cmd == 'chart_client_pick':
        cid = payload.get('cid')
        c = get_client(cid)
        if not c:
            send(user_id, "❌ Клиент не найден.", main_menu(is_admin(user_id))); return
        png = make_client_trend_png(c['name'])
        _send_chart(user_id, png, f"📈 Тренд: {c['name'][:60]}")

    elif cmd == 'chart_clients_page':
        page = int(payload.get('page', 0))
        clients = get_clients()
        send(user_id, "👤 Выберите клиента:",
             clients_menu_chart(clients, page=page))

    elif cmd == 'day_image':
        d = payload.get('date')
        if d:
            try:
                show_day_image(user_id, datetime.strptime(d, '%Y-%m-%d').date())
            except Exception:
                traceback.print_exc()
                show_main_menu(user_id)
        else:
            show_day_image(user_id, tz_today())

    elif cmd == 'back_to_day':
        d = payload.get('date')
        if d:
            try:
                show_day_shipments(user_id, datetime.strptime(d, '%Y-%m-%d').date())
            except Exception:
                show_main_menu(user_id)
        else:
            show_main_menu(user_id)

    elif cmd == 'toggle_report':
        new_val = toggle_morning_report(user_id)
        label = 'включена' if new_val else 'выключена'
        send(user_id, f"Утренняя сводка {label}.",
             main_menu(is_admin(user_id), bool(new_val)))

    elif cmd == 'import_text':
        if not is_admin(user_id):
            send(user_id, "❌ Только для администраторов."); return
        update_state(user_id, awaiting='text_import')
        send(user_id, "✍️ Импорт текстом\n\nФормат: Клиент | Дата | Тоннаж",
             cancel_menu())

    elif cmd == 'import_csv':
        if not is_admin(user_id):
            send(user_id, "❌ Только для администраторов."); return
        update_state(user_id, awaiting='csv_import')
        send(user_id, "📥 Отправьте файл .xlsx как документ (скрепка → Документ).",
             cancel_menu())

    elif cmd == 'rollback_menu':
        if not is_admin(user_id):
            send(user_id, "❌ Только для администраторов."); return
        backups = list_backups(limit=200)
        if not backups:
            send(user_id, "📂 Резервных копий пока нет.", admin_menu()); return
        send(user_id, f"↩️ Выберите копию (всего {len(backups)}):",
             rollback_menu(backups, page=0))

    elif cmd == 'rollback_page':
        backups = list_backups(limit=200)
        page = int(payload.get('page', 0))
        send(user_id, f"↩️ Резервные копии (стр. {page + 1}):",
             rollback_menu(backups, page=page))

    elif cmd == 'restore_backup':
        if not is_admin(user_id):
            send(user_id, "❌ Только для администраторов."); return
        name = payload.get('name', '')
        ok, err = restore_backup(name)
        if ok:
            send(user_id, f"✅ База откатана к копии:\n{name}", admin_menu())
        else:
            send(user_id, f"❌ Не удалось откатить: {err}", admin_menu())

    elif cmd == 'clear_period':
        if not is_admin(user_id):
            send(user_id, "❌ Только для администраторов."); return
        send(user_id, "🗑 За какой период удалить отгрузки?", clear_period_menu())

    elif cmd == 'clear_period_confirm':
        if not is_admin(user_id):
            send(user_id, "❌ Только для администраторов."); return
        period = payload.get('period')
        if period == 'custom':
            update_state(user_id, awaiting='clear_period_custom_start')
            send(user_id, "Введите начальную дату (ДД.ММ.ГГГГ):", cancel_menu())
            return
        s, e, label = _period_bounds(period)
        if not s or not e:
            send(user_id, "Не понял период.", admin_menu()); return
        cnt = count_shipments_period(s.isoformat(), e.isoformat())
        if cnt == 0:
            send(user_id, f"За период «{label}» нет отгрузок.", admin_menu()); return
        update_state(user_id, awaiting='clear_period_final_confirm',
                     clear_start=s.isoformat(), clear_end=e.isoformat(),
                     clear_label=label, clear_count=cnt)
        kb = VkKeyboard(inline=True)
        kb.add_button(f'✅ Да, удалить {cnt}', color=VkKeyboardColor.NEGATIVE,
                      payload=_p(cmd='clear_period_do'))
        kb.add_button('❌ Отмена', color=VkKeyboardColor.SECONDARY,
                      payload=_p(cmd='cancel'))
        send(user_id, f"⚠️ Подтвердите удаление.\n\nПериод: {label}\n"
                      f"Будет удалено: {cnt}\n\nНажмите «Да, удалить».",
             kb.get_keyboard())

    elif cmd == 'clear_period_do':
        if not is_admin(user_id):
            send(user_id, "❌ Только для администраторов."); return
        st = get_state(user_id)
        start = st.get('clear_start')
        end = st.get('clear_end')
        label = st.get('clear_label', '')
        if not start or not end:
            send(user_id, "❌ Нет активного периода.", admin_menu()); return
        try: make_backup('before-clear')
        except Exception: traceback.print_exc()
        deleted = delete_shipments_period(start, end)
        clear_state(user_id)
        send(user_id, f"🗑 Удалено: {deleted}\nПериод: {label}", admin_menu())

    elif cmd == 'pick_date':
        d = payload.get('date')
        if d == 'today': target = tz_today()
        elif d == 'tomorrow': target = tz_today() + timedelta(days=1)
        elif d == 'custom':
            update_state(user_id, awaiting='add_custom_date')
            send(user_id, "Введите дату (ДД.ММ.ГГГГ):", cancel_menu()); return
        else: return
        update_state(user_id, pending={'date': target.isoformat()})
        show_client_picker(user_id, action='add')

    elif cmd == 'pick_client':
        cid = payload.get('cid')
        action = payload.get('action', 'add')
        sid = payload.get('sid')
        c = get_client(cid)
        if not c:
            send(user_id, "Клиент не найден.", main_menu(is_admin(user_id))); return
        name = c['name']
        if action == 'add':
            p = get_state(user_id).get('pending', {})
            p['client'] = name
            update_state(user_id, awaiting='add_tonnage', pending=p)
            send(user_id, f"👤 {name}\nВведите тоннаж или выберите:", tonnage_menu())
        elif action == 'edit_client' and sid:
            update_shipment(sid, client_name=name, user_id=user_id)
            clear_state(user_id)
            s = get_shipment(sid)
            send(user_id, f"✅ Клиент изменён.\n\n{format_shipment_card(s)}",
                 main_menu(is_admin(user_id)))

    elif cmd == 'clients_page':
        show_client_picker(user_id, action=payload.get('action', 'add'),
                           sid=payload.get('sid'), page=int(payload.get('page', 0)))

    elif cmd == 'new_client':
        update_state(user_id, awaiting='new_client_name',
                     action=payload.get('action', 'add'), sid=payload.get('sid'))
        send(user_id, "Введите имя нового клиента:", cancel_menu())

    elif cmd == 'pick_tonnage':
        t = payload.get('t')
        if t == 'custom':
            update_state(user_id, awaiting='add_tonnage')
            send(user_id, "Введите тоннаж числом:", cancel_menu()); return
        try: t = float(t)
        except (TypeError, ValueError): return
        p = get_state(user_id).get('pending', {})
        p['tonnage'] = t
        update_state(user_id, awaiting=None, pending=p)
        show_confirm(user_id)

    elif cmd == 'save_shipment':
        on_save_shipment(user_id)

    elif cmd == 'restart_add':
        start_add(user_id)

    elif cmd == 'add_again':
        p = get_state(user_id).get('pending', {})
        d = p.get('date') or tz_today().isoformat()
        start_add_with_date(user_id, d)

    elif cmd == 'add_again_for':
        start_add_with_date(user_id, payload.get('date') or tz_today().isoformat())

    elif cmd == 'add_comment':
        update_state(user_id, awaiting='add_comment')
        send(user_id, "Введите комментарий (или '-' чтобы убрать):", cancel_menu())

    elif cmd == 'stats':
        period = payload.get('period')
        if period == 'custom':
            update_state(user_id, awaiting='stats_custom_start')
            send(user_id, "Введите начальную дату (ДД.ММ.ГГГГ):", cancel_menu()); return
        show_stats(user_id, period=period)

    elif cmd == 'export_csv':
        on_export_csv(user_id)
    elif cmd == 'export_csv_all':
        on_export_csv(user_id, days=None)
    elif cmd == 'download_db':
        on_download_db(user_id)
    elif cmd == 'admin_menu':
        if is_admin(user_id): send(user_id, "⚙️ Настройки:", admin_menu())
    elif cmd == 'open_shipment':
        on_open_shipment(user_id, payload)

    elif cmd == 'edit_shipment':
        sid = payload.get('sid')
        s = get_shipment(sid)
        if not s:
            send(user_id, "Не найдено.", main_menu(is_admin(user_id))); return
        send(user_id, f"Что изменить?\n\n{format_shipment_card(s)}", edit_menu(sid))

    elif cmd == 'edit_field':
        sid = payload.get('sid')
        field = payload.get('field')
        s = get_shipment(sid)
        if not s:
            send(user_id, "Не найдено.", main_menu(is_admin(user_id))); return
        if field == 'client':
            show_client_picker(user_id, action='edit_client', sid=sid)
        elif field == 'tonnage':
            update_state(user_id, awaiting='edit_tonnage', sid=sid)
            send(user_id, f"Текущий тоннаж: {fmt_num(s['tonnage'])} т\nВведите новый:",
                 cancel_menu())
        elif field == 'date':
            update_state(user_id, awaiting='edit_date', sid=sid)
            send(user_id, f"Текущая дата: {ru_date(s['shipment_date'])}\nВведите новую:",
                 cancel_menu())
        elif field == 'comment':
            update_state(user_id, awaiting='edit_comment', sid=sid)
            send(user_id, f"Текущий комментарий: {s.get('comment') or '(нет)'}\n"
                          f"Введите новый:", cancel_menu())

    elif cmd == 'del_shipment':
        sid = payload.get('sid')
        if not is_admin(user_id):
            send(user_id, "❌ Только администратор."); return
        delete_shipment(sid)
        send(user_id, "🗑 Удалено.", main_menu(is_admin(user_id)))

    elif cmd == 'open_client':
        show_client_card(user_id, payload.get('cid'))
    elif cmd == 'clients_summary':
        show_clients_info(user_id)
    elif cmd == 'client_history':
        show_client_history(user_id, payload.get('cid'))
    elif cmd == 'client_chart':
        c = get_client(payload.get('cid'))
        if not c:
            send(user_id, "❌ Клиент не найден.", main_menu(is_admin(user_id))); return
        png = make_client_trend_png(c['name'])
        _send_chart(user_id, png, f"📈 Тренд: {c['name'][:60]}")

    elif cmd == 'week_compare':
        show_week_comparison(user_id)


def _period_bounds(period):
    today = tz_today()
    if period == 'today': return today, today, f"Сегодня ({ru_date(today)})"
    if period == 'yesterday':
        y = today - timedelta(days=1)
        return y, y, f"Вчера ({ru_date(y)})"
    if period == 'week':
        s = today - timedelta(days=today.weekday())
        return s, today, f"Эта неделя ({ru_date(s)}–{ru_date(today)})"
    if period == 'month':
        s = today.replace(day=1)
        return s, today, f"Этот месяц ({ru_date(s)}–{ru_date(today)})"
    if period == 'year':
        s = today.replace(month=1, day=1)
        return s, today, f"Этот год ({ru_date(s)}–{ru_date(today)})"
    return None, None, None


# ════════════════════════════ ТЕКСТ ════════════════════════════

MAIN_MENU_BUTTONS = {
    '➕ Добавить погрузку', '📋 Сегодня', '📋 Завтра', '📅 Другая дата',
    '📊 Статистика', '📁 Клиенты', '❓ Помощь', '⚙️ Настройки',
    '🖼 Картинка дня', '📈 Графики'
}


def handle_text(user_id, text):
    text = (text or '').strip()

    if text.lower() in ('/отмена', 'отмена', 'cancel', '/cancel'):
        clear_state(user_id)
        send(user_id, "Отменено.", main_menu(is_admin(user_id)))
        return

    is_menu_btn = (text in MAIN_MENU_BUTTONS
                   or text.startswith('🔔 Сводка')
                   or text.startswith('🔕 Сводка'))
    if is_menu_btn and get_state(user_id).get('awaiting'):
        clear_state(user_id)

    state = get_state(user_id)
    awaiting = state.get('awaiting')

    if awaiting == 'chart_custom_start':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        update_state(user_id, awaiting='chart_custom_end',
                     chart_start=d.isoformat())
        send(user_id, "Введите конечную дату (ДД.ММ.ГГГГ):", cancel_menu())
        return

    if awaiting == 'chart_custom_end':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        chart = state.get('chart_type')
        start = state.get('chart_start')
        clear_state(user_id)
        if chart and start:
            show_chart_custom(user_id, chart, start, d.isoformat())
        else:
            show_main_menu(user_id)
        return

    if awaiting == 'chart_calendar_custom':
        m = re.match(r'(\d{1,2})[./](\d{4})', text.strip())
        if not m:
            send(user_id, "❌ Формат: ММ.ГГГГ (например 09.2026):", cancel_menu()); return
        month = int(m.group(1)); year = int(m.group(2))
        if not (1 <= month <= 12):
            send(user_id, "❌ Месяц от 1 до 12:", cancel_menu()); return
        clear_state(user_id)
        show_calendar_chart_custom(user_id, year, month)
        return

    if awaiting == 'text_import':
        if len(text) < 5:
            send(user_id, "❌ Слишком короткий текст.", cancel_menu()); return
        imported, skipped, errors = import_from_text(user_id, text)
        clear_state(user_id)
        result = [f"✍️ Импорт завершён:", f"• Добавлено: {imported}"]
        if skipped: result.append(f"• Пропущено: {skipped}")
        if errors:
            result.append(""); result.append("Примеры ошибок:")
            result.extend(errors[:5])
        send(user_id, "\n".join(result), admin_menu())
        return

    if awaiting == 'csv_import':
        send(user_id, "📎 Пришлите файл .xlsx как документ.", cancel_menu())
        return

    if awaiting == 'clear_period_custom_start':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        update_state(user_id, awaiting='clear_period_custom_end',
                     clear_start=d.isoformat())
        send(user_id, "Введите конечную дату:", cancel_menu())
        return

    if awaiting == 'clear_period_custom_end':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        s_iso = state.get('clear_start')
        try:
            s = datetime.strptime(s_iso, '%Y-%m-%d').date()
        except Exception:
            send(user_id, "❌ Ошибка.", admin_menu()); return
        cnt = count_shipments_period(s.isoformat(), d.isoformat())
        if cnt == 0:
            clear_state(user_id)
            send(user_id, f"Нет отгрузок за период {ru_date(s)}–{ru_date(d)}.",
                 admin_menu()); return
        label = f"{ru_date(s)}–{ru_date(d)}"
        update_state(user_id, awaiting='clear_period_final_confirm',
                     clear_start=s.isoformat(), clear_end=d.isoformat(),
                     clear_label=label, clear_count=cnt)
        kb = VkKeyboard(inline=True)
        kb.add_button(f'✅ Да, удалить {cnt}', color=VkKeyboardColor.NEGATIVE,
                      payload=_p(cmd='clear_period_do'))
        kb.add_button('❌ Отмена', color=VkKeyboardColor.SECONDARY,
                      payload=_p(cmd='cancel'))
        send(user_id, f"⚠️ Период: {label}\nБудет удалено: {cnt}\n\nПодтвердите:",
             kb.get_keyboard())
        return

    if awaiting == 'add_custom_date':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        clear_state(user_id)
        set_state(user_id, pending={'date': d.isoformat()})
        show_client_picker(user_id, action='add')

    elif awaiting == 'new_client_name':
        name = text.strip()
        if not name:
            send(user_id, "❌ Пустое имя:", cancel_menu()); return
        action = state.get('action', 'add')
        sid = state.get('sid')
        if action == 'edit_client' and sid:
            update_shipment(sid, client_name=name, user_id=user_id)
            clear_state(user_id)
            s = get_shipment(sid)
            send(user_id, f"✅ Клиент изменён.\n\n{format_shipment_card(s)}",
                 main_menu(is_admin(user_id)))
        else:
            touch_client(name)
            p = state.get('pending', {})
            p['client'] = name
            update_state(user_id, awaiting='add_tonnage', pending=p)
            send(user_id, f"👤 {name}\nВведите тоннаж или выберите:", tonnage_menu())

    elif awaiting == 'add_tonnage':
        t = parse_number(text)
        if t is None or t <= 0:
            send(user_id, "❌ Введите положительное число:", cancel_menu()); return
        p = state.get('pending', {})
        p['tonnage'] = t
        update_state(user_id, awaiting=None, pending=p)
        show_confirm(user_id)

    elif awaiting == 'add_comment':
        comment = text.strip()
        if comment == '-': comment = ''
        p = state.get('pending', {})
        p['comment'] = comment
        update_state(user_id, awaiting=None, pending=p)
        show_confirm(user_id)

    elif awaiting == 'edit_tonnage':
        t = parse_number(text)
        if t is None or t <= 0:
            send(user_id, "❌ Введите положительное число:", cancel_menu()); return
        sid = state.get('sid')
        update_shipment(sid, tonnage=t, user_id=user_id)
        clear_state(user_id)
        s = get_shipment(sid)
        send(user_id, f"✅ Тоннаж изменён.\n\n{format_shipment_card(s)}",
             shipment_item_menu(sid, s['shipment_date'], allow_delete=is_admin(user_id)))

    elif awaiting == 'edit_date':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        sid = state.get('sid')
        update_shipment(sid, shipment_date=d.isoformat(), user_id=user_id)
        clear_state(user_id)
        s = get_shipment(sid)
        send(user_id, f"✅ Дата изменена.\n\n{format_shipment_card(s)}",
             shipment_item_menu(sid, s['shipment_date'], allow_delete=is_admin(user_id)))

    elif awaiting == 'edit_comment':
        comment = text.strip()
        if comment == '-': comment = ''
        sid = state.get('sid')
        update_shipment(sid, comment=comment, user_id=user_id)
        clear_state(user_id)
        s = get_shipment(sid)
        send(user_id, f"✅ Комментарий изменён.\n\n{format_shipment_card(s)}",
             shipment_item_menu(sid, s['shipment_date'], allow_delete=is_admin(user_id)))

    elif awaiting == 'stats_custom_start':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        update_state(user_id, awaiting='stats_custom_end', stats_start=d.isoformat())
        send(user_id, "Введите конечную дату:", cancel_menu())

    elif awaiting == 'stats_custom_end':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        start = state.get('stats_start')
        clear_state(user_id)
        show_stats(user_id, period='custom', start=start, end=d.isoformat())

    elif text == '➕ Добавить погрузку':
        start_add(user_id)
    elif text == '📋 Сегодня':
        show_day_shipments(user_id, tz_today())
    elif text == '🖼 Картинка дня':
        show_day_image(user_id, tz_today())
    elif text == '📋 Завтра':
        show_day_shipments(user_id, tz_today() + timedelta(days=1))
    elif text == '📅 Другая дата':
        update_state(user_id, awaiting='add_custom_date')
        send(user_id, "Введите дату (ДД.ММ.ГГГГ):", cancel_menu())
    elif text == '📊 Статистика':
        send(user_id, "Выберите период:", stats_menu())
    elif text == '📈 Графики':
        clear_state(user_id)
        send(user_id, "📈 Выберите график (все в формате А4):", charts_menu())
    elif text == '📁 Клиенты':
        show_clients_for_history(user_id)
    elif text == '❓ Помощь':
        show_help(user_id)
    elif text == '⚙️ Настройки' and is_admin(user_id):
        send(user_id, "⚙️ Настройки:", admin_menu())
    elif text.startswith('🔔 Сводка') or text.startswith('🔕 Сводка'):
        new_val = toggle_morning_report(user_id)
        label = 'включена' if new_val else 'выключена'
        send(user_id, f"Утренняя сводка {label}.",
             main_menu(is_admin(user_id), bool(new_val)))
    else:
        send(user_id, "Не понял. Используйте кнопки меню.",
             main_menu(is_admin(user_id)))


# ════════════════════════════ ФОН ════════════════════════════

_morning_last_day = None


def morning_report_loop():
    global _morning_last_day
    while True:
        try:
            n = tz_now()
            today_key = n.date().isoformat()
            if (n.hour == MORNING_REPORT_HOUR
                    and n.minute >= MORNING_REPORT_MINUTE
                    and _morning_last_day != today_key):
                _morning_last_day = today_key
                for u in get_all_users():
                    if not u.get('morning_report'): continue
                    uid = u['user_id']
                    try:
                        shipments = get_shipments_by_date(n.date())
                        text = format_day_shipments(
                            n.date(), shipments,
                            f"🌅 Доброе утро! Погрузки на сегодня ({ru_date(n.date())}):")
                        send(uid, text, main_menu(is_admin(uid)))
                        try:
                            png = make_day_image_png(n.date(), shipments)
                            attach = upload_photo_bytes(uid, png)
                            total_t = sum(s['tonnage'] for s in shipments)
                            total_trucks = round(total_t / TRUCK_CAPACITY, 2)
                            caption = (f"🖼 Сводка А4 на {ru_date(n.date())}\n"
                                       f"Итого: {fmt_num(total_t)} т, "
                                       f"{fmt_num(total_trucks)} фур")
                            send(uid, caption, main_menu(is_admin(uid)),
                                 attachment=attach)
                        except Exception: traceback.print_exc()
                    except Exception: traceback.print_exc()
        except Exception: traceback.print_exc()
        time.sleep(60)


_backup_last_day = None


def backup_loop():
    global _backup_last_day
    while True:
        try:
            n = tz_now()
            today_key = n.date().isoformat()
            if (n.hour == BACKUP_HOUR
                    and n.minute >= BACKUP_MINUTE
                    and _backup_last_day != today_key):
                path = make_backup('auto')
                _backup_last_day = today_key
                if path: print(f"[backup] saved: {path} at {n}")
        except Exception: traceback.print_exc()
        time.sleep(60)


def _run_keepalive_server():
    port = int(os.getenv('PORT', '8080'))
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'OK')
        def log_message(self, *args): pass
    try:
        server = HTTPServer(('0.0.0.0', port), Handler)
        print(f"[keepalive] listening on port {port}")
        server.serve_forever()
    except Exception as e:
        print(f"[keepalive] error: {e}")


def _extract_payload(event):
    raw = getattr(event, 'payload', None)
    if not raw: return None
    if isinstance(raw, dict): return raw
    try: return json.loads(raw)
    except (ValueError, TypeError): return None


def _extract_attachments(event):
    atts = getattr(event, 'attachments', None)
    if not atts: return []
    if isinstance(atts, list): return atts
    if isinstance(atts, dict):
        inner = atts.get('attachments')
        if isinstance(inner, list): return inner
        result = []
        i = 1
        while True:
            ref = atts.get(f'attach{i}')
            typ = atts.get(f'attach{i}_type')
            if ref is None and typ is None: break
            if typ == 'doc' and ref:
                try:
                    if isinstance(ref, str) and ref.startswith('doc'):
                        parts = ref[3:].split('_')
                        owner_id = int(parts[0]); doc_id = int(parts[1])
                        doc_info = vk.docs.getById(docs=f'{owner_id}_{doc_id}')
                        items = doc_info.get('items') or []
                        if items:
                            result.append({'type': 'doc', 'doc': items[0]})
                except Exception: traceback.print_exc()
            i += 1
        return result
    return []


def _collect_docs_for_import(event, user_id):
    docs = []
    atts = _extract_attachments(event)
    for a in atts:
        if isinstance(a, dict) and a.get('type') == 'doc':
            docs.append(a)
    if not docs:
        msg_id = getattr(event, 'message_id', None)
        if msg_id:
            try:
                resp = vk.messages.getById(message_ids=msg_id)
                items = resp.get('items') or []
                if items:
                    for a in (items[0].get('attachments') or []):
                        if isinstance(a, dict) and a.get('type') == 'doc':
                            docs.append(a)
            except Exception: traceback.print_exc()
    return docs


def main():
    print(f"[bot] starting at {tz_now()} (UTC+{TIMEZONE_OFFSET_HOURS})")
    if not TOKEN: print("❌ VK_TOKEN не задан."); return
    if not GROUP_ID: print("❌ VK_GROUP_ID не задан."); return
    if longpoll is None: print("❌ Long Poll не инициализирован."); return

    threading.Thread(target=_run_keepalive_server, daemon=True).start()
    threading.Thread(target=morning_report_loop, daemon=True).start()
    threading.Thread(target=backup_loop, daemon=True).start()

    print("[bot] listening for events...")
    for event in longpoll.listen():
        if event.type == VkEventType.MESSAGE_NEW and event.to_me:
            user_id = event.user_id
            upsert_user(user_id)

            st = get_state(user_id)
            if st.get('awaiting') == 'csv_import':
                docs = _collect_docs_for_import(event, user_id)
                if docs:
                    try: handle_csv_attachment(user_id, docs)
                    except Exception:
                        traceback.print_exc()
                        send(user_id, "❌ Ошибка при импорте.", cancel_menu())
                        clear_state(user_id)
                    continue

            payload = _extract_payload(event)
            if payload and payload.get('cmd'):
                try: handle_payload(user_id, payload)
                except Exception:
                    traceback.print_exc()
                    send(user_id, "❌ Ошибка. Попробуйте ещё раз.",
                         main_menu(is_admin(user_id)))
            else:
                try: handle_text(user_id, event.text or '')
                except Exception:
                    traceback.print_exc()
                    send(user_id, "❌ Ошибка. Попробуйте ещё раз.",
                         main_menu(is_admin(user_id)))


if __name__ == '__main__':
    main()
