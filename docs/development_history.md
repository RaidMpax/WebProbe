# WebProbe

> 历史开发记录：本文保留了开发期间的阶段说明、旧版运行指令和实验复盘；其中“当前”指对应记录日期的版本。当前产品范围、安装方式和结果请以仓库根目录的 README 为准。

阶段 A 已练习搜索、导航和动态加载；B1–B4 已将浏览器工具、固定规则循环和模型决策循环逐步接通。当前四套 TestLab 固定数据集共 30 条 Case；搜索、反馈表单和购物车都提供可选模型循环，登录暂时使用规则循环。购物车模型路径已有离线假响应测试，但还没有新的真实 Gemini 评测结果；真实 Gemini 历史结果与限制记录在后文。

## 本地快速启动

项目使用 Python 3.11+、PowerShell 和项目内 Chromium。第一次运行时，在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.browsers'
.\.venv\Scripts\python.exe -m playwright install chromium
```

以后启动界面只需：

```powershell
.\run_webprobe.ps1
```

启动脚本将 Streamlit 绑定到 `127.0.0.1`，只允许本机浏览器访问。界面会显示本地地址。先到“运行测试”选择一个 TestLab Case、执行策略和步骤/时间预算，即可在不配置 API Key、不调用外部模型的情况下运行规则策略。购物车规则评测可在项目根目录另开 PowerShell 执行：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.browsers'
.\.venv\Scripts\python.exe -m webprobe.cart_evals --policy rule
```

这会运行购物车的 7 条固定 Case，结果保存在 `artifacts/cart_evals/<run_id>/`，包括逐题结果、截图和 Playwright Trace。完整项目测试命令见下方“本地运行”。选择“模型策略”会向 `.env` 配置的服务发送真实请求；离线规则演示不需要 Key。

## 本批产品规格（固定预期）

- 页面初始展示“蓝色茶杯”和“帆布包”。
- 输入“茶杯”并点击“搜索”后，只展示“蓝色茶杯”，并显示“找到 1 件商品”。
- 点击“蓝色茶杯”进入详情页，页面标题和商品名正确显示，价格为 ¥39；浏览器返回后回到搜索页。
- 详情页的库存先显示“加载中”，约 1.2 秒后显示“库存：有货”；验证允许最多等待 5 秒。
- 这个预期由项目规格定义，不能由被测页面或未来的 Agent 自行改写。
- 搜索可由 `delay_ms=1200` 模拟正常延迟：等待期间显示“搜索中…”，完成后仍应只显示“蓝色茶杯”；等待期间的空列表不是故障。

### 购物车 TestLab 规格与评测

- `lab/cart.html` 初始购物车为空，商品件数与总价均为 0；蓝色茶杯单价 ¥39，帆布包单价 ¥59。
- 加入 1 个茶杯和 1 个帆布包后，购物车应显示两种商品各 1 件、总件数 2、总价 ¥98；重复加入会增加对应数量。
- 评测可在新浏览器上下文注入 `window.__WEBPROBE_CART_TOTAL_BUG__`，使总价错误地按茶杯单价计算。该故障不通过 URL 传递，也不影响 Ground Truth 之外的正常页面。
- `cart_v1.jsonl` 有 7 条固定 Case，覆盖不同商品组合/数量、两条总价故障和正常对照。Ground Truth 由 `cart_dataset.py` 按固定价格校验，不传入执行循环。
- `cart_loop.py` 按指定数量加商品；页面总价异常时重新打开干净页面并重复操作。只有两轮商品与数量都正确、总价都错，才报告 `wrong_cart_total`。Baseline 只观测一轮，异常保留为待复核。
- `cart_evals.py` 独立核对商品数量、商品行和总价，按 Case 汇总混淆矩阵，并保存截图、Playwright Trace、JSONL 轨迹和数据/策略源码哈希。除规则与 Baseline 外，也提供购物车模型策略：模型提出浏览器动作，Python 核对状态；错价必须由 Python 重开本地页面并重放相同商品后再次出现，才形成疑似 Bug。`tests/test_cart_model_loop.py` 用预写响应和真实本地浏览器测试这条路径，不会请求模型 API，也不构成真实模型成绩。
- `tests/test_cart.py` 校准本地页面的实际行为；`tests/test_cart_evals.py` 校验固定答案、故障复现、Baseline 未分类行为和证据产物。

2026-11-12 的历史运行使用购物车四题旧版数据集；该成绩不代表后来扩展的七题版本。旧运行目录保留当时的数据副本和哈希。

也可以从命令行运行购物车 Evals：先以 `--policy baseline` 跑旧策略，再把其 `summary.json` 路径传给当前策略的 `--baseline-summary`。两次运行共用 `cart_v1.jsonl`，产物各自保存在新的 `artifacts/cart_evals/<run_id>/` 目录；只有完整数据集运行才能生成版本对照。

要用模型策略只验证一题，可运行 `python -m webprobe.cart_evals --policy model --case-id cart_wrong_total_001`。它会读取项目 `.env` 并真实调用配置的模型服务；这不同于上面的离线假响应测试，也不能代表七题整套结果。确认服务商账户与额度后再运行；本次开发没有触发此 API 请求。

### 登录 TestLab 规格与评测

- `lab/login.html` 只使用虚构账号 `qa_student` / `qa_demo_123`，没有真实认证服务或网络请求。
- `login_v1.jsonl` 有 6 条固定 Case：正确登录、错误账号/密码拒绝、两条正确凭据故障题，以及按钮文案变化。
- `login_loop.py` 用真实浏览器状态断言登录结果。正确凭据被拒绝时，当前策略重新打开干净页面再登录；只有第二次也被拒绝才报告疑似故障。密码不写入常规页面观察 JSON。
- `login_evals.py` 独立读取 Ground Truth 并保存截图、Trace、逐题轨迹、混淆矩阵和哈希；Baseline 与当前策略可用 `--baseline-summary` 对照。登录循环目前是规则策略，尚未接入 LLM 决策。

2026-11-12 的历史运行使用登录四题旧版数据集；该成绩不代表后来扩展的六题版本。旧运行目录保留当时的数据副本和哈希。

### 反馈表单的固定规格

- 从搜索页的“反馈表单”链接可进入 `lab/feedback.html`。姓名和反馈内容都必填；空着任一项点击提交，页面应提示“请填写所有必填项”，提交次数保持 0。
- 两项都填写后点击提交，页面显示“提交成功”，提交次数变为 1。表单只在本地页面内模拟提交，不向服务器或外部地址发送数据。
- 表单评测在每个独立的浏览器上下文里用 Playwright 初始化脚本配置预设故障：浏览器跳过必填校验，空表单也显示提交成功。正常案和故障案均打开同一个干净的 `lab/feedback.html` URL；故障标签仅由评测器掌握。`tests/test_feedback.py` 独立验证两种状态并检查重新加载后的初始状态。
- TestLab 还可只把提交按钮的文字从“提交反馈”改为“发送反馈”，提交规则保持不变。这是正常页面变化，Ground Truth 为无 Bug；表单循环从当前页面观察读取按钮名称后再点击。
- 搜索与表单使用不同的固定数据集和评测入口，成绩不能直接混在一起。搜索评测的 URL 仍包含故障开关或延迟参数，因此目前不宜用它声称真实模型的盲测能力。

表单的 `evals/datasets/feedback_v1.jsonl` 保留原始三案，`feedback_v2.jsonl` 保留八案，供旧实验复查；当前 `feedback_v3.jsonl` 有十三条人工固定 Case，增加不同输入、单个必填字段缺失和按钮文案变化场景。四条故障题属于同一个必填校验故障家族，并不是四种独立 Bug。`webprobe/form_dataset.py` 检查每条 Case 的输入、产品规格、故障标签和预算是否自洽；`tests/test_form_dataset.py` 为每条 Case 启动新的浏览器上下文，校准 TestLab 的实际表现。这是 **Ground Truth 校准**，不算 Agent 成绩。运行校准测试：

2026-11-16 扩展到当前 30 题后，实际依次运行了四套规则 Baseline/当前策略对照。搜索为 3/4 → 4/4（Baseline 有 1 题未形成结论）；反馈表单为 9/13 → 13/13，当前版 TP=4、FP=0、FN=0、TN=9；购物车为 5/7 → 7/7，当前版 TP=2、FP=0、FN=0、TN=5；登录为 4/6 → 6/6，当前版 TP=2、FP=0、FN=0、TN=4。四套当前版结果都来自固定规则循环，不是 LLM 评测。正例是同一故障家族的重复用例，样本规模仍小；这些结果只说明当前 TestLab 规格下的行为，不证明对未知网站的泛化。对照文件：[搜索](artifacts/evals/20261116T022050Z_9292f7b9/comparison.json)、[表单](artifacts/form_evals/20261116T022129Z_d599798f/comparison.json)、[购物车](artifacts/cart_evals/20261116T022157Z_43ac3e4a/comparison.json)、[登录](artifacts/login_evals/20261116T022211Z_fee12f9b/comparison.json)。

```powershell
.\.venv\Scripts\python.exe -m pytest -v tests/test_form_dataset.py -p no:cacheprovider --basetemp .tmp\form-calibration
```

## 本地运行

在本目录的 PowerShell 中：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PLAYWRIGHT_BROWSERS_PATH = (Join-Path (Get-Location) '.browsers')
.\.venv\Scripts\python.exe -m playwright install chromium
$testTemp = Join-Path (Get-Location) (".tmp\pytest-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $testTemp | Out-Null
.\.venv\Scripts\python.exe -m pytest -q --basetemp $testTemp
streamlit run streamlit_app.py
```

依赖与 Chromium 已安装后，日常启动可直接运行 `.\run_webprobe.ps1`。脚本会进入项目目录、使用项目内 Chromium，并把 Streamlit 限制在 `127.0.0.1`；规则测试不需要 API Key，选择模型策略时才会读取项目根目录的 `.env`。

测试直接打开本地 HTML 文件，不访问外部网站。首次安装依赖和浏览器需要网络。运行产物保存在 `artifacts/`：搜索截图、详情页加载完成截图及导航状态记录 `navigation.json`。

## 本地演示界面

运行 `streamlit run streamlit_app.py` 后可在“运行测试”页选择搜索、表单、购物车或登录 Case，并为单题调整浏览器动作数和时间预算。模型策略最多 13 个浏览器动作，预留一次停止/报告调用后不会超过本地每分钟 14 次的 Gemini 请求预留上限。搜索、表单和购物车可选规则或模型策略；登录提供离线规则策略与不复查 Baseline。“测试报告”展示断言、实际预算、浏览器观察、截图及 Trace；Evaluation Dashboard 可选择四套固定数据集并查看逐题结果与规则版本对照。单题结果写入 `artifacts/ui_runs/`，各功能 Evals 分别保存到 `artifacts/evals/`、`artifacts/form_evals/`、`artifacts/cart_evals/` 和 `artifacts/login_evals/`。界面只访问项目自带的 TestLab。模型策略仍需本机 `.env` 配置，且只有点击模型运行按钮时才发起请求。

运行历史的 SQLite 索引保存在 `artifacts/webprobe_history.sqlite3`。它只存运行类型、策略、数据集、成功数、任务摘要和产物路径；逐步轨迹、断言细节、截图与 Playwright Trace 仍分别留在每次运行的 JSON/JSONL/PNG/ZIP 文件中。Streamlit 首次查看历史时会把已有产物补录到索引，之后新的 UI 单题运行和默认目录下的 Evals 会登记到 SQLite。

## B1 浏览器工具

`webprobe/browser_tools.py` 提供 `open`、`fill`、`click`、`back` 和 `observe`。每次调用返回 `PageObservation`（URL、标题、主标题、状态文本、搜索输入值、可见商品）。表单页还会记录姓名与反馈内容的当前输入值、表单状态、提交次数和提交按钮文字；其他页面这些字段为 `None`。`fill("textbox", ...)` 与 `click("button", ...)` 可以直接操作表单，工具只返回操作后的事实，不判定网站 Bug。调用顺序目前仍由固定测试脚本决定。工具只接受测试中列出的本地页面 URL；这是本阶段的局部约束，不代表已完成任意网站的安全隔离。`artifacts/tool_steps.json` 和 `artifacts/form_tool_steps.json` 保存固定脚本的真实返回值。表单观察暂时依赖 TestLab 的字段标签与 `data-*` 状态，尚不是通用网站观察器。

## B2 有预算的决策循环

`webprobe/demo_loop.py` 用固定规则选择动作，按“观察 → 决策 → 操作 → 再观察”循环运行。`max_steps` 统计实际浏览器动作，达到上限就停止；评测调用还会传入 `max_seconds`，超时返回 `time_budget_exhausted`。结果包含 `completed`、`suspected_bug`、`budget_exhausted`、`time_budget_exhausted`、`needs_review` 和 `execution_failed`；执行失败或超时都不等于网站 Bug。`artifacts/demo_run.json` 是完整正常场景的一次运行记录。这里的决策器仍是写死的教学示例，不是 LLM Agent。

### 表单规则循环

`webprobe/form_loop.py` 为反馈表单实现独立的有限规则循环。`FormGoal` 只含任务输入与正确产品规格，不接收数据集中的 `has_bug` 标签；`choose_form_action()` 根据上一轮 `PageObservation` 选动作，`execute_form_action()` 调用 `BrowserTools`。点击提交时使用本轮观察到的按钮文字，因此 v3 的正常文案变化仍可完成。空必填项第一次意外提交会再试一次，两次均成功才返回 `suspected_bug` 和同会话复查证据。工具失败、证据不足和步骤耗尽分别记录，不报网站 Bug。`tests/test_form_loop.py` 验证这些分支，`artifacts/form_loops/` 保存当前固定 Case 的实际步骤。这里仍是人写的规则，不是 LLM Agent。

### 表单 Evals：固定数据集的独立评分

`webprobe/form_evals.py` 为每题创建全新的浏览器上下文，使用相同的无参数表单 URL，运行所选策略，并保存页面截图、Playwright Trace、步骤观察、断言、失败分类与汇总。单题断言、整套指标和对照检查位于较短的 `webprobe/form_eval_scoring.py`。评分器依据固定产品规格检查最后一次实际提交的页面状态；对于故障报告，还独立核对两次成功提交空表单的证据。单题执行失败或预算耗尽不会被算成“无 Bug”。运行：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = (Join-Path (Get-Location) '.browsers')
.\.venv\Scripts\python.exe -m webprobe.form_evals
```

输出位于 `artifacts/form_evals/<run_id>/`，包括数据集副本、`results.jsonl`、`summary.json`、`bad_cases.jsonl`、每题 PNG 和 Trace ZIP。`summary.json` 保存数据集与代码哈希。混淆矩阵以一题为单位，未形成结论的题单独计数。Recovery Rate 定义为“至少有一条显式恢复记录且最终完成”的 Case 数除以“至少有一条显式恢复记录”的 Case 数；没有恢复尝试时该指标为 `null`，不按 0% 处理。

表单 Baseline 与当前版使用相同的数据集、浏览器环境、评分器、步骤与时间预算。唯一策略差别是：Baseline 遇到第一次空表单异常成功提交就停止为 `needs_review`，当前版再提交一次核对证据。运行一组对照：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = (Join-Path (Get-Location) '.browsers')
$baseline = .\.venv\Scripts\python.exe -m webprobe.form_evals --policy rule_baseline
.\.venv\Scripts\python.exe -m webprobe.form_evals --policy rule --baseline-summary (Join-Path $baseline 'summary.json')
```

第二个运行目录的 `comparison.json` 核对数据集与源码哈希，并列出任务成功率、未形成结论数、混淆矩阵、恢复尝试数、恢复后成功数、Recovery Rate、平均步骤与耗时。Baseline 的故障题如果停在 `needs_review`，应单列未形成结论，不记为 FN。这里的 Baseline 是**规则策略消融对照**，不是基础 LLM。当前九题集中在一个表单规格与一种故障机制上，单次耗时差不能用来证明稳定性能。表单与搜索的规则循环和模型循环都会在动作间检查 `max_seconds`；模型请求、Playwright 默认操作超时和搜索/库存显式等待都会压到当时的剩余时间以内。同步浏览器调用依靠 Playwright 超时返回，调度与结果保存等收尾时间不属于 Agent 循环时限；运行记录仍会以实际耗时再次核对。

2026-11-08 首次真实 Gemini 表单评测使用 `feedback_v3.jsonl`，结果为 4/9 任务完成、5/9 未形成结论，混淆矩阵 TP=0、FP=0、FN=0、TN=4。5 个未结论中，3 个是 Free tier 每分钟 15 次请求上限触发的 429，1 个是重复填写姓名耗尽步骤，1 个在一次异常成功提交后提前停止。真实故障题没有获得有效的 Bug 检测结论，因此这次数据**不能证明模型检测能力**。逐题记录保存在 [`20261108T103643Z_eece6afc`](artifacts/form_evals/20261108T103643Z_eece6afc/summary.json)。这是改进前的真实运行基线；后续 V3 策略增加了重复动作恢复、异常提交复现和请求节流，与该 V2 记录不直接混成同一版本成绩。

2026-11-08 随后真实运行了一次 V3 九题评测：任务按时完成 4/9，模型调用失败 4/9（3 次 Gemini 返回“当前地区不支持该 API”，1 次请求超时），另有 1 题因超过用例时限而没有算任务成功。分类混淆矩阵为 TP=1、FP=0、FN=0、TN=4；这只表示 5 个形成结论的 Case 与 Ground Truth 一致，样本少且只有一种预设故障，不能据此宣称模型检测能力可靠或优于 V2。请求节流总等待 55.146 秒。逐题决策、错误、截图和 Trace 保存在 [`20261108T105449Z_fe224d59`](artifacts/form_evals/20261108T105449Z_fe224d59/summary.json)。本次代码更新后，后续评测还会记录 `model_error_type`，并按超时、地区限制、速率限制等原因拆分模型服务失败；历史结果文件保留原样，不会伪装成当时已经记录了这些字段。

2026-11-10 完成运行时预算改动后的规则固定集对照。搜索集 Baseline 3/4、当前策略 4/4；唯一执行失败是 Baseline 在 3 秒正常慢加载场景下的 2.5 秒等待超时，当前策略把等待调到 3.5 秒后完成。平均耗时从 2.025 秒升到 2.673 秒，体现了更长等待带来的时间成本。表单集 Baseline 6/9、当前策略 9/9；3 个待审故障由当前策略的二次提交复查确认，TP 从 0 到 3，TN 保持 6。平均步骤 3.11→3.44，平均耗时 0.354→0.397 秒。四次运行都没有触发 Case 时间预算，因此这些结果验证的是本地规则策略和固定 TestLab；预算边界本身由离线时钟测试覆盖。本次是小型固定集的单次规则实验，不代表真实 LLM 能力或普遍性能提升。搜索与表单的逐题记录、截图、Trace 和对照文件分别在 [`search current`](artifacts/evals/20261110T004401Z_e14e1b3e/comparison.json) 与 [`form current`](artifacts/form_evals/20261110T004435Z_c64903bd/comparison.json) 运行目录。

同日全套自动化回归测试 **121 项通过（93.69 秒）**，使用项目 Chromium 和本地 TestLab；模型循环测试使用预写假响应，没有调用外部模型 API。

2026-11-07 实际运行了一次九题对照：Baseline 成功 6/9，3 题为 `needs_review`；带二次复查的当前规则策略成功 9/9，混淆矩阵为 TP=3、FP=0、FN=0、TN=6。平均步骤为 3.11→3.44，平均耗时为 0.280→0.317 秒。增量来自对三道故障题做第二次提交复查。这是单次、单一故障家族的本地规则实验，不是 LLM 成绩，也不能推出统计显著或普遍性能提升。逐题原始评分、两个运行摘要、哈希与限制记录在 [`evals/reports/2026-11-07-form-rule-comparison.json`](evals/reports/2026-11-07-form-rule-comparison.json)；本机截图与 Trace 仍在对应的 `artifacts/form_evals/` 运行目录。

## B3 单次模型工具调用

`webprobe/llm_decider.py` 让模型每轮通过函数工具提议一个动作。Gemini 使用官方 OpenAI 兼容地址上的 Chat Completions。项目从根目录 `.env` 读取配置，系统环境变量优先；`.env` 已被 Git 忽略，不能提交真实密钥。Evals 会按每个 Case 最坏模型调用数预留免费层请求额度；等待时间记录在 `rate_limit_wait_seconds` 和汇总的 `model_rate_limit_wait_seconds` 中，不计入单题网页执行时间。

不想付费时可继续用已有 Gemini Key，但先在 Google AI Studio 的 API Keys 页面确认它关联的项目处于 **Free**，且没有启用 Cloud Billing。Google 标注 `gemini-3.1-flash-lite` 有免费层额度；如果项目已关联有效的付费结算，不能据此保证调用不会收费。免费层请求可能被用于改进 Google 产品，因此本项目的模型评测只向模型发送本地 TestLab 的虚构数据；不要把真实用户信息或密钥放进网页/任务。真实调用只会在你启动模型测试时发生。

### 配置 Gemini Key（只需一次）

项目根目录的 `.env` 是本机配置文件，已被 Git 忽略。第一次使用时，把 `.env.example` 复制为 `.env`，然后只修改 `GEMINI_API_KEY=` 这一行，把旧项目 `.env` 中 `GEMINI_API_KEY=` 右边的值粘贴过来。不要加引号，也不要提交 `.env`。Windows PowerShell 命令如下；若 `.env` 已存在，不要复制覆盖：

```powershell
Copy-Item .env.example .env
notepad .env
```

保存后再启动或重启 Streamlit。后续单题运行、命令行模型评测和九题模型评测都会复用这份配置；启动界面和检查配置不会调用 Gemini。

## B4 有预算的模型循环

`webprobe/model_loop.py` 将模型选动作、Playwright 执行、页面观察回传串起来。模型的 `stop` 只有在程序核对最近一次搜索提交的 URL、关键词、状态文字、可见商品，以及详情页固定预期后才算完成；预算用尽、操作失败或模型响应无效分别记录。错配只会使这条流程返回 `needs_review`，不会自动判定网站有 Bug。`webprobe/llm_decider.py` 现在会把函数调用和实际工具结果放进下一次 Responses API 请求。`tests/test_model_loop.py` 使用预写的假模型响应与真实本地浏览器验证接线；尚无真实模型实验，也没有 Bug 检测成绩。

### D.7.1 表单模型循环接线（离线验证）

`webprobe/llm_decider.py` 按任务类型提供搜索或表单动作表。表单模型可提议 `open`、`fill_name`、`fill_message`、`submit`、`report_bug`、`stop`、`halt`；Python 再次校验返回动作，并把真实浏览器观察送回下一轮模型输入。`FormGoal.task` 保存自然语言任务，故障开关与 Ground Truth 不传给模型。`webprobe/form_model_loop.py` 限制浏览器步骤数与模型调用数；模型说 `stop` 时，程序仍要检查最近一次提交是否符合固定规格。历史版本演进记录保留了当时只接通正常完成路径的上下文；当前搜索与表单流程都支持受证据约束的 Bug 报告。

运行离线接线测试：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = (Join-Path (Get-Location) '.browsers')
.\.venv\Scripts\python.exe -m pytest -q tests/test_form_model_loop.py -p no:cacheprovider --basetemp .tmp\pytest-form-model
```

`artifacts/form_model_scripted.json` 保存预写响应驱动真实浏览器的表单步骤。这只证明工具调用接口能接通，**不代表模型自主选择了这些动作**。当前未配置 API Key 或模型。

### D.7.2 表单模型复用固定 Evals

表单评测入口支持 `--policy model`：它与规则策略共用 `feedback_v3.jsonl`、独立浏览器上下文、固定评分器、截图和 Trace，并额外记录模型名称、调用次数与可见决策摘要。模型没有形成结论时，评分器仍会从实际提交步骤计算网页断言；`agent_conclusion` 保持 `null`，不把它记成“无 Bug”。D.7.2 当时尚未接入模型 Bug 报告；当前实现见下一节。

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = (Join-Path (Get-Location) '.browsers')
.\.venv\Scripts\python.exe -m webprobe.form_evals --policy model
```

Gemini 配置步骤：把 `.env.example` 复制成 `.env`，保持 `WEBPROBE_PROVIDER=gemini` 和 `WEBPROBE_MODEL=gemini-3.1-flash-lite`，只在 `GEMINI_API_KEY=` 后粘贴你自己的 Key。不要把 Key 发到聊天里。模型调用最多输出 256 个 Token，SDK 自动重试关闭；Evals 对本地观察到的 15 RPM Free tier 上限使用 14 RPM 的本地预留预算，Google 项目实际限制仍以 AI Studio 显示为准。

未设置真实 API 配置时，入口会在创建实验目录前报错。`tests/test_form_evals.py` 使用预写响应和真实本地页面检查模型路径的评分接线；这些测试**不是**真实模型 Evals 结果。数据集与相关源码哈希会写入每次运行的 `summary.json`，模型与规则策略不会混在同一份汇总中。

每次模型请求默认最多等待 20 秒，且关闭 SDK 自动重试；可通过 `WEBPROBE_REQUEST_TIMEOUT_SECONDS` 调整单次调用时限。固定表单任务运行时，请求超时还会进一步收紧到当前 Case 的剩余时间。超时会记录为模型调用失败或时间预算耗尽，不记成网站 Bug。评测前为遵守 Gemini 速率限制所做的等待发生在 Case 计时之外，并单独记录。

### D.7.3 让模型提议 Bug，再由证据核验

表单动作表新增 `report_bug`。模型可以提议报告，但 `webprobe/form_model_loop.py` 不直接采信：它调用 `webprobe/form_loop.py` 中与规则策略共用的 `verify_form_bug_report()`，要求两次实际空必填提交都显示异常成功，且提交计数依次为 1、2。若本地 TestLab 已有一次异常提交证据，但模型提前 `stop` 或报告，循环会在步骤预算允许时自动重提一次；只有第二次真实观察也符合条件才返回 `suspected_bug`。正常表单和缺少第一次证据的情况仍为 `needs_review`。Evals 再从保存的步骤独立检查两次证据，并结合 Ground Truth 评分。

`tests/test_form_model_loop.py` 用预写模型动作和真实本地浏览器覆盖有效复现、提前停止后的复查、重复填入恢复和正常页面误报；`tests/test_form_evals.py` 检查报告进入评分、截图和 Trace。这里验证的是模型接口与安全闸门，**不是**真实模型的检测率。V2 与 V3 的真实 Gemini 单次运行分别记录在上方；V3 暴露的服务地区限制和超时现已被细分写入 Evals 结果，但需要修复 API 可用性后重跑，才能继续分析模型行为。

### D.7.4 记录模型 Token 用量

`OpenAIDecider` 对 OpenAI Responses API 读取 `usage.input_tokens` / `output_tokens`，对 Gemini Chat Completions 读取 `usage.prompt_tokens` / `completion_tokens`；两者都按一个 Case 累加。表单 `results.jsonl` 的 `token_usage` 保存单题用量，`summary.json` 仅在所有 Case 都有完整用量时汇总，并用 `token_usage_covered_cases` 记录覆盖题数。如果服务或假响应没有提供 `usage`，对应值保持 `null`；不会把未知用量记成 0。`api_cost` 仍为 `null`，因为项目不读取你的账单，也无法仅凭 Token 数准确判断账户最终是否扣费。

缺少所选服务商的 API Key 时，模型评测入口在创建实验目录前报错。离线测试中人为提供的 usage 数字只验证汇总代码，不是实际 API 消耗。真实运行的 Token 统计只对成功返回且包含完整 usage 的 Case 求和；API 429 题目的用量保持未知。

### D.7.5 单题试运行

表单评测入口支持 `--case-id`，先从固定数据集里选择一题，再执行所选策略。例如离线检查正常的必填校验：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = (Join-Path (Get-Location) '.browsers')
.\.venv\Scripts\python.exe -m webprobe.form_evals --policy rule --case-id feedback_empty_001
```

配置好真实 API 后，可将 `--policy` 改为 `model`；先看单题目录中的 `results.jsonl`、截图、Trace 和 `summary.json`，再决定是否运行九题整套评测。单题 `summary.json` 标记 `evaluation_scope: single_case` 和 `selected_case_ids`，其中的成功率只描述这一题；单题模式不能与整套 Baseline 生成 `comparison.json`。未知 Case ID 会在创建实验目录前报错。没有配置真实 API 时，此命令的模型策略仍不能运行。

## C1 可控的搜索结果故障

正常打开 `lab/search.html` 时行为不变。使用 `lab/search.html?bug=search_mismatch` 会启用预设故障：输入“茶杯”后错误地显示“帆布包”，状态仍显示“找到 1 件商品”。`tests/test_search.py` 把“蓝色茶杯”固定为 Ground Truth，并分别验证正常页面符合规格、故障页面稳定偏离规格。故障注入开关是教学用测试机制，不代表已接入通用测试集或实现了 Bug 分类。

## C2 正常慢加载与等待

`lab/search.html?delay_ms=1200` 在提交后先显示“搜索中…”，约 1.2 秒后显示正常结果。TestLab 用 `data-search-state` 标记加载是否结束；`BrowserTools.wait_for_search_ready()` 等待这个就绪标记，不把“正确商品名”当成等待条件，因此真正的商品错配仍留给后面的断言发现。规则循环把 `wait_search` 记为一个浏览器步骤；等待超时记录 `execution_failed`，不报网站 Bug。等待上限在对照实验中分别为 2.5 秒和 3.5 秒。这个就绪标记只属于本地 TestLab，未来测试其他网站要寻找该站自己的加载完成信号。

## 第一版 WebProbe Evals

`evals/datasets/search_v1.jsonl`、`search_v2.jsonl` 保留旧实验定义。当前 `search_v3.jsonl` 人工固定四个 Case：正常搜索、搜索结果错配、1.2 秒正常慢加载，以及 3 秒正常延迟的预算边界。四案共用“搜索茶杯应显示蓝色茶杯”的产品规格；只有商品错配案开启故障开关。运行器 `webprobe/evals.py` 在每案前创建全新的浏览器上下文，执行所选规则策略，独立对比搜索完成后可见商品与固定预期，并保存步骤、断言、截图、Playwright Trace、`results.jsonl`、`bad_cases.jsonl` 和 `summary.json` 到 `artifacts/evals/<run_id>/`。

代码阅读顺序：先看 `webprobe/eval_dataset.py` 中的 `EvalCase` 和 `load_cases()`，理解固定题目；再看 `webprobe/eval_scoring.py` 中的 `score_case()` 和 `summarize_results()`，理解单题与整套评分。最后只需看 `webprobe/evals.py` 的 `run_case()` 与 `run_suite()`，理解浏览器执行和结果保存。旧的 `from webprobe.evals import load_cases, score_case` 导入方式仍可用。

在本目录的 PowerShell 中运行：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = (Join-Path (Get-Location) '.browsers')
.\.venv\Scripts\python.exe -m webprobe.evals
```

规则循环在商品列表与固定规格不符时重新提交一次；两次仍错才输出 `suspected_bug`，并记录为同一浏览器会话内复现。慢加载时先等搜索就绪，再做商品断言。评测器以一个 Case 为单位统计 TP、FP、FN、TN；`execution_failed` 和 `needs_review` 保持未形成结论。传入 `--baseline-summary <旧版 summary.json 路径>` 可在新实验目录生成 `comparison.json`，但不同数据集版本的实验不能直接比较。每次运行同时保存数据集副本、数据集 SHA-256 和策略及 TestLab 源码 SHA-256；比较文件还记录源码哈希是否相同。本节的历史对照只评估规则循环；模型循环使用独立的真实或离线假响应运行，不能与规则成绩混称。搜索任务的 `max_seconds` 现在会在循环中检查，并作为模型请求与显式页面等待的剩余超时上限；收尾工作不计入 Agent 循环时限。

摘要中的 `model_failure_count` 统计模型服务或响应错误，包括发生在 Case 时间预算内的请求超时；这类超时也会进入 `time_budget_exceeded_count`，两项可以重叠。即使页面已显示错配，超时仍不会自动形成 Bug 结论，也不会进入 TP/FP/FN/TN。

搜索评测中普通浏览器动作的默认超时为 8 秒，用来容纳繁忙机器上元素已找到但尚未稳定的情况；`wait_search` 仍单独使用 Baseline 2.5 秒、当前版 3.5 秒的策略预算。两种等待不能混为一谈。

### 同数据集等待预算对照

`rule_baseline` 与 `rule` 共用同一套规则、四案数据集、评分器和 Case 预算，只把 `wait_search` 上限设为 2500 毫秒与 3500 毫秒。两组分别标记为 `rule_demo_v3_wait_2500` 与 `rule_demo_v4_wait_3500`。在项目目录运行：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = (Join-Path (Get-Location) '.browsers')
$baseline = .\.venv\Scripts\python.exe -m webprobe.evals --policy rule_baseline
.\.venv\Scripts\python.exe -m webprobe.evals --policy rule --baseline-summary (Join-Path $baseline 'summary.json')
```

第二次运行的 `comparison.json` 保存成功率、执行失败数、Bad Case 数、恢复尝试与成功数、Recovery Rate、平均耗时及混淆矩阵前后值，并核对数据集与源码哈希。一次运行只能说明这次四案的观察结果；尤其平均耗时会受浏览器启动和系统负载影响，不能据此推断稳定的性能提升。模型模式仍使用原来的 2500 毫秒，未参加这组规则对照。

### 小规模重复实验

`webprobe/repeat_eval.py` 默认交替顺序运行三轮：第一轮基线→改进版、第二轮改进版→基线、第三轮基线→改进版。每次依然调用原来的 `run_suite()`，保存独立的截图、Trace 和原始结果。运行：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = (Join-Path (Get-Location) '.browsers')
.\.venv\Scripts\python.exe -m webprobe.repeat_eval --trials 3
```

`artifacts/repeats/<run_id>/progress.json` 记录每轮的原始目录，`repeated_comparison.json` 保存逐轮成功率、平均耗时差、耗时差的中位数和范围、每个 Case 的通过次数。汇总前会核对所有运行的数据集哈希、策略源码哈希和 Case 顺序。这里只做描述性统计：三轮、四个本地 Case 不足以推断统计显著性，也不代表真实 LLM 表现。

### Bad Case 分析记录

`webprobe/bad_cases.py` 只分析任务失败的 Case。它根据 `run_status`、最后失败动作和结构化 `error_type` 标记可疑现象；例如 `wait_search / SearchWaitTimeout` 归为 `Timeout / Loading Issue`。分类结果写入 `bad_cases.jsonl`，包含 Case ID、分类依据、截图和 Trace 路径，并要求人工复核。这个分类器不修改任务评分，也不把等待超时当成网站 Bug。三秒延迟案的 Ground Truth 是无 Bug；在 2.5 秒基线下它是执行失败、未形成结论，且不进入 TP/FP/FN/TN。其他未知失败会保留为 `Unclassified`，不会凭报错文字推断根因。

### 模型循环使用同一套 Evals

设置 `WEBPROBE_API_KEY`（或 `OPENAI_API_KEY`）与 `WEBPROBE_MODEL` 后，可运行 `python -m webprobe.evals --policy model`。它复用同一数据集、浏览器隔离、独立商品断言、截图和 Trace；当前搜索模型策略名为 `model_loop_v4_search_auto_repro_time_budget`，额外记录模型调用次数、可见决策摘要和模型错误类型。模型可以提议 `report_bug`，但 Python 只在同会话最近两次提交的关键词与页面状态正确、且两次商品结果都偏离固定预期时接受报告；对搜索这种本地只读操作，若模型只查一次就报告，程序会在剩余步数内自动复查一次并保存恢复记录。页面仍加载、工具报错或关键词不符不会变成网站 Bug。真实模型是否会在慢加载场景中正确选择 `wait_search` 尚未验证。若每个 Case 的模型响应都完整返回用量，汇总会计算输入/输出/总 Token；有任一 Case 缺失时总量标为未知，并记录已覆盖 Case 数。项目没有固化可靠的服务定价，因此不估算 API Cost。模型调用预算暂定为每案 `max_steps + 1`；搜索模型循环按 `max_seconds` 限制模型请求和浏览器动作。历史 `model_loop_v2_time_budget` 与 `model_loop_v3_search_bug_repro_time_budget` 结果保留原版本含义，不能当作已具备这项自动复查能力的成绩。

`tests/test_evals.py` 使用预写模型响应和真实本地浏览器验证这条接线，不产生真实模型成绩。未设置 API 配置时，模型模式在创建实验目录前报错。现有 `artifacts/evals/` 中的运行成绩仍属于规则版本，不能当作模型结果。

搜索评测也支持 `--case-id` 单题试运行。运行真实模型前，可先从固定数据集选一题检查动作与证据，例如 `python -m webprobe.evals --policy model --case-id search_mismatch_001`。单题结果仍保存完整截图和 Trace，`summary.json` 会标记 `evaluation_scope: single_case`；它不能与完整集 Baseline 生成对照，也不能代表整套成绩。

2026-11-10 用已配置的 Gemini Free Tier 真实运行了这一搜索故障题，记录在 [`20261110T023015Z_85d7d3f9`](artifacts/evals/20261110T023015Z_85d7d3f9/summary.json)。模型完成打开、输入和第一次搜索后，提出报告商品错配；Python 因为只有一次提交证据而拒绝，并要求复现。随后模型请求超时，整题在 20.029 秒结束，实际调用 5 次，最终结论为空。独立评测因此没有将真实预设 Bug 记为 TP，也没有把超时算成无 Bug；截图、Trace、恢复原因和原始错误都保存在运行目录中。该历史结果生成时还没有 `model_error_type` 字段；当前代码已把请求超时单独分类为 `request_timeout`，以便后续运行区分模型服务失败与纯步骤预算耗尽。此次 `token_usage` 与 `api_cost` 均未知，不能从项目日志推算账单金额；这是一道题的失败样本，不能代表整套模型成绩。

按上面的 Bad Case 修改后，用同一 Case、同一 20 秒预算再运行一次，结果保存在 [`20261110T024654Z_7e963b79`](artifacts/evals/20261110T024654Z_7e963b79/summary.json)：5 次模型调用、8.656 秒完成，结论为 `bug`，两次搜索都显示错误商品，独立评分为 TP=1，复现成功；服务返回 3,669 Tokens（输入 3,526、输出 143），API 费用仍未估算。模型这次自己决定再次提交搜索，因此程序的自动复查分支没有被触发。两次各只有一题、一次运行，结果只能说明这两次实际轨迹不同，不能据此证明普遍提升；自动复查分支由离线固定响应测试覆盖。后续搜索结果会把使用的模型名一并保存，旧运行目录保持原样。

这条 Bad Case 后，搜索 Agent 新增了一次受控恢复：模型只报告一次错配时，程序会在步骤预算内自动按同一关键词再搜索一次。只有两次都在正确页面、关键词匹配、搜索完成且商品仍违反规格，才接受疑似 Bug；恢复后仍不一致就继续交回模型处理。重复搜索是本地 TestLab 的只读操作，不能把这个策略直接套到会产生提交或付款副作用的表单。

## LangChain 对照练习（可选）

`webprobe/langchain_intro.py` 使用 `create_agent`、一个查询 TestLab 固定规格的本地函数，以及两条预写模型响应，演示 `human → ai tool_calls → tool → ai` 的消息顺序。LangChain 负责把 Python 函数注册为工具、执行模型提出的调用，并把工具结果加入消息历史。这里没有浏览器操作，也没有真实 LLM 决策；练习结果不参加 WebProbe Evals。`ScriptedToolModel.bind_tools()` 只是让测试模型接受工具列表，实际模型集成不需要照搬这个类。

这是独立的学习依赖，不改变 WebProbe 主流程：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-langchain.txt
.\.venv\Scripts\python.exe -m webprobe.langchain_intro
.\.venv\Scripts\python.exe -m pytest -q tests/test_langchain_intro.py -p no:cacheprovider --basetemp .tmp\pytest-langchain-intro
```

对照主项目：`llm_decider.py` 显式处理函数调用与工具结果，`model_loop.py` 显式控制步骤预算和完成断言。`create_agent` 能封装常见工具循环，但测试规格、浏览器访问限制、Bug 证据和 Evals 仍由应用自己负责。

## LangGraph 状态与分支练习（可选）

`webprobe/langgraph_intro.py` 把一次商品检查拆为 `observe → decide → act → verify` 四个节点。`ProbeState` 在节点间保存当前快照、尝试次数和结论；验证未通过时，条件边把流程送回观察，达到两次上限则以 `needs_review` 结束。运行三个预写场景：正常、首次异常后恢复、持续不符。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-langchain.txt
.\.venv\Scripts\python.exe -m webprobe.langgraph_intro
.\.venv\Scripts\python.exe -m pytest -q tests/test_langgraph_intro.py -p no:cacheprovider --basetemp .tmp\pytest-langgraph-intro
```

此练习的快照、动作和分支规则都是预写的：没有 Playwright、真实 LLM 或网页 Bug 判定，不参加 WebProbe Evals。它展示 LangGraph 怎样组织已有流程；主项目的 `model_loop.py` 仍使用普通 Python 循环。

## 学习路线（概览）

1. **A 浏览器基础**：页面、Locator、操作、断言，先掌握确定性测试。
2. **B Agent 基础**：封装浏览器工具，再接入模型的工具调用和有限循环。
3. **C 缺陷验证**：扩充可控 TestLab、预设故障、复现与报告。
4. **D Evals**：固定数据集与独立 Ground Truth、Baseline、指标和失败分析。
5. **E 工程增强**：恢复、状态管理、预算与人工确认。
6. **F 产品化**：Streamlit、持久化、文档与真实实验展示。

从本批起保留固定规格和自动化断言；后续的 Agent 结果必须与这些独立预期比较。
