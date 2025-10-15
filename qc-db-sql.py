#!/usr/bin/env python3
from __future__ import annotations
import argparse, os, sys
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
    ap=argparse.ArgumentParser(description='Run a read-only SQL query and print rows')
    ap.add_argument('--dsn-file', default='mysql_labsite.dsn')
    ap.add_argument('--sql', required=True, help='SQL to execute (use only SELECT)')
    ap.add_argument('--params', nargs='*', default=[], help='Query parameters')
    ap.add_argument('--limit', type=int, default=None, help='Optional limit to append if supported')
    args=ap.parse_args()
    cfg=parse_dsn(args.dsn_file)
    cnx=mysql.connector.connect(host=cfg.get('SERVER') or cfg.get('HOST') or 'localhost', port=int(cfg.get('PORT','3306')), user=cfg.get('USER') or cfg.get('UID') or '', password=cfg.get('PASSWORD') or cfg.get('PWD') or '', autocommit=False)
    try:
        cur=cnx.cursor(); cur.execute('SET SESSION TRANSACTION READ ONLY'); cnx.start_transaction(readonly=True); cur.close()
    except Exception: pass
    sql=args.sql
    if args.limit is not None and 'limit' not in sql.lower():
        sql += f' LIMIT {int(args.limit)}'
    cur=cnx.cursor()
    cur.execute(sql, tuple(args.params))
    cols=[d[0] for d in cur.description]
    print('\t'.join(cols))
    for row in cur.fetchall():
        print('\t'.join([str(v) if v is not None else '' for v in row]))
    cur.close(); cnx.rollback(); cnx.close()
    return 0

if __name__=='__main__':
    sys.exit(main())

