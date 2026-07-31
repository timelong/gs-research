# gsr —— 投行研报抓取与中译工具

抓取头部投行公开研报，翻译成简体中文 Markdown，PDF 原文单独归档。
首个接入源为高盛（gspublishing.com）。

## 先读这段：站点的四个实测事实

这四点决定了整个架构，动手前务必理解。都是拿真实页面验证过的，不是推测。

**1. 内容是公开的，但站点有 Cloudflare 挑战。**

研报本身无需登录，浏览器直接可看可下。但全站挂了 Cloudflare managed
challenge——用 `requests` / `httpx` 直接请求（包括请求 `.pdf`）一律返回
403 "Just a moment..."。浏览器能访问是因为它执行了 JS 挑战并拿到了
`cf_clearance` cookie。

所以抓取层建立在 Playwright persistent context 上：固定 profile 目录，
`cf_clearance` 持久化复用，首次 headful 过一次挑战之后可长期免挑战。
**PDF 下载也走 `page.request`**（继承页面 cookie），另起 HTTP 客户端会因缺
cookie 而 403。

**2. 列表页和正文都是静态 HTML，不存在需要逆向的列表 API。**

`/content/public.html` 把研报条目直接嵌在首屏文档里，页面内另有一份
结构化 JSON（含 `distributionHeadline` 等字段）。研报详情页也是静态正文。

这意味着翻译链路可以完全绕开 PDF——直接拿干净 HTML 正文，段落、标题、
列表、表格结构天然保留，不用跟 PDF 的分栏和图表排版较劲。PDF 只作归档副本。

研报 URL 形态固定：

```
https://www.gspublishing.com/content/research/en/reports/YYYY/MM/DD/<uuid>.html
                                                                    <uuid>.pdf
```

**3. 页面显示的日期会比实际发布日晚一天（如果你在东八区）。**

实测同一篇研报：

| 来源 | 值 |
|---|---|
| `publicationDateTime` | `1783699236000` = 2026-07-10 16:00 UTC |
| 实际发布时点 | 2026-07-10 12:00 ET |
| URL 路径 | `/2026/07/10/` |
| 页面显示（UTC+8 渲染） | `11 Jul 2026 \| 12:00am` ← 晚一天 |

页面时间是按浏览器本地时区渲染的。所以**日期一律取自 URL 路径**，
展示文本只在 URL 解不出日期时兜底。这条有专门的回归测试守着。

**4. 首屏只有 10 条，翻页靠 `search.html?page=N`，公开库共 654 篇 / 27 页。**

`public.html` 没有"加载更多"按钮，但页面里内嵌了一个搜索页链接带
`page=` 参数——那就是翻页入口，比模拟滚动可靠得多。参数已原样抄进
`goldman.yaml` 的 `query_template`（保持原编码不动，只把 page 参数化）。

已实测：`search.html` **每页 25 条**，翻页正常工作。页面自己会写出
结果总数和总页数（`Search Results: 0 - 25 of 654`、`1 of 27`），
程序会读出来用于日志和收紧翻页上限。

因为 `sort=time` 是降序，做了**日期早停**：一旦某页最旧条目已早于
`--since`，后面只会更旧，直接停止翻页。抓"今年以来"只需翻 2 页
（第 1 页覆盖到 2 月，第 2 页就跨到上一年了），省掉 25 页无用请求，
也就是少惹 Cloudflare。

## 安装

```bash
cd gs-research
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

国内网络装不上时换镜像：

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
  --timeout 120 --retries 10
```

### 浏览器内核

默认配置 `browser.channel: "chrome"`，**直接用系统已装的 Google Chrome，
不需要额外下载任何东西**。装了 Chrome 就可以跳过这一节。

附带好处：真实 Chrome 的指纹比 Playwright 自带 Chromium 更"正常"，
过 Cloudflare 挑战反而更顺。

如果想用 Playwright 自带内核，把 `browser.channel` 留空，然后：

```bash
# 官方 CDN 在国内基本下不动，配个镜像
export PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
playwright install chromium          # ~150MB
```

Chrome 装在非默认位置的话，填 `browser.executable_path`（绝对路径）。
浏览器启不起来时程序会打出具体该怎么改，不用猜。

## 大模型配置

配置分两处：**`config/config.yaml`** 选模型和调参数，**环境变量**放 API key
（不写进配置文件，避免误提交到 git）。

### 1. 选用哪个模型

`config/config.yaml` 的 `translate` 段：

```yaml
translate:
  enabled: true
  provider: "deepseek"        # ← 改这里换模型，值 = 下面 providers 里的 key 名
  target_lang: "简体中文"
  chunk_chars: 4000           # 每块字符数。太大模型容易掉内容，太小上下文断裂
  chunk_overlap: 200          # 块间重叠字符数，帮模型衔接上下文
  concurrency: 2              # 并发翻译的块数。API 限流严就设 1
  glossary_file: "./config/glossary.json"
  keep_figure_placeholders: true   # 译文里保留 [图表：...] 占位标记
```

### 2. 各模型的参数

同文件 `translate.providers` 下，已预置五个：

```yaml
  providers:
    deepseek:
      kind: "openai"          # 兼容 OpenAI 协议，复用同一份实现
      base_url: "https://api.deepseek.com/v1/chat/completions"
      model: "deepseek-chat"
      api_key_env: "DEEPSEEK_API_KEY"   # ← key 从这个环境变量读
      max_tokens: 8192
      temperature: 0.2        # 翻译任务要低温，别调高
    claude:
      kind: "anthropic"       # 协议不同，单独一个实现
      base_url: "https://api.anthropic.com/v1/messages"
      model: "claude-sonnet-5"
      api_key_env: "ANTHROPIC_API_KEY"
    openai:      ...
    qwen:        ...          # 通义，DASHSCOPE_API_KEY
    zhipu:       ...          # 智谱，ZHIPU_API_KEY
    modelscope:  ...          # 魔搭 API-Inference，MODELSCOPE_API_KEY
```

改 `model` 字段可换具体型号（如 `deepseek-chat` → `deepseek-reasoner`）。

**`base_url` 要写完整端点路径**，不是 SDK 用的根地址。各家官网示例给的
往往是根地址，需要自己补后缀：

| 官网示例给的 | 配置里要写 |
|---|---|
| `https://api.deepseek.com/v1` | `https://api.deepseek.com/v1/chat/completions` |
| `https://api-inference.modelscope.cn/v1` | `https://api-inference.modelscope.cn/v1/chat/completions` |

Anthropic 那种 `kind: "anthropic"` 的除外，它的端点是 `/v1/messages`。

**`use_system_proxy` 是国内环境的关键项。** httpx 默认会自动接管环境变量里
的代理（`HTTPS_PROXY` / `ALL_PROXY`），这会把国内 API 也绕到境外代理去——
慢，而且常直接报错（比如挂了 SOCKS 代理但没装 `socksio`）。

所以每个 provider 都显式声明：

| provider | `use_system_proxy` | 原因 |
|---|---|---|
| claude / openai | `true` | 境外，需要代理 |
| deepseek / qwen / zhipu / modelscope | `false` | 国内，直连 |

碰到代理相关报错时程序会直接给出该改哪里，不会闷头重试。

### 3. API key

变量名由该 provider 的 `api_key_env` 指定：

```bash
export DEEPSEEK_API_KEY=sk-xxx       # 默认 provider
# 按需选其一
export ANTHROPIC_API_KEY=sk-ant-xxx
export OPENAI_API_KEY=sk-xxx
export DASHSCOPE_API_KEY=xxx         # 通义
export ZHIPU_API_KEY=xxx             # 智谱
export MODELSCOPE_API_KEY=ms-xxx     # 魔搭，令牌在「首页 → 访问令牌」里拿
```

没设置的话运行时会直接告诉你缺哪个变量，不会跑到一半才失败。

### 4. 临时切换，不改配置

```bash
python -m gsr translate --provider claude
```

### 5. 加一个新模型

对方兼容 OpenAI 协议的话，在 `providers` 下照抄一段，改
`base_url` / `model` / `api_key_env` 就行，**代码不用动**。
协议不兼容的才需要去 `gsr/translate/providers.py` 加一个类
（照 `AnthropicProvider` 的样子写，实现一个 `complete()` 方法）。

### 6. 术语表比换模型更重要

`config/glossary.json` 是金融术语表：

```json
{
  "overweight": "超配",
  "backwardation": "现货溢价",
  "terminal rate": "终端利率",
  "breakeven": "盈亏平衡通胀率"
}
```

翻译时**只把该分块里实际出现的词条注入 prompt**（避免 prompt 无谓膨胀），
保证同一术语在全篇及跨研报之间译法一致。

这个对译文质量的影响比换模型更直接——像 backwardation / contango 这类词，
模型不给约束经常译成"逆价差""正价差"之类不统一的说法。建议按你关注的
领域先补十几条。

## 用法

```bash
# 先看解析器在真实页面上是否工作（不联网，强烈建议第一步做这个）
#   浏览器里打开 public.html，Cmd+S 存成 HTML，然后：
python -m gsr parse-test ~/Downloads/public.html

# 只发现列表并入库，不下载。先跑这个确认抓到的是对的东西。
python -m gsr discover --since ytd

# 发现 + 下载正文和 PDF
python -m gsr fetch --since ytd

# 翻译已下载的
python -m gsr translate --limit 10

# 一条龙
python -m gsr run --since ytd --limit 20

# 查看和统计
python -m gsr list --since 30d
python -m gsr list --status failed -v      # -v 显示具体错误
python -m gsr list --keyword commodities
python -m gsr status

# 失败处理：先看原因，再重置计数重跑
python -m gsr retry
```

`--since` 支持的写法：

| 写法 | 含义 |
|---|---|
| `ytd` | 今年 1 月 1 日起（默认） |
| `today` / `yesterday` | 今天 / 昨天 |
| `7d` `30d` `90d` | 往前 N 天 |
| `2w` | 往前 N 周 |
| `3m` `6m` | 往前 N 个月 |
| `1y` | 往前 1 年 |
| `2026-01-01` | 具体日期 |
| `2026-03` | 该月 1 日 |
| `all` | 不限 |

配 `--until` 可以取任意闭区间：`--since 2026-01-01 --until 2026-03-31`。

## 首次运行注意

1. **`browser.headless` 保持 `false`**。第一次跑必须能看到浏览器窗口——
   如果 Cloudflare 弹出验证码，需要你手动点一下。过了之后 `cf_clearance`
   就存进 profile 了。
2. **`.browser-profile/` 目录不要删**。删了就要重新过挑战。
3. **别调快限速**。`fetch.min_interval_sec` 默认 6 秒 + 随机抖动。
   Cloudflare 对高频访问会从 managed challenge 升级到硬验证码，
   而验证码只能人工过。为了快改小这个值是负收益。
4. **先用小 `--limit` 试**（比如 `--limit 3`），确认整条链路通了再放量。

## 输出结构

```
data/
  goldman/2026/07/
    2026-07-10_Global Strategy Paper..._ce510cb7.raw.html      # 原始页面
    2026-07-10_Global Strategy Paper..._ce510cb7.original.md   # 提取的英文正文
    2026-07-10_Global Strategy Paper..._ce510cb7.zh.md         # 中文译文
    2026-07-10_Global Strategy Paper..._ce510cb7.pdf           # PDF 归档
  reports.db                                                    # 元数据与状态
```

译文顶部保留 front matter（标题、日期、原文链接、PDF 链接），
并带一行机翻声明。

## 架构

```
config/
  config.yaml            全局配置（限速、翻译、provider）
  sources/goldman.yaml    站点配置（URL 模式、选择器、分页、正文提取）
  glossary.json           金融术语表，翻译时注入 prompt
gsr/
  cli.py                  命令行入口
  config.py               配置加载
  models.py               ReportMeta / FetchResult
  storage.py              SQLite：元数据、去重、状态机
  browser.py              Playwright persistent context + 限速 + 挑战检测
  parsing.py              内嵌 JSON 挖掘、日期宽松解析、正文清洗
  daterange.py            --since / --until 解析
  adapters/
    base.py               适配器基类 + 注册表
    goldman.py            高盛实现（三级降级解析）
  translate/
    providers.py          provider 适配层（OpenAI 兼容 / Anthropic）
    chunker.py            Markdown 结构感知分块
    translator.py         翻译编排
tests/
  test_parsing.py         81 项自检，不联网不需要 Playwright
```

### 三级降级解析

列表解析按顺序尝试，前一级拿到结果就停：

| 策略 | 数据来源 | 字段丰富度 | 抗改版能力 |
|---|---|---|---|
| 策略 | 适用页面 | 数据来源 | 字段丰富度 | 抗改版 |
|---|---|---|---|---|
| `embedded_json` | public.html | 内嵌 JSON | 高（+受限标记） | 中 |
| `search_table` | search.html | `SearchResults__*` 表格 | **最高（含摘要）** | 中高 |
| `dom_testid` | public.html | `data-testid` | 中 | 中高 |
| `href_regex` | 两者皆可 | URL 正则全文扒 | 低（链接/标题/日期） | 极高 |

**两个页面结构完全不同**，这点花了点功夫才发现：`public.html` 是卡片列表 +
内嵌 JSON，`search.html` 是表格。实际抓取走的是 `search.html`（因为要翻页），
所以 `search_table` 才是主力策略。

`search.html` 的 class 名是语义化的 `SearchResults__colDate`、
`SearchResults__headline` 这种 BEM 风格，**可以安全依赖**——
和 `public.html` 上 `gs-uitk-c-hf7351` 那种编译 hash 是两回事。

它的元数据也最全，比 `public.html` 多一段**摘要摘录**
（`SearchResults__colExtract`），还带一个原始毫秒时间戳
（`SearchResults__hiddenEl`，绕过了展示文本的时区问题）。

**绝不依赖 class 名**。页面上的 `gs-uitk-c-hf7351--text-root` 之类是编译
产生的 hash，改版必失效。`data-testid` 是测试锚点，相对稳定。

实测确认的内嵌 JSON 字段（`goldman.yaml` 的 `json_field_map` 已按此配置，
并保留了备选名以应对改名）：

```json
{
  "distributionHeadline": "Global Strategy Paper: Balancing Innovation...",
  "path": "/content/research/en/reports/2026/07/10/ce510cb7-....html",
  "totalPages": 35,
  "leadAuthor": "Christian Mueller-Glissmann, CFA",
  "hasMultipleAuthors": true,
  "sourceDisplayName": "Research | Portfolio Strategy",
  "publicationDateTime": 1783699236000,
  "restrictionDetails": null,
  "securityRestrictionMap": null
}
```

注意列表页**不提供摘要**，`summary` 只能从详情页正文取。
`restrictionDetails` / `securityRestrictionMap` 非 null 表示该研报有访问
限制，会被标记到 `restricted` 字段，抓不到正文时便于排查。

### 状态机与断点续跑

```
discovered ──fetch──> fetched ──translate──> translated
   status 只表示进度，单调前进，失败不会把它改回去

失败另记一组字段：fail_count / failed_stage / failed_at / last_error
```

**关键设计：失败信息与 status 解耦。** 早先版本失败时把 status 改成
`failed`，结果翻译失败的条目掉出了"待翻译"队列（那个队列只查
`status='fetched'`），反而被"待下载"队列捞走——失败一次就再也重试不了
正确的那一步。现在 status 留在原进度上，重试天然落回正确队列。

所以**失败后直接重跑同一条命令即可**，不需要任何额外操作：

```bash
python -m gsr translate            # 上次翻译失败的会自动重试
python -m gsr fetch --since ytd    # 上次下载失败的会自动重试
```

连续失败超过 `fetch.max_fail_retries`（默认 3）次的条目会被跳过，
避免卡在坏条目上空转。要放开：

```bash
python -m gsr list --status failed -v   # 先看失败原因
python -m gsr retry                     # 重置失败计数
python -m gsr translate --retry-failed  # 或单次强制重试
```

成功后会清空该条的失败痕迹（含计数），彻底回到正常轨道。

中途 Ctrl-C 不丢进度。去重按 `source:uuid` 主键，重复入库只补齐空字段、
不覆盖已有值——避免解析退化把好数据冲掉。

数据库会自动就地升级（补列、把历史 `status='failed'` 按落盘产物反推回
正确进度），不需要删库重建。

### 翻译层结构

```
translate/
  providers.py    provider 适配层：OpenAICompatProvider（DeepSeek/通义/智谱/
                  OpenAI 共用）+ AnthropicProvider。统一用 httpx 打 HTTP，
                  不引各家 SDK —— 少四五个依赖，且重试/超时语义能自己控。
  chunker.py      结构感知分块：优先在标题边界切，其次空行段落，最后句末标点。
                  绝不切在句子中间 —— 切坏了模型会补全或漏译。
  cache.py        分块级缓存，中途失败不作废已译内容（见下）
  translator.py   编排：分块 → 并发调用 → 拼回。front matter 原样保留，
                  只翻正文；输出顶部加一行机翻声明。
```

具体配置见上面的「[大模型配置](#大模型配置)」。

### 正文清理

HTML 版是个阅读器界面，带一整套交互控件：章节目录、PDF/分享按钮、字号菜单、
音频朗读播放器、图标字体。这些**不在 `nav`/`footer` 里**，只删那几个标签
清不掉，会原样进到译文里——模型会一本正经地把 "Font Size"、
"Listen to report" 翻成中文，还白占掉第一个分块的大半额度。

分两层清理：

**节点层**（`strip_selectors`）——按属性剔除，基于实测存在的属性而非
猜测的 class 名：`[data-gs-uitk-component="icon"]`、`[role="img"]`、
`[aria-hidden="true"]` 负责图标字体（那些 `×` `▼` `{}` `check` 字形），
`button`/`select`/`audio`/`svg` 负责控件，`[class*="toolbar" i]` 之类
负责工具栏和目录。

**文本层**（`reader_chrome`）——节点层清不掉的残留（有些控件文字不在
独立节点里）按文本规则再清一遍：整行等于已知标签、只含一个指向
`#chapter`/`mailto:`/`.pdf` 的链接、音频进度 `0:00/12:39`、报头元信息行
（`28 June 2026 | 7:06PM EDT | Research | …`，这些字段 front matter 里都有）。

规则刻意保守，只删明确是界面元素的行——正文段落不可能长这样。有专门的
反例测试：句中的 "more"、"The PDF version contains…"、"Table 3 shows…"、
正文里的普通链接都不会被误删。

最后用元数据里的标题生成规范 H1，并去掉开头那些已被标题涵盖的报头碎片。
实测这一整套清掉约 31% 的字符，全是噪音。

### 分块缓存（省 token 的关键）

一篇 35 页研报要切 25 个分块。如果第 6 块失败，整篇就失败——**前 5 块
花掉的 token 全部作废**，重试从头再来。研报越长越亏。

所以每块译完立刻落盘到 `<输出文件>.parts.json`，重试时命中缓存的块直接
复用，只补没译成的那些。整篇成功后缓存文件自动删除。

缓存 key = 分块原文 + provider + model 的哈希。换模型或原文变了会自然失效，
不会拿旧译文糊弄。附带好处：同一篇里重复出现的段落只翻一次。

用 `translate.cache_blocks: false` 可关掉。

### 响应解析与重试分类

各家兼容实现的返回结构差异不小，而且经常在 HTTP 200 里夹带错误对象
（限流、余额不足、token 无效）。所以取正文时每一层都显式检查，报错必带
原始响应片段。

关键是**区分瞬时故障和配置问题**——前者该退避重试，后者重试只浪费额度：

| 情形 | 判定 | 处理 |
|---|---|---|
| 空信封（`choices=null`、`usage` 全 0、`object`/`created` 为空） | 瞬时 | 退避重试，最多 `response_retries` 次 |
| `content` 为空且无 `finish_reason` | 瞬时 | 退避重试 |
| `finish_reason=length`（截断） | 配置 | 不重试，提示调大 `max_tokens` 或减小 `chunk_chars` |
| 只有 `reasoning_content`（思考型模型） | 配置 | 不重试，提示换非思考模型 |
| `content_filter` | 策略 | 不重试，提示换 provider |
| 显式 error 对象 | 配置/额度 | 不重试，原样透出服务端消息 |

**空信封**是魔搭免费额度限流的典型表现：HTTP 200、`choices: null`、
`usage` 全为 0。`usage.prompt_tokens=0` 说明服务端连输入都没计数，
所以与请求内容无关，纯粹是服务端侧的问题。

### 并发

`translate.concurrency` 是全局值，各 provider 可以单独覆盖：

```yaml
    modelscope:
      concurrency: 1        # 免费额度限流紧，并发 2 会触发空信封
      response_retries: 4
```

魔搭已默认设为 1。

### 新增研报源

1. 写 `config/sources/<name>.yaml`
2. 在 `gsr/adapters/` 加一个继承 `BaseAdapter` 的类，实现 `list_reports`
   和 `fetch_report`，加 `@register` 装饰器
3. 把 `<name>` 加到 `config.yaml` 的 `sources` 列表

主流程不需要任何改动。

## 自检

```bash
# 合成用例：81 项，覆盖三级解析、降级行为、日期过滤、--since 各种写法、
# 正文提取与免责声明截断、分块内容守恒、存储去重与状态流转、
# 内嵌 JSON 的多层转义鲁棒性
python -m tests.test_parsing

# public.html 回归：40 项，固化内嵌 JSON 字段名、多策略一致性、
# 日期不受时区偏移影响
python -m tests.test_real_page ~/Downloads/public.html

# search.html 回归：42 项，固化表格结构、元数据完整度、
# 不退化到 href_regex、总数/总页数读取、metaText 拆分边界
python -m tests.test_real_search ~/Downloads/Search.html

# 浏览器层逻辑：20 项，用假 page 对象，不启真实浏览器。
# 覆盖 networkidle 超时容忍、ready_selector 等待、Cloudflare 挑战识别
python -m tests.test_browser_logic

# provider 层：40 项，不发真实请求。
# 覆盖代理策略（国内直连/境外走代理）、端点后缀、环境类报错的提示
python -m tests.test_providers

# 失败重试与状态机：49 项。
# 覆盖失败后留在正确队列、失败次数上限、成功后清痕、老库迁移、
# 队列为空时的诊断信息不误导
python -m tests.test_retry

# 正文清理：50 项。素材是真实 original.md 里的界面噪音原文。
# 两条底线：噪音必须清掉、正文一个字不能少
python -m tests.test_body_cleanup

# 响应解析健壮性 + 分块缓存：71 项。
# 覆盖各种畸形响应的可读报错、瞬时故障与配置问题的重试分类、
# 空正文诊断、缓存续跑省 token
python -m tests.test_response_and_cache
```

八个都不联网、不发 API 请求、不需要 Playwright 内核，共 393 项。

真实页面回归测试值得单独说：它把「页面显示 07-11、正确答案是 07-10」
这个坑固化成了断言。这类时区 off-by-one 一旦回退，抓取结果会静默错位一天，
而且很难发现——所以专门守住。

## 踩过的坑

**队列为空时的提示误导人。** 原来只要队列空就打印"先跑 `gsr fetch`"，
但真实原因常常是失败次数已达上限被跳过——错误的提示会让人以为数据丢了。
现在 `queue_diagnosis()` 会查库说明真实情况：库里有多少篇、卡在哪一步、
最近的失败原因是什么、该用哪个命令恢复。

**把瞬时故障当成致命错误。** 魔搭限流时返回 HTTP 200 + `choices: null`
的空信封，原本直接判定整篇翻译失败。而 `usage.prompt_tokens=0` 恰恰
说明服务端连输入都没计数，是它自己的问题，退避重试就能过。

**`if cache:` 让缓存永远不生效。** `BlockCache` 定义了 `__len__`，
空缓存 `len()==0`，于是 `if cache:` 判定为假——而缓存为空正是首次运行的
情形，结果缓存一次都没被写入过。修法：判断统一用 `is not None`，
并给类显式加 `__bool__` 返回 True 堵住这个坑。

这类 bug 单看代码很难发现（读起来完全正常），靠的是测试断言"重试时
只调用了 N-1 次模型"才暴露出来。

**失败状态覆盖了进度状态，导致失败无法重试。** 原本失败时把 `status` 改成
`failed`，但两个队列的判据是 `status='discovered'` 和 `status='fetched'`——
一旦被改成 `failed`，条目就同时掉出了两个队列的正确归属：翻译失败的会被
"待下载"队列捞走，而"待翻译"队列再也看不到它。表现就是翻译失败一次后
提示"没有待翻译的研报"。

修法是把两件事拆开：`status` 只记进度、单调前进；失败单独记
`fail_count` / `failed_stage` / `last_error`。这条有 `test_retry` 守着，
包括老库的自动迁移。


**`wait_until: networkidle` 会必然超时。** 站点挂了 Datadog RUM 和
LaunchDarkly，持续发埋点请求（`rum?ddsource=` / `logs?ddsource=`），
网络永远不会空闲，等待条件一直不满足，60 秒后超时，三次重试全废。
但研报列表是服务端直出的静态 HTML，DOM 一就绪内容就全在了。

所以：`wait_until` 固定为 `domcontentloaded`，再用站点配置里的
`ready_selector` 等关键容器出现。并且**超时后先检查内容是否已就绪，
就绪就继续处理**——否则每次都白等三轮，还多惹三次 Cloudflare。
这条有 `test_browser_logic` 守着。

## 待实测确认

列表环节已全部跑通验证。剩下的是**详情页链路**：正文提取、免责声明截断、
PDF 下载。这部分逻辑有单测覆盖（用合成 HTML），但没在真实研报页上跑过。

先小批量试：

```bash
python -m gsr fetch --since ytd --limit 2
```

如果正文提取不理想（比如 `content_selectors` 没命中真实容器，
退化成了整页文本），把某篇研报页存下来发出来，同 `parse-test` 的思路排查。

翻页环节的防御措施（已生效，无需担心）：

- 某页解析不出条目 → 停止翻页
- 某页内容与前页重复（翻页参数失效的典型表现）→ 停止翻页
- 读到总页数后 → 到最后一页自动停止
- `page_param` 整体失败 → 自动回退到 `public.html` 滚动加载
- `max_pages` 默认 40 页上限兜底

## 边界

- 只抓公开可见内容。需要机构客户登录才能看的部分不在范围内。
- 抓取前请自行确认符合网站服务条款。限速配置保守是出于这个考虑，
  也是为了不触发硬验证码。
- 机器翻译仅供快速理解，关键结论请核对原文 PDF。
