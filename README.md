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
| Cloudflare 托管挑战会自动通过 | 真实 Chrome 首次访问需 18–45 s：Karger 45 s、T&F 19 s、OUP 26 s、BMJ 在 45 s 导航超时后页面仍继续渲染。通过后 `cf_clearance` 进上下文，同域后续请求直连；PDF 端点必须由浏览器上下文携带 cookie，裸 curl 一律 403。 |
| `page.goto` 超时不等于失败 | 挑战页会拖住 `DOMContentLoaded`；实测 Karger/BMJ 的 goto 达到 45 s 上限时文章已经在渲染，旧代码直接丢弃标签页，是 `blocked_bot` 的主因之一。 |
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

### 2.1 browser-act 后端（挑战型出版社）

`browser_act.BrowserAct` 是 Playwright 失败后的第二引擎，覆盖
`10.1001 / 10.1056 / 10.1080 / 10.1093 / 10.1136 / 10.1159 / 10.1177 / 10.1148 / 10.1126`：
直链和文章页仍先走 Playwright（暖态更快），只有 `blocked_bot` / `no_pdf` / `error` 时才切到
browser-act（独立 cookie 罐，可绕开 Playwright 侧被挑战的状态）。

1. `browser-act --session … navigate` 打开文章页并轮询正文 / PDF 候选；
2. 本机 `chrome` 类型带自动化标记，Cloudflare 不会自动放行；遇到挑战时由 Playwright 的 headed
   Chrome 过一次，`cookies clear` + `cookies import` 把 clearance 灌进 browser-act，再导航；
3. 依次尝试 `citation_pdf_url`、`/pdf/`、`articlepdf`、`pdfdirect` 候选，`cookies export --url <pdf>`
   后由 `requests` 用同一 UA 取字节；
4. 失败则保留 Playwright 的原始结论（`blocked_bot` / `not_entitled`）。

browser-act 的 Chrome 继承桌面代理（本机 Clash），出口会变成 VPN、丢掉机构 IP 权限。
必须让 daemon 在没有代理环境、且 GSettings 回落到默认（直连）时启动：

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
    -u ALL_PROXY -u all_proxy GSETTINGS_BACKEND=memory \
    browser-act daemon restart
browser-act --session ip-check browser open <browser_id> https://ifconfig.me/ip   # 应为 218.76.28.x
```

浏览器环境只需创建一次：

```bash
browser-act browser create --type chrome --name pubmed-publishers \
  --desc "PubMed PDF fetch for challenge-fronted publishers"
```

`PUBMED_DL_BROWSER_ACT` 指定浏览器名（默认 `pubmed-publishers`，空串禁用）。

实测（2026-11-19）：

| 站点 | browser-act 结果 | 关键点 |
| --- | --- | --- |
| Karger / T&F / BMJ / JAMA / NEJM | `ok`，`source=browser-act:<host>` | 先 `cookies clear` + `robots.txt` 上 `cookies import`，再导航文章 |
| browser-act `chrome` 自身过挑战 | 3 分钟仍 `Just a moment…` | 自动化指纹被识别；只靠导入 clearance 无效 |
| cookie 文件格式 | `expires` 必须是整数 | 浮点过期时间会被 CLI 静默丢弃，`cf_clearance` 随之丢失 |
| 导入时机 | 必须在同域非挑战页（robots.txt） | 挑战页会先写入一个无效的新 `cf_clearance` 覆盖导入值 |
| `chrome-direct` | 本机返回 `230404`，并会终止正在运行的 Chrome | Xvfb 环境不可用，未采用 |

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

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--limit N` | 只跑前 N 条（冒烟测试） |
| `--prefix 10.1002,10.1007` | 只跑指定 DOI 前缀 |
| `--retry-failed` | 重试此前失败的记录（配合人工解锁） |
| `--attempt-walled` | 对被判定为反爬墙的前缀也尝试页面解析 |
| `--wall-wait S` | 等待挑战自行通过的秒数（默认 90，等价 `PUBMED_DL_WALL_WAIT`） |
| `--browser-act NAME` | browser-act 浏览器名；传空串禁用该后端 |
| `--shard i/n` | 分片并发 |

环境变量：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `PUBMED_DL_PROFILE_BASE` | `/tmp` | Chrome profile 根目录。默认放 tmpfs 很容易被十来个并发 profile 撑爆，建议指向 `~/.cache/pubmed_dl_chrome` |
| `PUBMED_DL_SD_PACE` | `20` | ScienceDirect 每篇成功后的间隔秒数；低于 20 容易触发 `CPE00001` 会话冷却 |
| `PUBMED_DL_WALL_WAIT` | `90` | 每篇等待 JS 挑战自行通过的上限秒数；托管挑战实测 18–45 s，留足余量 |
| `PUBMED_DL_ROUNDS` | `2` | 每条记录的重试轮数（直链 → Playwright → browser-act 为一轮） |
| `PUBMED_DL_COOLDOWN` | `180` | 同一前缀连续 5 次未通过后的冷却秒数 |
| `PUBMED_DL_ACT_DELAY` | `5` | 挑战型前缀每条之间的基准间隔秒数（实际 4–7 s 抖动） |
| `PUBMED_DL_WALL_BACKOFF` | `20` | 某条判定 `blocked_bot` 后，同前缀下一条前的退避秒数 |
| `PUBMED_DL_ACT_FIRST` | 空（关） | 设为 `1` 时，对已持有 clearance 的前缀优先走 browser-act（更稳但更慢） |
| `PUBMED_DL_BROWSER_ACT` | `pubmed-publishers` | browser-act 浏览器名；空串关闭 browser-act 后端 |
| `PUBMED_DL_PORT` | `9222` | `login` 使用的 CDP 端口 |
| `PUBMED_DL_DISPLAY` | `:99` | 无头 Xvfb 显示号 |

### 3.1 安装为命令 / 作为 SDK 调用

```bash
pip install -e .          # 之后 pubmed-dl 等价于 python -m pubmed_dl
```

程序化入口是 `pubmed_dl/api.py`（CLI 只是它的格式化层），不打印、不解析参数：

```python
from pubmed_dl import api

res = api.download(["10.3389/fgene.2024.1401315", "33423259"], out="downloads")
res.counts          # {'ok': 1, 'blocked_bot': 1}
res.rows            # 该批次的 state 行（status/source/pages/path/...）
res.index_csv       # downloads/index.csv

api.download_csv("data/csv-ultrasound-set.csv", "downloads", limit=5, prefix="10.3390")
api.report("downloads")                       # -> stats dict，同时刷新 index/bib/summary.md
api.probe("10.3389/fgene.2024.1401315")       # -> {status, source, detail, bytes, magic}
api.verify("downloads", min_sim=0.35)         # -> {ok, suspect, missing, low}
```

- DOI 可写裸串、`doi:` 前缀或 `https://doi.org/...`；纯数字按 PMID 处理。
- 传入 `browser=` 可复用已有 `Browser`（生命周期由调用方负责，否则内部 `start_browser` / `close`）。
- Playwright 同步 API 绑定线程：`Browser` 必须在调用线程创建与使用。
- 单条墙钟超时依赖 SIGALRM，只在主线程生效；在工作线程调用时退化为按请求 `timeout_ms` 兜底。

### 3.2 MCP 服务器（stdio）

```bash
pip install -e '.[mcp]'
pubmed-dl-mcp            # 或 python -m pubmed_dl.mcp_server
```

客户端配置（Claude Desktop / Claude Code / Codex，任意 stdio 客户端）：

```json
{
  "mcpServers": {
    "pubmed-dl": {
      "command": "/data/yangzs/Projects/05_Tools/04_AutomatedPubMed/.venv/bin/pubmed-dl-mcp",
      "env": { "PUBMED_DL_DISPLAY": ":99", "PUBMED_DL_PORT": "9222" }
    }
  }
}
```

| 工具 | 只读 | 作用 |
| --- | --- | --- |
| `pubmed_download` | 否 | DOI/PMID 列表 → PDF；返回 `counts`/`rows`（最多 50 行明细） |
| `pubmed_download_csv` | 否 | PubMed CSV 批量下载，支持 `prefix` / `only_pmcid` / `shard` / `limit` |
| `pubmed_report` | 否 | 汇总运行目录并刷新 `index.csv` / `references.bib` / `missing.csv` / `summary.md` |
| `pubmed_probe` | 是 | 单 DOI 诊断（`status` / `source` / `bytes` / `magic`），不落盘 |
| `pubmed_verify` | 是 | 复核已下载 PDF（magic / 页数 / 标题重合度） |

- 浏览器是单占位资源：并发调用第二个下载会直接返回提示错误，而不是抢 9222 端口。
- 工具内是阻塞流水线，走 `anyio.to_thread` 执行，事件循环仍能响应 ping/取消。
- stdout 是 JSON-RPC 流；服务器自身不打印。长批次用 `limit` 分批，状态文件保证断点续跑。



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
| 10.1159 | Karger | ✅ 可下载 | 挑战 ~45 s 自动通过，`Article/Pdf` 直链走浏览器上下文 |
| 10.1080 / 10.3109 | T&F | ✅ 可下载 | 挑战 ~19 s 自动通过，`/doi/pdf` 走浏览器上下文 |
| 10.1093 | OUP | ✅ 页面可达 | 挑战 ~26 s 自动通过 |
| 10.1136 | BMJ | ✅ 可下载 | 实测 1.1 MB PDF（`citation_pdf_url`） |
| 10.1001 | JAMA | ✅ 可下载 | 实测 433 KB PDF（挑战 ~40 s） |
| 10.1056 | NEJM | ✅ 可下载 | 实测 486 KB PDF（挑战 ~25 s） |
| 10.1126 | Science | ⚠️ 待复核 | 仍是 `WALLED_PREFIXES`，需 `--attempt-walled` |
| 10.1177 / 10.1148 | SAGE / RSNA | ⚠️ 待复核 | SAGE 实测连接被重置（路由问题），RSNA 未测 |
| 10.3760 | 中华医学会 (Yiigle) | ⛔ 验证码 | 需人工解锁 |
| 10.1055 | Thieme | ⛔ 连接失败 | 待查 |
| 10.1097 | LWW | ⚠️ 页面可用，PDF 需人工 | 页面解析 |

## 6. 挑战等待与人工解锁

先区分两类墙，处理方式不同：

| 类型 | 表现 | 处理 |
| --- | --- | --- |
| 托管挑战（Cloudflare managed challenge） | `Just a moment…` / `Performing security verification`，18–45 s 后自动跳文章 | 等待即可（`--wall-wait`，默认 90 s），不模拟点击、不改写页面；通过后同一上下文复用 `cf_clearance` |
| 交互式验证（ScienceDirect captcha、Yiigle `安全验证`） | 必须人工点选/输入 | 只能用 `login` 人工过一次，复用会话 |

交互式验证技术上可自动化，但属于规避访问控制，工具**不**这样做：

```bash
.venv/bin/python -m pubmed_dl login --urls https://www.sciencedirect.com/
# 在弹出的 Chrome 窗口里完成验证（必要时登录），回车结束
.venv/bin/python -m pubmed_dl run --csv ... --out downloads --retry-failed --attempt-walled
```

cookie 通常数小时到数天有效，可在窗口期内批量取回。

### 6.1 成功率优先的推荐参数

慢一点、尽量不丢条：

```bash
export PUBMED_DL_WALL_WAIT=120 PUBMED_DL_ROUNDS=3
export PUBMED_DL_ACT_DELAY=8 PUBMED_DL_WALL_BACKOFF=30
.venv/bin/python -m pubmed_dl run --csv data/csv-ultrasound-set.csv --out downloads \
  --retry-failed --attempt-walled --shard 0/10
```

- 每条：直链 → Playwright（最多 120 s/次）→ browser-act（独立 cookie 罐）→ 最多 3 轮；
- browser-act 成功后保留其暖 cookie；只有设置 `PUBMED_DL_ACT_FIRST=1` 才会优先走它（默认 Playwright 优先）；
- 连续失败给同前缀 20–30 s 退避，连续 5 次未通过才冷却 180 s；
- `not_entitled` 立即返回，不做无意义重试；ScienceDirect / Yiigle 的交互式验证仍需 `login`。

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

> 2026-11-18 复查：上表 Cloudflare 行是旧跑分（等待上限 20 s）。把等待上限提到 60 s、
> 并在 `page.goto` 超时后保留仍在渲染的标签页之后，`probe` 实测
> Karger `10.1159/000504976`、BMJ `10.1136/jmg.31.9.679`、
> T&F `10.1080/14767058.2024.2344718`、JAMA `10.1001/archdermatol.2011.124`、
> NEJM `10.1056/NEJMoa1203382` 全部返回 `ok`（`%PDF`，0.4–1.6 MB）；
> 10.1001 / 10.1056 已从 `WALLED_PREFIXES` 移除。

## 8. 网络连通性回退

Spandidos、Via Medica、SciELO、Korean J Med Sci 等站点在校园网出口直接不可达（连接超时），
但它们本身是开放获取。工具对这类前缀（`PROXY_FALLBACK_PREFIXES`）会改用本机 VPN
（`HTTPS_PROXY`）解析页面并取 PDF——只是绕开路由问题，不涉及订阅权限。
实测：Spandidos 15/15、Via Medica 15/15、SciELO 13/22、KJMS 7/8。

## 9. 已知限制

- 无 DOI 的记录无法直接解析到出版社；可用 `pubmed_dl.backfill` 做 Crossref 题名回填，
  但 PubMed 里就缺 DOI 且 Crossref 也匹配不到的部分（1960–1980 年代、部分中文刊）只能跳过。
- 订阅不覆盖的标题会返回 `not_entitled`，这是权限问题，不是抓取问题。
- 托管挑战只等待其自行通过（默认 90 s），交互式验证（ScienceDirect / Yiigle）不做自动破解；
  需要人工过一次后用 `login` 复用会话。仍对连续未通过的同一前缀做可配置冷却
  （`PUBMED_DL_COOLDOWN`，默认 180 s）。
- 个别出版社 PDF 走境外对象存储，速度可能低至 20 KB/s；已用 Range 断点续传兜底。
- SAGE 等站点的中国镜像（`sage.cnpereading.com`）只给元数据不给 PDF，已列入 `DEAD_HOSTS` 快速失败。
- 并发数受 `/tmp` 体积限制：十几条流同时跑必须把 `PUBMED_DL_PROFILE_BASE` 指向磁盘。
