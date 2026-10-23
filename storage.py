"""Capability links and confined, explicitly mounted file libraries."""
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import time


def confined(root, relative='', directory=False):
    root = Path(root).resolve()
    parts = str(relative).replace('\\', '/').split('/')
    if any(p in ('.', '..') or ':' in p or p.startswith('.') for p in parts if p):
        raise ValueError('Ugyldig filsti.')
    target = root
    for part in filter(None, parts):
        target = target / part
        if target.is_symlink():
            raise ValueError('Symbolske links deles ikke.')
    if not target.resolve().is_relative_to(root) or not target.exists():
        raise ValueError('Filen eller mappen findes ikke.')
    if directory and not target.is_dir():
        raise ValueError('Vælg en mappe.')
    return target


def listing(root, relative=''):
    folder = confined(root, relative, directory=True)
    entries = []
    for p in folder.iterdir():
        if p.is_symlink() or p.name.startswith('.') or not (p.is_file() or p.is_dir()):
            continue
        entries.append({'name': p.name, 'path': p.relative_to(root).as_posix(),
                        'directory': p.is_dir(), 'size': p.stat().st_size if p.is_file() else 0})
        if len(entries) > 2000:
            raise ValueError('Mappen har over 2.000 elementer. Opdel den i undermapper.')
    return sorted(entries, key=lambda e: (not e['directory'], e['name'].lower()))


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, data):
        data.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data / 'fjorddrop.sqlite')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS shares(id TEXT PRIMARY KEY, name TEXT NOT NULL,
            root TEXT NOT NULL, path TEXT NOT NULL, upload INTEGER NOT NULL,
            expires REAL NOT NULL, token_hash TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS relays(id TEXT PRIMARY KEY, token_hash TEXT NOT NULL);
        ''')
        self.db.commit()

    def get(self, key, default=''):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, value))
        self.db.commit()

    def create(self, name, root, path, upload, hours):
        sid, token = secrets.token_urlsafe(12), secrets.token_urlsafe(32)
        self.db.execute('INSERT INTO shares VALUES(?,?,?,?,?,?,?)',
                        (sid, name[:100], root, path, int(upload), time.time()+hours*3600, digest(token)))
        self.db.commit()
        return sid, token

    def share(self, sid, token=None):
        row = self.db.execute('SELECT * FROM shares WHERE id=? AND expires>?', (sid, time.time())).fetchone()
        if not row or (token is not None and not secrets.compare_digest(row['token_hash'], digest(token))):
            raise PermissionError('Linket er udløbet, tilbagekaldt eller ugyldigt.')
        return dict(row)

    def shares(self):
        return [dict(r) for r in self.db.execute('SELECT id,name,root,path,upload,expires FROM shares ORDER BY expires DESC')]

    def revoke(self, sid):
        self.db.execute('DELETE FROM shares WHERE id=?', (sid,))
        self.db.commit()

    def relay_key(self):
        rid, key = secrets.token_urlsafe(12), secrets.token_urlsafe(32)
        self.db.execute('INSERT INTO relays VALUES(?,?)', (rid, digest(key)))
        self.db.commit()
        return rid, key

    def relay_valid(self, rid, key):
        row = self.db.execute('SELECT token_hash FROM relays WHERE id=?', (rid,)).fetchone()
        return bool(row and secrets.compare_digest(row[0], digest(key)))
