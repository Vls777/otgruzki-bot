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
    """Удаляет все отгрузки за период (включительно). Возвращает кол-во."""
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
        lines.append("Нет отгрузок за период."); return "\n".
