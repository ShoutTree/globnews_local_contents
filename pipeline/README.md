# glob.news 内容流水线

选题 → 人工筛选 → 成文+配图 → 本地预览 → staging 预览 → 发布生产。

## 日常使用（4 条命令）

```bash
# 1. 选题：抓 RSS（BBC/Guardian/NPR/Phys.org/The Verge 等）→ LLM 挑选改写 → 链接校验 → candidates
python select_topics.py

# 2. 筛选：交互式勾选要做的选题（输入编号，如 1 3 5-8）
python approve.py data/candidates_<date>.json

# 3. 成文+配图：抓参考报道 → AI 撰写 → 写实封面图，产出 data/articles/<slug>/
python build_articles.py data/candidates_<date>.json

# 4. 预览 + 发布（文章归档在 data/articles/YYYY/MM/DD/<slug>/）：
#    本地： open data/articles/2026/10/24/<slug>/preview.html
#    单篇 staging 预览：
python publish.py --article data/articles/2026/10/24/<slug>/article.json --target staging
#    按日期批量发布（先 --dry-run 预览清单）：
python publish.py --date 2026-10-24 --target prod --dry-run
python publish.py --date 2026-10-24 --target prod
#    按目录批量（如整月）：
python publish.py --dir data/articles/2026/10 --target prod
#    发布门禁：--target prod 要求文章已过 staging（未过会拒绝并提示）；
#    staging 重复发布会跳过（幂等）；已 published 的重发 prod 也会跳过
```

staging 预览 URL 由 publish.py 打印（带 preview token，配合 staging 的 BasicAuth）。

## 配置：config.json

- **image.backend** — 换生图模型只改这一处（dashscope_qwen_image / dashscope_wanx），
  模型名在 image.backends.<name>.model 里改
- **text_models** — 文本模型列表（按序 fallback）
- **rss_sources** — 新闻源（name/url/category_hint）
- **exclude_keywords** — 选题粗滤词（战争/政治/灾难）
- **topic_count** — 每轮候选数量

## 内容纪律（写进了 prompt，人工过审时同样对照）

- 每篇文章必须基于真实来源报道，文章页展示 Sources 链接
- 不编造引语、数字、细节；不确定就删掉
- 封面一律 AI 生成写实风格图片，不使用真实新闻照片（版权）
- 不生成可识别真人面孔；敏感事件交人工判断

## 数据流向

- candidates：`data/candidates_*.json`（approved 标记筛选状态）
- 文章产物：`data/articles/YYYY/MM/DD/<slug>/{article.json, cover.jpg, preview.html}`（按生成日期归档；2026-10-24 前的旧文章已迁移）
- 发布：封面 → los1 `/www/wwwroot/media/glob.news/covers/`（nginx /media/）；
  文章 → Django `manage.py import_articles`（upsert by slug，status=staging|published）
