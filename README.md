# AutomatedPubMed — 校园网机构权限下的 PubMed 文献自动下载

给定一个 PubMed 导出 CSV（`PMID, Title, ..., DOI`），自动检索每篇文献的 PDF：
优先使用 **本校订阅权限**（校园网出口 IP），其次开放获取副本，最后回退到
出版社文章页解析。所有文件带元数据索引与 BibTeX，可断点续跑。

> 合规边界：只下载本机构订阅覆盖或开放获取的内容；遇到反爬验证（Cloudflare /
> captcha）时如实记录为 `blocked_bot`，不破解、不伪造身份。工具仅用于个人科研
> 文献整理，请遵守各出版社使用条款与下载频率限制。

---

## 1. 关键结论（逆向分析结果）

| 发现 | 说明 |
| --- | --- |
| 权限载体是 **出口 IP** | 校园网出口为 `218.76.28.x`（湖南长沙 CHINANET NAT 池），出版社按 IP 识别机构，页面显示 `中南大学` / `Central South University`。 |
| **必须绕过本机代理** | 本机 `127.0.0.1:7890`（Clash）会让请求从代理出口发出，机构权限立即失效。所有抓取都显式 `--noproxy *` / `trust_env=False`，并关掉 Chrome 的代理。 |
| Springer 订阅可用 | `link.springer.com/content/pdf/<doi>.pdf` 返回完整 PDF（含付费刊）；首次会 303 跳 `idp.springer.com` 换取机构 cookie，跟随重定向即可。 |
| Wiley 订阅可用 | 文章页显示 `Full Access`；`/doi/pdfdirect/<doi>?download=true` 触发真实下载。 |
| BMC / SpringerOpen 可用 | 走 Springer Nature Link 的同一 PDF 端点。 |
| Elsevier 被 captcha 挡住 | ScienceDirect 返回 `Are you a robot?`，加 `__cf_chl` 参数；无法自动通过。 |
| Cloudflare 站点被挡住 | Karger / T&F / OUP / BMJ / JAMA / NEJM / Science / SAGE / RSNA 停留在 `Just a moment…`。 |
| 中华医学期刊网需验证码 | Yiigle 的 PDF 端点返回 `安全验证` 页面。 |
| PMC 有 PoW 限流 | PMC 的 `/pdf/` 需要解 `cloudpmc-viewer-pow` 工作量证明，浏览器可解但复用 cookie 仍 403，未纳入默认路径。 |
| Springer 大文件很慢 | 部分 PDF 走 GCS 法兰克福节点，实测 ~17 KB/s，必须断点续传，否则文件被截断（pypdf 报 `Invalid object in /Pages`）。 |

## 2. 架构

```
CSV ──► records.Record
          │
          ├─ 1) 出版社直链（fetch.curl_pdf 断点续传 + 浏览器 cookie 回退）
          │      Springer/Nature, Wiley, BMC, PLOS, MDPI …
          ├─ 2) 文章页解析（doi.org → citation_pdf_url / PDF 链接）
          │      仅在非已知反爬前缀时启用
          ├─ 3) 开放获取回退（Unpaywall → 任意 OA PDF）
          └─ 4) 校验：%PDF + 大小 + 页数 + 标题重合度
                 │
                 └─ 落盘 downloads/pdf，状态追加 downloads/state*.jsonl
```

- 浏览器：系统 Chrome + CDP（`--remote-debugging-port`），真实指纹；无头环境用 Xvfb。
- 并发：`--shard i/n` 分片，每片独立 Chrome + 独立状态文件，可并行跑。
- 断点续跑：状态文件记录 `key/status/秒数/页数/标题重合度`，重跑自动跳过已完成。

## 3. 用法

```bash
# 单进程
.venv/bin/python -m pubmed_dl run --csv data/csv-ultrasound-set.csv --out downloads

# 三分片并行（本仓库当前采用）
.venv/bin/python -m pubmed_dl run --csv data/csv-ultrasound-set.csv --out downloads --shard 0/3
.venv/bin/python -m pubmed_dl run --csv data/csv-ultrasound-set.csv --out downloads --shard 1/3
.venv/bin/python -m pubmed_dl run --csv data/csv-ultrasound-set.csv --out downloads --shard 2/3

# 统计
.venv/bin/python -m pubmed_dl report --out downloads

# 单个 DOI 诊断
.venv/bin/python -m pubmed_dl probe --doi 10.1002/jum.15266

# 反爬站点的人工解锁（在 :0 桌面弹出 Chrome，人工完成验证码/登录一次）
.venv/bin/python -m pubmed_dl login --urls https://www.sciencedirect.com/

# 无 DOI 记录回填（Crossref 题名检索 + 年份校验，只补元数据、不下载）
.venv/bin/python -m pubmed_dl.backfill pubmed_search.csv \
    --out data/search_recovered.csv --state-dir downloads
```

环境变量：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `PUBMED_DL_PROFILE_BASE` | `/tmp` | Chrome profile 根目录。默认放 tmpfs 很容易被十来个并发 profile 撑爆，建议指向 `~/.cache/pubmed_dl_chrome` |
| `PUBMED_DL_SD_PACE` | `20` | ScienceDirect 每篇成功后的间隔秒数；低于 20 容易触发 `CPE00001` 会话冷却 |
| `PUBMED_DL_PORT` | `9222` | `login` 使用的 CDP 端口 |
| `PUBMED_DL_DISPLAY` | `:99` | 无头 Xvfb 显示号 |


常用参数：

| 参数 | 作用 |
| --- | --- |
| `--limit N` | 只跑前 N 条（冒烟测试） |
| `--prefix 10.1002,10.1007` | 只跑指定 DOI 前缀 |
| `--retry-failed` | 重试此前失败的记录（配合人工解锁） |
| `--attempt-walled` | 对被判定为反爬墙的前缀也尝试页面解析 |
| `--shard i/n` | 分片并发 |

## 4. 输出

```
downloads/
├── pdf/                    # 命名：年份_期刊_第一作者_PMID.pdf
├── index.csv               # 成功清单（含 source/pages/sha256）
├── references.bib          # BibTeX
├── state.shardN.jsonl      # 逐条状态与耗时
├── logs/shardN.log         # 运行日志
└── tmp/
```

状态值：`ok` / `blocked_bot`（反爬）/ `not_entitled`（订阅不覆盖）/
`no_pdf`（页面无 PDF 链接）/ `invalid_pdf`（校验失败）/ `error`（网络异常）。

## 5. 出版商访问矩阵

| DOI 前缀 | 出版商 | 本网状态 | 路径 |
| --- | --- | --- | --- |
| 10.1002 / 10.1111 / 10.1046 / 10.7863 | Wiley | ✅ 可下载 | `pdfdirect` |
| 10.1007 / 10.1186 | Springer / BMC | ✅ 可下载 | `/content/pdf/` |
| 10.1038 | Nature Portfolio | ✅ 可下载 | `/articles/<suffix>.pdf` |
| 10.1371 | PLOS | ✅ 开放获取 | `article/file?...printable` |
| 10.3390 | MDPI | ✅ 开放获取 | 页面下载 |
| 10.3389 | Frontiers | ✅ 开放获取 | 页面解析 |
| 10.2147 | Dove | ✅ 开放获取 | 页面解析 |
| 10.1016 | Elsevier | ⛔ captcha | 需人工解锁 |
| 10.1159 | Karger | ⛔ Cloudflare | 需人工解锁 |
| 10.1080 / 10.3109 | T&F | ⛔ Cloudflare | 需人工解锁 |
| 10.1093 | OUP | ⛔ Cloudflare | 需人工解锁 |
| 10.1136 | BMJ | ⛔ Cloudflare | 需人工解锁 |
| 10.1001 / 10.1056 / 10.1126 / 10.1177 / 10.1148 | JAMA / NEJM / Science / SAGE / RSNA | ⛔ Cloudflare | 需人工解锁 |
| 10.3760 | 中华医学会 (Yiigle) | ⛔ 验证码 | 需人工解锁 |
| 10.1055 | Thieme | ⛔ 连接失败 | 待查 |
| 10.1097 | LWW | ⚠️ 页面可用，PDF 需人工 | 页面解析 |

## 6. 反爬墙的人工解锁

识别到的墙都是"证明你是人"型（captcha / Turnstile），技术上可以自动化，但属于
规避访问控制，工具**不**这样做。可行路径是人工过一次验证，然后复用会话：

```bash
.venv/bin/python -m pubmed_dl login --urls https://www.sciencedirect.com/
# 在弹出的 Chrome 窗口里完成验证（必要时登录），回车结束
.venv/bin/python -m pubmed_dl run --csv ... --out downloads --retry-failed --attempt-walled
```

cookie 通常数小时到数天有效，可在窗口期内批量取回。

## 7. 实测结果（本机 / 本网）

输入：`data/csv-ultrasound-set.csv`（5424 条 PubMed 记录）。可用渠道按 DOI 前缀统计：

| 前缀 | 出版商 | 成功率 | 说明 |
| --- | --- | --- | --- |
| 10.1002 | Wiley | 94.7% | 直链 + 断点续传 |
| 10.1111 | Wiley | 91.6% | 同上 |
| 10.1046 | Wiley | 91.7% | 同上 |
| 10.1186 | BMC | 98.5% | Springer Nature Link |
| 10.1371 | PLOS | 97.4% | 开放获取 |
| 10.3390 | MDPI | 86.7% | 页面解析 |
| 10.3389 | Frontiers | 68.5% | 期刊 slug 直链 |
| 10.1007 | Springer | 72.6% | 未订阅标题显示 Buy article |
| 10.1515 | De Gruyter | 70.9% | 页面解析 |
| 10.1016 | Elsevier | 2.3% | ScienceDirect captcha，仅 Europe PMC OA 副本 |
| 10.3760 | 中华医学会 | 0% | Yiigle 验证码 |
| 10.1159 / 10.1080 / 10.1093 / 10.1055 / 10.1148 | Karger/T&F/OUP/Thieme/RSNA | 3%–11% | Cloudflare，靠 Europe PMC 补 OA 副本 |
| 10.1097 | LWW / Ovid | 48.7% | 页面解析到 `ovid.com` |
| 10.1155 | Hindawi (Wiley) | 56.4% | Wiley `pdfdirect` |

欧洲 PMC（`europepmc.org/api/getPdf?pmcid=`）可作为被墙期刊的开放获取补充，
但对同一出口 IP 有 ~10 秒级限流，需单线程 + 节流。

## 8. 网络连通性回退

Spandidos、Via Medica、SciELO、Korean J Med Sci 等站点在校园网出口直接不可达（连接超时），
但它们本身是开放获取。工具对这类前缀（`PROXY_FALLBACK_PREFIXES`）会改用本机 VPN
（`HTTPS_PROXY`）解析页面并取 PDF——只是绕开路由问题，不涉及订阅权限。
实测：Spandidos 15/15、Via Medica 15/15、SciELO 13/22、KJMS 7/8。

## 9. 已知限制

- 无 DOI 的记录无法直接解析到出版社；可用 `pubmed_dl.backfill` 做 Crossref 题名回填，
  但 PubMed 里就缺 DOI 且 Crossref 也匹配不到的部分（1960–1980 年代、部分中文刊）只能跳过。
- 订阅不覆盖的标题会返回 `not_entitled`，这是权限问题，不是抓取问题。
- 反爬墙（captcha / Turnstile）不做自动破解；需要人工过一次验证后用 `login` 复用会话，
  脚本只复用该会话的 cookie，并对同一前缀做 600 秒熔断（`Resolver._in_cooldown`）。
- 个别出版社 PDF 走境外对象存储，速度可能低至 20 KB/s；已用 Range 断点续传兜底。
- SAGE 等站点的中国镜像（`sage.cnpereading.com`）只给元数据不给 PDF，已列入 `DEAD_HOSTS` 快速失败。
- 并发数受 `/tmp` 体积限制：十几条流同时跑必须把 `PUBMED_DL_PROFILE_BASE` 指向磁盘。
