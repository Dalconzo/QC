#!/usr/bin/env python3
from __future__ import annotations
import sys, os
import mysql.connector

def parse_dsn(path: str):
    cfg={}
    if not os.path.exists(path):
        return cfg
    for raw in open(path,'r',encoding='utf-8',errors='ignore'):
        line=raw.strip()
        if not line or line.startswith('#') or line.startswith(';'):
            continue
        if '=' in line:
            k,v=line.split('=',1)
            cfg[k.strip().upper()]=v.strip()
    return cfg

def main():
    if len(sys.argv) < 3:
        print('Usage: qc-db-list-schema.py <schema> <dsn-file>')
        return 2
    schema=sys.argv[1]
    dsn=parse_dsn(sys.argv[2])
    cnx=mysql.connector.connect(host=dsn.get('SERVER') or dsn.get('HOST') or 'localhost', port=int(dsn.get('PORT','3306')), user=dsn.get('USER') or dsn.get('UID') or '', password=dsn.get('PASSWORD') or dsn.get('PWD') or '', autocommit=False)
    try:
        cur=cnx.cursor(); cur.execute('SET SESSION TRANSACTION READ ONLY'); cnx.start_transaction(readonly=True); cur.close()
    except Exception: pass
    cur=cnx.cursor()
    cur.execute('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME', (schema,))
    tables=[r[0] for r in cur.fetchall()]
    print(f'Tables in {schema} ({len(tables)}):')
    for t in tables[:50]:
        cur2=cnx.cursor()
        cur2.execute('SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION', (schema,t))
        cols=cur2.fetchall()
        cur2.close()
        col_str=', '.join([f"{c}({d})" for c,d in cols])
        print(f'- {t}: {col_str}')
    cur.close(); cnx.rollback(); cnx.close();
    return 0

if __name__=='__main__':
    sys.exit(main())

