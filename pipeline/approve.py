#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
步骤 2：人工筛选选题。

列出 candidates 文件中的候选，输入要保留的编号（逗号/空格分隔），
选中的标记 approved:true 写回同一文件。build_articles.py 只处理 approved 的条目。

用法：
    python approve.py data/candidates_20261023_1200.json
    python approve.py <file> --all      # 全部批准（慎用）
    python approve.py <file> --reset    # 清空全部批准
"""

import argparse
import json
import re
import sys


def main():
    parser = argparse.ArgumentParser(description='Approve story candidates for article generation')
    parser.add_argument('file', help='candidates JSON file from select_topics.py')
    parser.add_argument('--all', action='store_true', help='approve everything')
    parser.add_argument('--reset', action='store_true', help='un-approve everything')
    args = parser.parse_args()

    with open(args.file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    candidates = data['candidates']

    print(f"File: {args.file}  ({data.get('generated_at','')})\n")
    for i, c in enumerate(candidates, 1):
        mark = 'x' if c.get('approved') else ' '
        print(f"  [{mark}] {i:2d}. [{c.get('category','?'):10s}] (score {c.get('score','?')}) {c.get('title','')[:85]}")
        print(f"           {c.get('description','')[:110]}")
        print(f"           src: {c.get('source_name','')} -> {c.get('source_url','')[:85]}\n")

    if args.all:
        for c in candidates:
            c['approved'] = True
    elif args.reset:
        for c in candidates:
            c['approved'] = False
    else:
        raw = input("Approve which numbers? (e.g. 1 3 5-8, empty = keep as-is, 'q' quit): ").strip()
        if raw.lower() == 'q':
            sys.exit(0)
        if raw:
            picked = set()
            for part in re.split(r'[,\s]+', raw):
                if not part:
                    continue
                if '-' in part:
                    a, b = part.split('-', 1)
                    picked.update(range(int(a), int(b) + 1))
                else:
                    picked.add(int(part))
            for i, c in enumerate(candidates, 1):
                if i in picked:
                    c['approved'] = True

    approved = sum(1 for c in candidates if c.get('approved'))
    with open(args.file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\n{approved} approved. Saved -> {args.file}")
    if approved:
        print("Next: python build_articles.py " + args.file)


if __name__ == '__main__':
    main()
