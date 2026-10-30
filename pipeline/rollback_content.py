#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内容回滚：把某日期（不含）之后发布的文章从生产撤下（status: published -> staging）。

线上数据以本地 data/articles/ 为准，回滚只是"下线"，不删任何数据；
需要恢复时重新发布即可（import 为 upsert，会重新置为 published）：

    python publish.py --date 2026-10-27 --target prod

用法：
    python rollback_content.py --list-since 2026-10-27     # 查看某日（含）之后发布的文章
    python rollback_content.py --to 2026-10-26             # 回滚到 10-26：撤下 10-27 起发布的文章
    python rollback_content.py --to 2026-10-26 --dry-run   # 预览将撤下哪些，不动数据
"""

import argparse
import subprocess
import sys
from datetime import datetime, timedelta

from common import load_config


def cutoff_of(date_str: str) -> str:
    """'2026-10-26' -> '2026-10-27'（保留当天，撤下之后）。"""
    return (datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')


def run_remote_manage(config, django_snippet: str):
    site = config['site']
    r = subprocess.run(
        ['ssh', site['ssh_host'], f"{site['manage']} shell -c \"{django_snippet}\""],
        capture_output=True, text=True)
    # 过滤 Django 启动时的 CORS 警告噪音
    out = '\n'.join(l for l in r.stdout.splitlines() if not l.startswith('警告'))
    return r.returncode, out


def list_after(config, date_str: str):
    snippet = (
        "from api_news.models import NewsPost; "
        f"qs=NewsPost.objects.filter(status='published', published_at__gte='{cutoff_of(date_str)}'); "
        "print('COUNT', qs.count()); "
        "[print(p.slug, p.published_at.date()) for p in qs.order_by('published_at')]"
    )
    return run_remote_manage(config, snippet)


def retire_after(config, date_str: str):
    snippet = (
        "from api_news.models import NewsPost; "
        f"print('UPDATED', NewsPost.objects.filter(status='published', "
        f"published_at__gte='{cutoff_of(date_str)}').update(status='staging', featured=False))"
    )
    return run_remote_manage(config, snippet)


def main():
    parser = argparse.ArgumentParser(description='Roll back published articles by date')
    parser.add_argument('--list-since', metavar='YYYY-MM-DD',
                        help='列出该日（含）之后发布的文章')
    parser.add_argument('--to', metavar='YYYY-MM-DD',
                        help='回滚到该日（保留该日及之前，之后发布的撤回 staging）')
    parser.add_argument('--dry-run', action='store_true', help='只预览，不修改')
    args = parser.parse_args()

    if not args.list_since and not args.to:
        parser.error('需要 --list-since 或 --to（用法见文件头注释）')

    config = load_config()

    if args.list_since or args.dry_run:
        date_str = args.list_since or args.to
        code, out = list_after(config, date_str)
        if code != 0:
            sys.exit(f'remote query failed: {out}')
        print(out.strip() or '（无）')
        if args.dry_run:
            print('\nDRY RUN — 未修改任何数据。')
        return

    # --to 正式执行：先列出确认对象，再更新
    code, out = list_after(config, args.to)
    if code != 0:
        sys.exit(f'remote query failed: {out}')
    print(out.strip())
    if 'COUNT 0' in out:
        print(f'\n{args.to} 之后没有已发布文章，无需回滚。')
        return

    code, out = retire_after(config, args.to)
    if code != 0:
        sys.exit(f'update failed: {out}')
    print(out.strip())
    print(f"\n已回滚到 {args.to}。恢复发布：python publish.py --date YYYY-MM-DD --target prod")


if __name__ == '__main__':
    main()
