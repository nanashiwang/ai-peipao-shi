# 重庆机构陪跑师效率系统 MVP

本目录是独立本地试点工程，不依赖工作区其他文件。当前版本默认使用 mock AI 和 mock RPA，目标是快速跑通：

`数据导入 -> 家庭归档 -> 周报/画像生成 -> 老师审核 -> 发送任务 -> mock 发送 -> 日志回写`

> **📌 架构已升级为「总控台 + 多被控端」。** 服务器部署、添加被控端（看板生成接入包）、
> 看板使用、常见问题，请看 **[docs/部署与运维.md](docs/部署与运维.md)**。
> 另外：RPA 会话定位已改为「本地 PaddleOCR 为主 + 阿里 ARK 云端兜底」（本文件下方部分小节是旧 UIA 方案的描述，以部署与运维文档为准）。

## 本地运行

```powershell
cd chongqing-coach-mvp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

打开后台：

```text
http://127.0.0.1:8000
```

## 暂停服务与压缩前释放占用

如果是在当前 PowerShell 窗口里运行的本地服务，直接在运行 `uvicorn` 的窗口按：

```text
Ctrl + C
```

如果不确定服务是否还在占用 `8000` 端口，可以用下面命令查找并停止：

```powershell
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,State,OwningProcess
Stop-Process -Id <上一步看到的 OwningProcess> -Force
```

如果是 Docker 启动的服务，在项目目录执行：

```powershell
docker compose down
```

如果正在运行企业微信 RPA 发送脚本，也需要先停止。若脚本就在当前窗口运行，同样按 `Ctrl + C`。如果找不到窗口，可以先查看相关 Python 进程：

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "wecom_sender|mock_sender|uvicorn" } | Select-Object ProcessId,CommandLine
Stop-Process -Id <上一步看到的 ProcessId> -Force
```

压缩前建议确认没有服务占用：

```powershell
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
```

没有输出，通常就可以正常压缩项目目录。

## 一键试点流程

1. 点击「载入样例」导入 5 个家庭的聊天记录。
2. 进入「家庭/学员」或「家庭详情」，点击画像、周报、回复、打卡/PBL 四个 Agent 按钮。
3. 在「工作台」或对应 Agent 页面查看结构化结果卡片，人工编辑后保存审核稿。
4. 点击「加入发送任务」，进入「待发送任务」集中审核最终内容。
5. 点击「发送全部」或单条「发送」，任务会实际写入网页通讯会话。
6. 进入「发送日志」查看回写结果，也可以回到「网页通讯」检查新消息。

## Agent 接口与替换点

四个 Agent 已接入豆包/火山 Ark。未配置本地私有配置文件时，系统会自动回退到本地规则逻辑：

```text
POST /api/agent/profile
POST /api/agent/weekly-report
POST /api/agent/reply
POST /api/agent/checkin-pbl
```

兼容短路径：

```text
POST /agent/profile
POST /agent/weekly-report
POST /agent/reply
POST /agent/checkin-pbl
```

统一输入：

```json
{
  "family_id": "FAM001",
  "message": "可选，AI回复使用",
  "tone": "standard",
  "source": "UI按钮触发"
}
```

配置豆包 Ark：

```text
config/ark.json
```

`config/ark.json` 是私有文件，已写入 `.gitignore`，不要提交。可参考 `config/ark.example.json` 的结构。

Agent 原始 JSON、展示文本、人工审核稿会写入 `ai_outputs` 表。真实 API 调用集中在：

```text
app/services/ark_client.py
app/services/agent_service.py
```

家庭画像 Agent 会同步维护家长档案字段：关注点、沟通风格、满意度评级、风险信号、续报意向、学生状态和建议动作。家庭详情、AI 画像页和陪跑会话侧栏会优先展示这些结构化字段。

家庭详情支持人工记录跟进动作，覆盖电话、私信、群提醒、周报、补课、投诉和续报沟通。记录会写入 `followup_records`，同时进入家庭时间线，方便复盘服务闭环。

家庭详情的 AI 操作区支持“一键生成并复核”：一次生成家庭画像、AI 周报、AI 回复和打卡/PBL 识别结果，并在右侧直接编辑审核稿、保存复核结果或加入发送任务。

豆包返回失败或 `config/ark.json` 缺失/字段为空时，系统会使用本地规则兜底，并把失败原因写进 Agent 原始 JSON。

## Docker 运行

```powershell
cd chongqing-coach-mvp
docker compose up --build
```

Docker 模式会启动 FastAPI、PostgreSQL、Redis。后台地址仍为：

```text
http://127.0.0.1:8000
```

## 数据导入字段

支持 CSV / XLSX，字段可以使用英文或中文：

| 标准字段 | 中文别名 |
| --- | --- |
| family_id | 家庭编号、家庭ID |
| parent_nickname | 家长昵称 |
| child_grade | 孩子年级 |
| course_stage | 课程阶段 |
| unit_progress | Unit进度、Unit 进度、单元进度 |
| pbl_count | PBL次数、PBL 次数 |
| checkin_rate | 打卡率、打卡完成率 |
| next_milestone | 下一里程碑、下个里程碑 |
| coach_name | 陪跑师 |
| service_status | 服务状态 |
| message_time | 聊天时间、时间 |
| speaker | 说话人 |
| content | 消息内容、内容 |
| source | 群/单聊来源、来源 |
| checkin_status | 打卡状态 |

课程阶段模板可以只导入家庭档案，不必带聊天内容。导入后「家庭详情」会展示年级、课程阶段、Unit 进度、PBL 次数、打卡完成率和下一里程碑；后续聊天记录仍按原规则写入 `raw_messages`。

## 真实接口替换点

- `app/services/ai_mock.py`：后续替换 DeepSeek / 通义 / Kimi / 混元适配器。
- `app/services/scenario.py`：固定场景和打卡关键词规则。
- `rpa/mock_sender.py`：mock 发送器，不触碰企业微信。
- `rpa/wecom_sender.py`：真实企业微信 PC 端 RPA 发送器，使用 pywinauto / pywin32 / pyperclip。
- `APP_ENV`：`local/pilot/production` 等运行环境；`production` 会强制校验数据库和 ARK 密钥隔离。
- `DATABASE_URL`：本地默认 SQLite，Docker 默认 PostgreSQL；正式环境禁止使用 SQLite。
- `ADMIN_AUTH_REQUIRED` / `ADMIN_AUTH_SECRET` / `ADMIN_USERNAME` / `ADMIN_PASSWORD`：管理端鉴权配置；正式环境默认强制启用，支持管理员、陪跑师、只读角色。
- `SEND_LOG_RETENTION_DAYS` / `SEND_SCREENSHOT_RETENTION_DAYS` / `RUNTIME_LOG_RETENTION_DAYS`：发送日志、截图证据、运行日志的保留天数；控制端可先预览，再显式确认清理。

## 数据安全与脱敏

- 管理端只读角色访问家庭、时间线、AI 输出、周报、画像、发送任务和发送日志时，后端会自动返回脱敏视图。
- 脱敏视图会遮盖手机号、家长姓名、孩子年级/姓名，并把聊天内容、AI 生成正文、画像/周报文本替换为长度提示，不修改数据库原始记录。
- 运维排障或外部演示优先使用 `GET /api/ops/redacted-export` 导出脱敏快照，避免直接复制原始聊天和发送内容。
- SQLite 原始备份会标记为 `raw_sensitive` / `contains_sensitive_data=true`，仍包含家长、孩子、聊天和发送内容，只能由管理员加密保存。

## 控制端操作分层

- 发送任务接口会返回 `workflow_stage`、`allowed_operations` 和 `operation_warnings`，前端按这些能力渲染按钮。
- 只读角色只能查看；陪跑师可编辑、审核、试运行和网页测试发送；管理员才可确认企业微信真实发送和调度设备。
- 已发送/已取消等终态任务只能查看，避免历史任务被误改或重复触达。
- dry-run 发送失败会自动排队重试；真实发送失败或超过重试上限会进入健康告警，需人工复核后手动重试。

## 前端页面状态

- 前端统一使用 `state-card` 呈现空状态、加载态、错误态和风险态，避免页面出现空白区域。
- 首次刷新会在关键面板展示加载态；控制端加载失败会统一给出错误态和重试入口。
- 工作台检测到退费/投诉等高风险家庭时，会优先展示风险态并引导进入管理看板。

## 移动端与窄屏

- 小屏下左侧栏会切换为顶部横向滚动导航，主内容区改为单列，避免页面级横向溢出。
- 表格保留内部横向滚动，不挤压整页；聊天、家庭详情、任务审核等多栏页面在窄屏下自动堆叠。
- 前端请求头里的中文操作人会 URL 编码，后端自动解码，避免浏览器拒绝非 ASCII Header。

## 左侧栏导航

- 左侧栏按「日常工作」「资料与 AI」「运营排障」分组，陪跑师先看到日常入口，管理与排障入口降级展示。
- 每个业务面板都有对应导航入口和页面标题，避免从工作台跳转到隐藏页面时标题或高亮状态不一致。
- 静态测试会校验侧栏入口与页面面板一一对应，防止后续新增页面遗漏导航。

## 企业微信 PC 端真实 RPA

当前 RPA 已放弃截图/OCR 方案，主流程只走“页面登记会话名 -> UIA 搜索进入会话 -> UIA/剪贴板读取聊天文本”。RPA 支持两条链路：

- 发送链路：读取后台待发送任务，定位企微会话，粘贴/发送内容，回写发送日志。
- 同步链路：读取后台「企微会话」页面登记的会话名，逐个搜索进入会话，抽取聊天文本，回写数据库，并生成 AI回复、家庭画像、AI周报、打卡/PBL 结果。

```powershell
cd chongqing-coach-mvp
.\.venv\Scripts\Activate.ps1
```

如果执行 RPA 时出现 `ModuleNotFoundError: No module named 'pywinauto'`，说明当前命令用到了系统 Python，而不是项目虚拟环境。优先使用下面这种写法：

```powershell
.\.venv\Scripts\python.exe rpa\wecom_sender.py --diagnose
```

如果 `.venv` 中也缺依赖，先安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 1. 先启动后台服务

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 2. 在页面登记企微会话

打开后台：

```text
http://127.0.0.1:8000
```

进入「企微会话」，填写企微搜索框里能搜到的会话名，例如：

```text
艺博展讯
```

家庭编号、孩子年级、陪跑师可空。只需要第一次登记，后续 RPA 会从后台家庭列表读取这些会话名，不再扫描红点。

RPA 配置文件仍在：

```text
rpa/config.json
```

关键字段：

| 配置 | 说明 |
| --- | --- |
| `allowed_conversations` | 允许 RPA 真实发送的企微会话白名单；只影响发送，不影响同步 |
| `watch_conversations` | 旧未读监听字段，当前主流程不再依赖 |
| `ignored_conversations` | 不需要同步的企微会话名，RPA 遇到后跳过 |
| `conversation_family_map` | 兼容旧配置；当前建议直接在页面登记家庭 |
| `unknown_conversation_policy` | 未知会话处理策略：`prompt` / `ignore` / `skip` |
| `auto_launch_wecom` | 未找到企微窗口时是否尝试启动企业微信 |
| `wecom_executable_paths` | 企业微信可执行文件路径列表 |
| `dry_run` | `true` 只粘贴不发送，随后强制清空输入框；任务没有显式 `send_mode` 时仍作为默认试运行开关 |
| `allow_real_send` | 被控端本机真实发送硬开关；默认 `false`，不打开时即使任务是 `real_send` 也不会按发送键 |
| `auto_generate_ai_reply` | 同步未读消息后是否调用 AI回复 Agent |
| `auto_create_reply_task` | AI回复是否自动生成待发送任务 |
| `auto_generate_all_agents` | 同步后是否生成画像、周报、打卡/PBL 等全部 Agent 输出 |
| `auto_send_ai_replies` | 是否把本轮 AI回复任务直接交给 RPA 发送，默认关闭 |
| `use_clipboard_chat_extract` | UIA 读不到消息控件时，是否允许剪贴板读取能力存在 |
| `allow_clipboard_chat_extract` | 默认 `false`，避免误复制其他页面；只在命令行显式加 `--allow-clipboard-copy` 时开启 |
| `search_result_open_click_ratio_x` | 搜索结果已被 OCR/ARK 命中后，点击命中行的横向比例；未命中会直接中止，不再 Enter/坐标兜底 |

建议试点初期保持：

```json
{
  "dry_run": true,
  "allow_real_send": false,
  "auto_generate_ai_reply": true,
  "auto_create_reply_task": true,
  "auto_generate_all_agents": true,
  "auto_send_ai_replies": false
}
```

这样 RPA 会同步聊天并生成所有 AI 结果，但不会越过人工审核直接发送。

### 3. 诊断企微窗口和已登记会话

```powershell
.\.venv\Scripts\python.exe rpa\wecom_sender.py --diagnose
```

如果诊断提示没有找到企微窗口，请确认：

- 企业微信 PC 端已登录。
- 企业微信主窗口没有最小化。
- 如果企业微信是管理员权限启动，PowerShell 也需要用管理员权限启动。
- 后台服务 `http://127.0.0.1:8000` 已运行。

只检查窗口：

```powershell
.\.venv\Scripts\python.exe rpa\wecom_sender.py --check-window
```

### 4. 直接同步指定会话

```powershell
.\.venv\Scripts\python.exe rpa\wecom_sender.py --sync-target "艺博展讯"
```

执行后：

1. RPA 打开/定位企业微信。
2. 通过 UIA 进入搜索框，搜索 `艺博展讯`。
3. 进入对应会话。
4. 优先用 UIA 抽取聊天文本；默认不会自动 `Ctrl+A` / `Ctrl+C`，避免误复制其他页面。
4. POST 到 `/api/rpa/conversations/sync`。
5. 后端写入 `raw_messages`，生成 AI回复、画像、周报、打卡/PBL，保存到前端可见区域。
6. 如果 `auto_create_reply_task=true`，同时创建待发送任务。

### 5. 同步页面登记的全部会话

```powershell
.\.venv\Scripts\python.exe rpa\wecom_sender.py --sync-known
```

循环同步：

```powershell
.\.venv\Scripts\python.exe rpa\wecom_sender.py --watch-known
```

如果日志提示“已进入但 UIA/剪贴板都没有读到聊天文本”，说明当前企业微信版本没有把聊天记录暴露给 UIA，也没有允许复制聊天区文本。此时无需回到截图方案，优先考虑企业微信官方会话存档、聊天导出，或人工粘贴首轮历史记录。

确实需要尝试剪贴板读取时，必须显式加参数：

```powershell
.\.venv\Scripts\python.exe rpa\wecom_sender.py --sync-target "艺博展讯" --allow-clipboard-copy
```

脚本会在每次快捷键前确认前台窗口仍是 `WXWork.exe` 的企业微信窗口；如果焦点跑到浏览器、IDE 或其他页面，会立即停止。

### 6. 审核后发送

进入后台：

```text
http://127.0.0.1:8000
```

打开「待发送任务」，检查并编辑最终内容。点击页面里的 `发送` 后，任务内容会作为陪跑师消息写入网页通讯会话，并同步生成发送日志。

如果要用企微真实发送后台已有 pending 任务，需要任务已在控制端确认为 `real_send`，且被控端 `allow_real_send=true`：

```powershell
.\.venv\Scripts\python.exe rpa\wecom_sender.py
```

### 7. 自动发送 AI回复任务，仅建议小范围测试

确认白名单、坐标、会话定位都稳定后，再打开：

```json
{
  "allow_real_send": true,
  "auto_send_ai_replies": true
}
```

然后执行：

```powershell
.\.venv\Scripts\python.exe rpa\wecom_sender.py --reply-unread
```

这只会发送本轮未读同步中新建的 AI回复任务，不会批量扫历史 pending 任务。

### 8. 循环监听未读

```powershell
.\.venv\Scripts\python.exe rpa\wecom_sender.py --watch-unread
```

默认只同步并创建任务，不自动发送。轮询间隔由 `unread_poll_interval_seconds` 控制。

如果需要监听模式里直接执行 AI回复链路：

```json
{
  "auto_reply_in_watch_mode": true
}
```

### 9. 创建一条测试发送任务

```powershell
.\.venv\Scripts\python.exe rpa\wecom_sender.py --create-test-task --target "艺博展讯" --content "RPA真实发送测试"
```

再执行：

```powershell
.\.venv\Scripts\python.exe rpa\wecom_sender.py
```

## 当前边界

- mock 发送器不触碰企业微信；真实发送器会控制当前已登录的企业微信 PC 端。
- Agent 已支持豆包/火山 Ark；未配置 `ARK_API_KEY` / `ARK_ENDPOINT_ID` 时使用本地规则兜底。
- 企微 RPA 当前抽取“当前可见聊天文本”，不是企微官方会话存档；如需全量稳定同步，后续应接企微会话存档或 SCRM 接口。
- 定时任务先通过后台按钮触发，后续可加 APScheduler/Celery Beat。
