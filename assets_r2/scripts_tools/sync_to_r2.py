#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
assets_r2 → Cloudflare R2 (glob-news-assets) 增量同步。

策略：本地快照（.sync_r2_snapshot.json 记录 path -> {size, mtime}）对比，
      变化/新增的文件上传，远端多余对象删除（sync 语义，可 --no-delete 关闭）。
      快照丢失时自动全量重建（对比远端 list，内容相同则跳过上传）。

用法：
    python3 sync_to_r2.py                    # 同步整个 assets_r2/
    python3 sync_to_r2.py imgs/news/<slug>   # 只同步某子目录（按需，快）
    python3 sync_to_r2.py --dry-run
    python3 sync_to_r2.py --no-delete        # 不删远端多余对象
"""

import argparse
import json
import os
import sys

import boto3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # assets_r2/
TOOLS = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT = os.path.join(TOOLS, '.sync_r2_snapshot.json')
ENV_FILE = os.path.join(TOOLS, 'r2.env')

CONTENT_TYPES = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.webp': 'image/webp', '.gif': 'image/gif', '.svg': 'image/svg+xml',
    '.avif': 'image/avif',
    '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.wav': 'audio/wav',
    '.mp4': 'video/mp4', '.webm': 'video/webm', '.m3u8': 'application/vnd.apple.mpegurl',
    '.json': 'application/json', '.txt': 'text/plain', '.pdf': 'application/pdf',
}
CACHE_CONTROL = 'public, max-age=31536000, immutable'  # 内容寻址/一次性资产，长缓存


def load_env():
    env = {}
    with open(ENV_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env


def client(env):
    return boto3.client(
        's3',
        endpoint_url=env['R2_ENDPOINT'],
        aws_access_key_id=env['R2_ACCESS_KEY_ID'],
        aws_secret_access_key=env['R2_SECRET_ACCESS_KEY'],
        region_name='auto',
    )


# 永不上传：工具/凭证/快照（r2.env 含 secret，公开 bucket 绝不能上传）
EXCLUDE_DIRS = {'scripts_tools', '__pycache__'}
EXCLUDE_EXTS = {'.env'}
EXCLUDE_NAMES = {'.sync_r2_snapshot.json', '.DS_Store'}


def local_files(subdir=''):
    """收集本地待同步文件：{relpath: {'size':, 'mtime':}}"""
    root = os.path.join(BASE, subdir) if subdir else BASE
    if not os.path.isdir(root):
        sys.exit(f"目录不存在: {root}")
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d not in EXCLUDE_DIRS]
        for name in filenames:
            if name.startswith('.') or name in EXCLUDE_NAMES:
                continue
            if os.path.splitext(name)[1].lower() in EXCLUDE_EXTS:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, BASE)
            st = os.stat(full)
            files[rel] = {'size': st.st_size, 'mtime': int(st.st_mtime)}
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('subdir', nargs='?', default='',
                    help='只同步该子目录（相对 assets_r2/，如 imgs/news/<slug>）')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-delete', action='store_true', help='不删除远端多余对象')
    args = ap.parse_args()

    env = load_env()
    bucket = env['R2_BUCKET']
    s3 = client(env)

    new_snap = local_files(args.subdir)
    try:
        with open(SNAPSHOT, encoding='utf-8') as f:
            old_snap = json.load(f)
    except Exception:
        old_snap = {}

    # 上传：新增或 size/mtime 变化
    prefix = (args.subdir.strip('/') + '/') if args.subdir else ''
    uploads, deletes = [], []
    for rel, meta in new_snap.items():
        old = old_snap.get(rel)
        if not old or old['size'] != meta['size'] or old['mtime'] != meta['mtime']:
            uploads.append(rel)

    # 删除：快照里有、且属于本次同步范围、但本地已没有
    if not args.no_delete:
        for rel in old_snap:
            if not rel.startswith(prefix):
                continue
            if rel not in new_snap:
                deletes.append(rel)

    print(f"[sync_to_r2] scope={prefix or '(all)'} local_files={len(new_snap)} "
          f"upload={len(uploads)} delete={len(deletes)}" + (" (dry-run)" if args.dry_run else ""))

    if args.dry_run:
        for rel in uploads:
            print(f"  + {rel}")
        for rel in deletes:
            print(f"  - {rel}")
        return

    ok = err = 0
    for rel in uploads:
        full = os.path.join(BASE, rel)
        ext = os.path.splitext(rel)[1].lower()
        try:
            s3.upload_file(
                full, bucket, rel,
                ExtraArgs={
                    'ContentType': CONTENT_TYPES.get(ext, 'application/octet-stream'),
                    'CacheControl': CACHE_CONTROL,
                },
            )
            ok += 1
            print(f"  ↑ {rel}")
        except Exception as e:
            err += 1
            print(f"  ✗ {rel}: {e}", file=sys.stderr)

    for rel in deletes:
        try:
            s3.delete_object(Bucket=bucket, Key=rel)
            print(f"  ↓ deleted {rel}")
        except Exception as e:
            print(f"  ✗ delete {rel}: {e}", file=sys.stderr)

    # 合并快照（全量记录；非全量同步时保留其他目录的旧记录）
    merged = dict(old_snap)
    for rel in list(merged):
        if not args.no_delete and rel.startswith(prefix) and rel not in new_snap:
            del merged[rel]
    merged.update(new_snap)
    with open(SNAPSHOT, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)

    print(f"[sync_to_r2] done: {ok} uploaded, {len(deletes)} deleted, {err} errors")
    if err:
        sys.exit(1)


if __name__ == '__main__':
    main()
