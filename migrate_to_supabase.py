import getpass
import os
import sqlite3
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import app

SOURCE_DB = Path(__file__).with_name('production_control.db')
TABLES = [
    'kitchens', 'materials', 'fgs', 'sfgs', 'fg_bom', 'fg_material_bom',
    'sfg_bom', 'material_rates', 'direct_expense_rates', 'meal_demand',
    'inventory_day', 'app_settings',
]
ID_TABLES = [table for table in TABLES if table != 'app_settings']


def database_url():
    value = getpass.getpass('Paste the Supabase Transaction pooler URI (input hidden): ').strip()
    placeholder = '[YOUR-PASSWORD]'
    if placeholder in value:
        password = getpass.getpass('Enter the Supabase database password (input hidden): ')
        value = value.replace(placeholder, quote(password, safe=''))
    else:
        parsed = urlsplit(value)
        if parsed.password is None:
            password = getpass.getpass('Enter the Supabase database password (input hidden): ')
            user = parsed.username or 'postgres'
            host = parsed.hostname or ''
            if parsed.port:
                host += f':{parsed.port}'
            value = urlunsplit((parsed.scheme, f'{user}:{quote(password, safe="")}@{host}', parsed.path, parsed.query, parsed.fragment))
    if not value.startswith(('postgres://', 'postgresql://')):
        raise ValueError('That does not look like a PostgreSQL connection URI.')
    return value


def main():
    if not SOURCE_DB.is_file():
        raise FileNotFoundError('production_control.db was not found beside this migration tool.')
    os.environ['DATABASE_URL'] = database_url()
    app.init_db()
    source = sqlite3.connect(f'file:{SOURCE_DB.as_posix()}?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    target = app.conn()
    try:
        existing = {table: target.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in TABLES if table != 'app_settings'}
        if any(existing.values()):
            raise RuntimeError('The online database already has app records. Migration stopped to avoid overwriting them.')

        source_counts = {}
        for table in TABLES:
            rows = source.execute(f'SELECT * FROM {table}').fetchall()
            source_counts[table] = len(rows)
            if not rows:
                continue
            columns = rows[0].keys()
            names = ','.join(columns)
            marks = ','.join('?' for _ in columns)
            sql = f'INSERT INTO {table}({names}) VALUES({marks}) ON CONFLICT DO NOTHING'
            for row in rows:
                target.execute(sql, tuple(row))

        for table in ID_TABLES:
            maximum = target.execute(f'SELECT MAX(id) FROM {table}').fetchone()[0]
            target.execute('SELECT setval(pg_get_serial_sequence(?, ?), ?, ?)',
                           (table, 'id', maximum or 1, maximum is not None))
        target.commit()

        target_counts = {table: target.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in TABLES}
        print('Migration finished. Record counts:')
        for table in TABLES:
            print(f'  {table}: local {source_counts[table]}, online {target_counts[table]}')
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Migration stopped: {exc}')
        raise SystemExit(1)
