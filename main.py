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
import urllib.request
import traceback
import threading
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
        c = by_client.setdefault(s['client_name'], {'tonnage': 0.0})
        c['tonnage'] += s['tonnage']
        d = by_day.setdefault(s['shipment_date'], {'tonnage': 0.0})
        d['tonnage'] += s['tonnage']
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
    kb.add_button('📋 Завтра', color=VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button('📅 Другая дата', color=VkKeyboardColor.SECONDARY)
    kb.add_button('📊 Статистика', color=VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button('📁 Клиенты', color=VkKeyboardColor.SECONDARY)
    kb.add_button('❓ Помощь', color=VkKeyboardColor.SECONDARY)
    kb.add_line()
    label = '🔔 Сводка: вкл' if morning_on else '🔕 Сводка: выкл'
    kb.add_button(label, color=VkKeyboardColor.SECONDARY)
    if is_admin:
        kb.add_button('⚙️ Настройки', color=VkKeyboardColor.SECONDARY)
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
    kb.add_button('📊 График', color=VkKeyboardColor.POSITIVE, payload=_p(cmd='chart', period='week'))
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
    for name, c in sorted(stats['by_client'].items(), key=lambda x: -x[1]['tonnage'])[:30]:
        lines.append(f"• {name}: {fmt_num(c['tonnage'])} т / {fmt_num(c['trucks'])} фур")
    lines.append("")
    lines.append("📅 По дням:")
    for d in sorted(stats['by_day'].keys(), reverse=True)[:30]:
        c = stats['by_day'][d]
        lines.append(f"• {ru_date(d)}: {fmt_num(c['tonnage'])} т / {fmt_num(c['trucks'])} фур")
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
        "• ✏️ Изменять и удалять отгрузки\n"
        "• 📊 Статистика за любой период\n"
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


# ════════════════════════════ ИМПОРТ ════════════════════════════

def _split_line(line):
    if '\t' in line: return [p.strip() for p in line.split('\t')]
    if '|' in line: return [p.strip() for p in line.split('|')]
    if ';' in line: return [p.strip() for p in line.split(';')]
    if ',' in line: return [p.strip() for p in line.split(',')]
    parts = re.split(r'\s{2,}', line)
    if len(parts) >= 3: return [p.strip() for p in parts]
    return [p.strip() for p in line.split()]


def import_from_text(user_id, text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines: return 0, 0, ["Пустое сообщение"]
    first_low = lines[0].lower()
    has_header = ('клиент' in first_low or 'контрагент' in first_low or
                  'дата' in first_low or 'тонн' in first_low)
    imported = skipped = 0
    errors = []
    start_idx = 1 if has_header else 0
    for rn, line in enumerate(lines[start_idx:], start=start_idx + 1):
        try:
            parts = _split_line(line)
            if len(parts) < 3:
                errors.append(f"Строка {rn}: нужно 3 колонки"); skipped += 1; continue
            client = parts[0].strip()
            date_str = parts[1].strip()
            tonnage_str = parts[2].strip().replace(',', '.').replace(' ', '')
            if not client or not tonnage_str:
                skipped += 1; continue
            d = parse_month_or_date(date_str)
            if not d:
                errors.append(f"Строка {rn}: не понял дату '{date_str}'"); skipped += 1; continue
            tonnage = float(tonnage_str)
            if tonnage <= 0: raise ValueError("тоннаж <= 0")
            add_shipment(client, tonnage, d.isoformat(), user_id, comment='импорт')
            imported += 1
        except Exception as e:
            errors.append(f"Строка {rn}: {e}"); skipped += 1
    return imported, skipped, errors


def _import_rows(user_id, rows):
    if not rows or len(rows) < 2:
        return 0, 0, ["Нужна хотя бы одна строка данных"]
    header = [str(h or '').strip().lower() for h in rows[0]]
    idx_client = idx_date = idx_tonnage = None
    for i, h in enumerate(header):
        if idx_client is None and ('клиент' in h or 'контрагент' in h or
                                    'наименование' in h or h == 'client'):
            idx_client = i
        elif idx_date is None and ('дата' in h or 'месяц' in h or
                                    'период' in h or h in ('date', 'month')):
            idx_date = i
        elif idx_tonnage is None and ('тонн' in h or 'вес' in h or
                                       'масса' in h or h in ('tonnage', 'weight')):
            idx_tonnage = i
    if idx_client is None or idx_date is None or idx_tonnage is None:
        if len(header) >= 3:
            idx_client, idx_date, idx_tonnage = 0, 1, 2
        else:
            return 0, 0, ["Не удалось определить колонки"]
    imported = skipped = 0
    errors = []
    for rn, r in enumerate(rows[1:], start=2):
        try:
            if r is None or len(r) <= max(idx_client, idx_date, idx_tonnage):
                skipped += 1; continue
            def _cell(v):
                if v is None: return ''
                if isinstance(v, datetime): return v
                return str(v).strip()
            client = _cell(r[idx_client])
            date_raw = r[idx_date]
            tonnage_raw = _cell(r[idx_tonnage]).replace(',', '.').replace(' ', '')
            if not client or tonnage_raw == '':
                skipped += 1; continue
            if isinstance(date_raw, datetime): d = date_raw.date()
            elif isinstance(date_raw, date): d = date_raw
            else: d = parse_month_or_date(_cell(date_raw))
            if not d:
                errors.append(f"Строка {rn}: не понял дату"); skipped += 1; continue
            tonnage = float(tonnage_raw)
            if tonnage <= 0: raise ValueError("тоннаж <= 0")
            add_shipment(client, tonnage, d.isoformat(), user_id, comment='импорт')
            imported += 1
        except Exception as e:
            errors.append(f"Строка {rn}: {e}"); skipped += 1
    return imported, skipped, errors


def import_1c_data(user_id, rows):
    """
    Парсит выгрузку из 1С «Выполнение сборки и отгрузки товаров».

    Логика:
      - шапка отчёта, служебные строки — пропуск
      - «Расходный ордер ...» — пропуск
      - «Акт С/М ...» — пропуск
      - строки с артикулами (число в A) — пропуск
      - ВСЁ ОСТАЛЬНОЕ — клиент, вес берём из последней колонки (кг),
        это итог по клиенту за период. Дата — первый «Расходный ордер» под ним.
    """
    n = len(rows)
    imported = 0
    skipped = 0
    errors = []
    samples = []
    date_re = re.compile(r'от\s+(\d{2}\.\d{2}\.\d{4})')

    service_prefixes = (
        'выполнение сборки', 'параметры', 'отбор',
        'склад', 'получатель', 'регистратор', 'артикул', 'итого',
    )

    for idx, row in enumerate(rows):
        rn = idx + 1
        try:
            if not row:
                continue
            r = list(row)
            if len(r) < 7:
                r = r + [None] * (7 - len(r))
            a = r[0]
            g = r[6]

            a_str = str(a).strip() if a is not None else ''
            if not a_str:
                continue
            a_low = a_str.lower()

            # служебные
            if any(a_low.startswith(p) for p in service_prefixes):
                continue

            # отгрузки и акты
            if 'расходный ордер' in a_low:
                continue
            if a_low.startswith('акт с/м'):
                continue

            # товары (артикул — число)
            if a_str.replace('.', '').replace(',', '').replace(' ', '').isdigit():
                continue

            # если G пусто — ищем числовое значение справа
            if g is None:
                for cell in reversed(r):
                    if cell is None: continue
                    s = str(cell).replace(',', '.').replace(' ', '').strip()
                    if s and re.match(r'^\d+(\.\d+)?$', s):
                        g = cell
                        break

            if g is None:
                skipped += 1
                continue

            g_str = str(g).replace(',', '.').replace(' ', '').strip()
            if not g_str:
                skipped += 1
                continue

            try:
                weight_kg = float(g_str)
            except ValueError:
                skipped += 1
                continue

            if weight_kg <= 0:
                skipped += 1
                continue

            weight_t = round(weight_kg / 1000.0, 3)

            # дата — первый «Расходный ордер» ниже
            shipment_date = None
            j = idx + 1
            while j < n:
                rj = rows[j]
                if not rj:
                    j += 1
                    continue
                aj = str(rj[0]).strip() if rj[0] is not None else ''
                aj_low = aj.lower()
                if 'расходный ордер' in aj_low:
                    m = date_re.search(aj)
                    if m:
                        try:
                            shipment_date = datetime.strptime(
                                m.group(1), '%d.%m.%Y').date()
                        except ValueError:
                            pass
                    break
                if aj and not aj.replace('.', '').replace(',', '').replace(' ', '').isdigit():
                    if not any(aj_low.startswith(p) for p in service_prefixes):
                        if 'расходный ордер' not in aj_low and not aj_low.startswith('акт с/м'):
                            break
                j += 1

            if not shipment_date:
                shipment_date = tz_today()

            add_shipment(a_str, weight_t, shipment_date.isoformat(),
                         user_id, comment='1С')

            if len(samples) < 15:
                samples.append(
                    f"{a_str[:55]} | {shipment_date.isoformat()} | "
                    f"{weight_kg} кг → {weight_t} т")

            imported += 1

        except Exception as e:
            errors.append(f"Строка {rn}: {e}")
            skipped += 1

    if samples:
        print("[import-1c] Примеры:")
        for s in samples:
            print(f"  • {s}")
    print(f"[import-1c] Всего: imported={imported}, skipped={skipped}")

    return imported, skipped, errors


def import_csv_data(user_id, data_bytes, filename=''):
    name = (filename or '').lower()

    if name.endswith('.xlsx') or name.endswith('.xlsm'):
        try:
            from openpyxl import load_workbook
        except ImportError:
            return 0, 0, ["Библиотека openpyxl не установлена"]
        try:
            wb = load_workbook(io.BytesIO(data_bytes), data_only=True)
            ws = wb.active
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
        except Exception as e:
            return 0, 0, [f"Не удалось прочитать Excel: {e}"]

        for r in rows[:300]:
            if r and r[0] and 'расходный ордер' in str(r[0]).lower():
                return import_1c_data(user_id, rows)
        return _import_rows(user_id, rows)

    if name.endswith('.xls'):
        return 0, 0, ["Формат .xls не поддерживается. Сохраните как .xlsx"]

    try:
        text = data_bytes.decode('utf-8-sig')
    except UnicodeDecodeError:
        try: text = data_bytes.decode('cp1251')
        except Exception: return 0, 0, ["Не удалось прочитать файл"]

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines: return 0, 0, ["Файл пуст"]

    for ln in lines[:50]:
        if 'расходный ордер' in ln.lower():
            delim = ';' if ';' in lines[0] else ('\t' if '\t' in lines[0] else ',')
            reader = list(csv.reader(lines, delimiter=delim))
            return import_1c_data(user_id, reader)

    first = lines[0]
    if ';' in first: delim = ';'
    elif '\t' in first: delim = '\t'
    elif ',' in first: delim = ','
    else: return 0, 0, ["Не найден разделитель"]
    rows = list(csv.reader(lines, delimiter=delim))
    return _import_rows(user_id, rows)


def handle_csv_attachment(user_id, attachments):
    try:
        make_backup('before-import')
    except Exception:
        traceback.print_exc()

    for att in attachments:
        if att.get('type') != 'doc':
            continue
        doc = att.get('doc') or {}
        url = doc.get('url')
        filename = doc.get('title', '')
        if not url:
            continue
        send(user_id, f"📥 Скачиваю файл: {filename}")
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = resp.read()
        except Exception as e:
            send(user_id, f"❌ Не удалось скачать файл: {e}")
            clear_state(user_id); return

        send(user_id, "⚙️ Обрабатываю файл (может занять до минуты)...")
        imported, skipped, errors = import_csv_data(user_id, data, filename)
        clear_state(user_id)

        result = [f"📥 Импорт завершён:", f"• Добавлено: {imported}"]
        if skipped: result.append(f"• Пропущено: {skipped}")
        if errors:
            result.append(""); result.append("Примеры ошибок:")
            result.extend(errors[:5])
        result.append("")
        result.append("Проверьте: 📊 Статистика → Этот год")
        result.append("Если что-то не так — ⚙️ Настройки → ↩️ Откатить базу")
        send(user_id, "\n".join(result), admin_menu())
        return
    send(user_id, "❌ Во вложении не найден файл.", admin_menu())
    clear_state(user_id)


# ════════════════════════════ ЭКСПОРТ ════════════════════════════

def upload_csv_doc(user_id, data, filename):
    fd, path = tempfile.mkstemp(suffix='.csv')
    os.close(fd)
    try:
        with open(path, 'wb') as f: f.write(data)
        upload = vk_api.VkUpload(vk_session)
        resp = upload.document(path, filename, peer_id=user_id)
        doc = resp.get('doc') if isinstance(resp, dict) else None
        if not doc: raise RuntimeError(f"Некорректный ответ: {resp}")
        return f"doc{doc['owner_id']}_{doc['id']}"
    finally:
        try: os.remove(path)
        except Exception: pass


def upload_photo_bytes(user_id, photo_bytes):
    fd, path = tempfile.mkstemp(suffix='.png')
    os.close(fd)
    try:
        with open(path, 'wb') as f: f.write(photo_bytes)
        upload = vk_api.VkUpload(vk_session)
        photo = upload.photo_messages(photos=path, peer_id=user_id)[0]
        return f"photo{photo['owner_id']}_{photo['id']}"
    finally:
        try: os.remove(path)
        except Exception: pass


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


# ════════════════════════════ СРАВНЕНИЕ НЕДЕЛЬ ════════════════════════════

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


# ════════════════════════════ ГРАФИКИ ════════════════════════════

def make_chart_png(start, end, title, shipments):
    by_day = defaultdict(float)
    d = start
    while d <= end:
        by_day[d] = 0.0; d += timedelta(days=1)
    for s in shipments:
        day = datetime.strptime(s['shipment_date'], '%Y-%m-%d').date()
        by_day[day] += s['tonnage']
    dates = sorted(by_day.keys())
    values = [by_day[d] for d in dates]
    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.bar(dates, values, color='#4a76a8', edgecolor='#2c4a70')
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.set_ylabel('Тонны')
    ax.grid(axis='y', linestyle='--', alpha=0.5)
    ax.set_axisbelow(True)
    for bar, v in zip(bars, values):
        if v > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                    f'{v:.0f}', ha='center', va='bottom', fontsize=9)
    if len(dates) > 7:
        ax.xaxis.set_major_formatter(DateFormatter('%d.%m'))
        plt.xticks(rotation=45, ha='right')
    else:
        ax.set_xticks(dates)
        ax.set_xticklabels([d.strftime('%d.%m') for d in dates])
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close(fig); buf.seek(0)
    return buf.read()


def show_chart(user_id, period=None, start=None, end=None, client_name=None):
    today = tz_today()
    if client_name:
        shipments = get_shipments_by_client(client_name, limit=2000)
        if not shipments:
            send(user_id, "Нет данных.", main_menu(is_admin(user_id))); return
        dates = sorted(set(s['shipment_date'] for s in shipments))
        s = datetime.strptime(dates[0], '%Y-%m-%d').date()
        e = datetime.strptime(dates[-1], '%Y-%m-%d').date()
        title = f"{client_name} — отгрузки ({ru_date(s)}–{ru_date(e)})"
    else:
        if period == 'week':
            s = today - timedelta(days=today.weekday()); e = today
            title = f"Отгрузки за неделю ({ru_date(s)}–{ru_date(e)})"
        elif period == 'last_week':
            s = today - timedelta(days=today.weekday() + 7); e = s + timedelta(days=6)
            title = f"Отгрузки за прошлую неделю"
        elif period == 'month':
            s = today.replace(day=1); e = today
            title = f"Отгрузки за месяц ({ru_date(s)}–{ru_date(e)})"
        elif period == 'last_month':
            first = today.replace(day=1); e = first - timedelta(days=1)
            s = e.replace(day=1); title = f"Отгрузки за прошлый месяц"
        elif period == 'custom':
            s = datetime.strptime(start, '%Y-%m-%d').date()
            e = datetime.strptime(end, '%Y-%m-%d').date()
            title = f"Отгрузки {ru_date(s)}–{ru_date(e)}"
        else:
            s, e = today - timedelta(days=6), today
            title = "Отгрузки за последние 7 дней"
        shipments = get_shipments_by_period(s, e)
    if not shipments:
        send(user_id, "Нет данных за период.", main_menu(is_admin(user_id))); return
    png = make_chart_png(s, e, title, shipments)
    try:
        attach = upload_photo_bytes(user_id, png)
        total = sum(x['tonnage'] for x in shipments)
        caption = (f"📈 {title}\n\n"
                   f"Итого: {fmt_num(total)} т, "
                   f"{fmt_num(round(total / TRUCK_CAPACITY, 2))} фур")
        send(user_id, caption, main_menu(is_admin(user_id)), attachment=attach)
    except Exception as e:
        traceback.print_exc()
        send(user_id, f"❌ Не удалось отправить график: {e}", main_menu(is_admin(user_id)))


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
        text = ("✍️ Импорт текстом\n\n"
                "Скопируйте данные из 1С или Excel и отправьте одним сообщением.\n\n"
                "Формат строк:\nКлиент | Дата | Тоннаж\n\n"
                "Например:\nООО Ромашка 01.2026 150\nООО Ромашка 02.2026 180\n\n"
                "Дата: 01.2026, 15.01.2026, янв 2026.\n"
                "Заголовок (Клиент Дата Тоннаж) — можно, бот пропустит.")
        send(user_id, text, cancel_menu())

    elif cmd == 'import_csv':
        if not is_admin(user_id):
            send(user_id, "❌ Только для администраторов."); return
        update_state(user_id, awaiting='csv_import')
        send(user_id, "📥 Отправьте файл .xlsx из 1С как документ (скрепка → Документ).",
             cancel_menu())

    elif cmd == 'rollback_menu':
        if not is_admin(user_id):
            send(user_id, "❌ Только для администраторов."); return
        backups = list_backups(limit=200)
        if not backups:
            send(user_id, "📂 Резервных копий пока нет.", admin_menu()); return
        send(user_id, f"↩️ Выберите резервную копию для отката "
                      f"(всего {len(backups)}). Перед откатом создаётся "
                      f"ещё одна копия текущего состояния.",
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
            send(user_id, f"✅ База откатана к копии:\n{name}\n\n"
                          f"Проверьте: 📊 Статистика → Этот год",
                 admin_menu())
        else:
            send(user_id, f"❌ Не удалось откатить: {err}", admin_menu())

    elif cmd == 'clear_period':
        if not is_admin(user_id):
            send(user_id, "❌ Только для администраторов."); return
        send(user_id, "🗑 За какой период удалить отгрузки?\n\n"
                      "Перед удалением создаётся резервная копия базы.\n"
                      "Восстановить можно через ⚙️ Настройки → ↩️ Откатить базу.",
             clear_period_menu())
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
            send(user_id, f"За период «{label}» нет отгрузок.",
                 admin_menu()); return
        update_state(user_id, awaiting='clear_period_final_confirm',
                     clear_start=s.isoformat(), clear_end=e.isoformat(),
                     clear_label=label, clear_count=cnt)
        kb = VkKeyboard(inline=True)
        kb.add_button(f'✅ Да, удалить {cnt}', color=VkKeyboardColor.NEGATIVE,
                      payload=_p(cmd='clear_period_do'))
        kb.add_button('❌ Отмена', color=VkKeyboardColor.SECONDARY,
                      payload=_p(cmd='cancel'))
        send(user_id, f"⚠️ Подтвердите удаление.\n\n"
                      f"Период: {label}\n"
                      f"Будет удалено отгрузок: {cnt}\n\n"
                      f"Нажмите «Да, удалить» для подтверждения.", kb.get_keyboard())
    elif cmd == 'clear_period_do':
        if not is_admin(user_id):
            send(user_id, "❌ Только для администраторов."); return
        st = get_state(user_id)
        start = st.get('clear_start')
        end = st.get('clear_end')
        label = st.get('clear_label', '')
        if not start or not end:
            send(user_id, "❌ Нет активного периода для очистки.",
                 admin_menu()); return
        try:
            make_backup('before-clear')
        except Exception:
            traceback.print_exc()
        deleted = delete_shipments_period(start, end)
        clear_state(user_id)
        send(user_id, f"🗑 Удалено отгрузок: {deleted}\n"
                      f"Период: {label}\n\n"
                      f"Если это была ошибка — ⚙️ Настройки → ↩️ Откатить базу.",
             admin_menu())

    elif cmd == 'pick_date':
        d = payload.get('date')
        if d == 'today': target = tz_today()
        elif d == 'tomorrow': target = tz_today() + timedelta(days=1)
        elif d == 'custom':
            update_state(user_id, awaiting='add_custom_date')
            send(user_id, "Введите дату (ДД.ММ.ГГГГ). Можно: 'сегодня', 'завтра', 'послезавтра'.",
                 cancel_menu()); return
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
            send(user_id, "Введите тоннаж числом (например, 25.5):", cancel_menu()); return
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
        if is_admin(user_id):
            send(user_id, "⚙️ Настройки:", admin_menu())
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
            send(user_id, f"Текущая дата: {ru_date(s['shipment_date'])}\nВведите новую (ДД.ММ.ГГГГ):",
                 cancel_menu())
        elif field == 'comment':
            update_state(user_id, awaiting='edit_comment', sid=sid)
            send(user_id, f"Текущий комментарий: {s.get('comment') or '(нет)'}\nВведите новый:",
                 cancel_menu())
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
        show_chart(user_id, client_name=c['name'])
    elif cmd == 'week_compare':
        show_week_comparison(user_id)
    elif cmd == 'chart':
        period = payload.get('period')
        if period == 'custom':
            update_state(user_id, awaiting='chart_custom_start')
            send(user_id, "Введите начальную дату (ДД.ММ.ГГГГ):", cancel_menu()); return
        show_chart(user_id, period=period)


def _period_bounds(period):
    today = tz_today()
    if period == 'today':
        return today, today, f"Сегодня ({ru_date(today)})"
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
    '📊 Статистика', '📁 Клиенты', '❓ Помощь', '⚙️ Настройки'
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

    if awaiting == 'text_import':
        if len(text) < 5:
            send(user_id, "❌ Слишком короткий текст. Пришлите данные или нажмите «Отмена».",
                 cancel_menu()); return
        imported, skipped, errors = import_from_text(user_id, text)
        clear_state(user_id)
        result = [f"✍️ Импорт завершён:", f"• Добавлено: {imported}"]
        if skipped: result.append(f"• Пропущено: {skipped}")
        if errors:
            result.append(""); result.append("Примеры ошибок:")
            result.extend(errors[:5])
        result.append(""); result.append("Проверьте: 📊 Статистика → Этот год")
        send(user_id, "\n".join(result), admin_menu())
        return

    if awaiting == 'csv_import':
        send(user_id, "📎 Пришлите файл .xlsx как документ (скрепка → Документ) "
                      "или нажмите «Отмена».", cancel_menu())
        return

    if awaiting == 'clear_period_custom_start':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        update_state(user_id, awaiting='clear_period_custom_end',
                     clear_start=d.isoformat())
        send(user_id, "Введите конечную дату (ДД.ММ.ГГГГ):", cancel_menu())
        return
    if awaiting == 'clear_period_custom_end':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        s_iso = state.get('clear_start')
        if not s_iso:
            send(user_id, "❌ Ошибка: начало периода потеряно.", admin_menu()); return
        try:
            s = datetime.strptime(s_iso, '%Y-%m-%d').date()
        except ValueError:
            send(user_id, "❌ Ошибка формата.", admin_menu()); return
        cnt = count_shipments_period(s.isoformat(), d.isoformat())
        if cnt == 0:
            clear_state(user_id)
            send(user_id, f"За период {ru_date(s)}–{ru_date(d)} нет отгрузок.",
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
        send(user_id, f"⚠️ Подтвердите удаление.\n\n"
                      f"Период: {label}\n"
                      f"Будет удалено отгрузок: {cnt}\n\n"
                      f"Нажмите «Да, удалить» для подтверждения.", kb.get_keyboard())
        return

    if awaiting == 'add_custom_date':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        clear_state(user_id)
        set_state(user_id, pending={'date': d.isoformat()})
        show_client_picker(user_id, action='add')
    elif awaiting == 'new_client_name':
        name = text.strip()
        if not name:
            send(user_id, "❌ Пустое имя. Введите ещё раз:", cancel_menu()); return
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
            send(user_id, "❌ Не понял дату. Введите ДД.ММ.ГГГГ:", cancel_menu()); return
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
            send(user_id, "❌ Не понял дату. Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        update_state(user_id, awaiting='stats_custom_end', stats_start=d.isoformat())
        send(user_id, "Введите конечную дату (ДД.ММ.ГГГГ):", cancel_menu())
    elif awaiting == 'stats_custom_end':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        start = state.get('stats_start')
        clear_state(user_id)
        show_stats(user_id, period='custom', start=start, end=d.isoformat())
    elif awaiting == 'chart_custom_start':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        update_state(user_id, awaiting='chart_custom_end', chart_start=d.isoformat())
        send(user_id, "Введите конечную дату (ДД.ММ.ГГГГ):", cancel_menu())
    elif awaiting == 'chart_custom_end':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите ДД.ММ.ГГГГ:", cancel_menu()); return
        start = state.get('chart_start')
        clear_state(user_id)
        show_chart(user_id, period='custom', start=start, end=d.isoformat())
    elif text == '➕ Добавить погрузку':
        start_add(user_id)
    elif text == '📋 Сегодня':
        show_day_shipments(user_id, tz_today())
    elif text == '📋 Завтра':
        show_day_shipments(user_id, tz_today() + timedelta(days=1))
    elif text == '📅 Другая дата':
        update_state(user_id, awaiting='add_custom_date')
        send(user_id, "Введите дату (ДД.ММ.ГГГГ). Можно: 'сегодня', 'завтра', 'послезавтра'.",
             cancel_menu())
    elif text == '📊 Статистика':
        send(user_id, "Выберите период:", stats_menu())
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
                    except Exception:
                        traceback.print_exc()
        except Exception:
            traceback.print_exc()
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
        except Exception:
            traceback.print_exc()
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
                except Exception:
                    traceback.print_exc()
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
            except Exception:
                traceback.print_exc()
    try:
        raw = getattr(event, 'attachments', None)
        print(f"[import] user={user_id} msg_id={getattr(event,'message_id',None)} "
              f"docs={len(docs)} raw={str(raw)[:200]}")
    except Exception:
        pass
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
