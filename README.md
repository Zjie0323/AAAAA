# A股量化工具集

一套面向 A 股短线交易的量化工具，由三个独立但协同的模块组成：**实时看板**、**竞价选股引擎**、**微信订阅推送**。

设计上刻意保持**零第三方依赖**（除 MCP 推送服务外全部使用 Python 标准库），部署时无需 pip / npm，只需 `python3`。

> ⚠️ 本项目仅用于研究学习，展示公开行情数据，**不构成任何投资建议**。

---

## 一、模块概览

| 模块 | 源码目录 | 职责 | 运行位置 | 部署路径 |
|---|---|---|---|---|
| **实时看板** | `astock-realtime/` | 行情服务 + Web 看板 + 竞价定盘快照 | 服务器常驻 | `/opt/astock-realtime` |
| **竞价选股引擎** | `astock-screen/` | 涨停基因池 → 次日观察名单 → 智能推荐 | 服务器盘后定时 | `/opt/astock-screen` |
| **微信订阅推送** | `astock-realtime/`（推送三件套）+ `python/mcp-subscribe-push/` | 定盘后把 TOP3 推送到微信 | Windows 本机定时 | 本机 |
| **部署物料** | `deploy/` | install.sh + systemd 单元 + 运维脚本 | — | — |

### 数据流

```
                    ┌─────────────────────────────────────────┐
   东财/腾讯接口 ──→ │ astock-screen/  live_limit.py  --fetch  │
                    │        ↓                                │
                    │  live_raw.json  →  gen_tomorrow.py      │
                    │        ↓                                │
                    │  tomorrow_watch.json  (次日观察名单)     │
                    └────────────┬────────────────────────────┘
                                 │ 读取
                    ┌────────────▼────────────────────────────┐
                    │ astock-realtime/server.py               │
                    │  /api/bid_watch → reco_engine.py 打分    │
                    │  09:25 定盘 → _bid_picks_YYYYMMDD.json   │
                    └────────────┬────────────────────────────┘
                                 │ 读取
                    ┌────────────▼────────────────────────────┐
                    │ auto_push_bid.py → mcp_wx_push.py       │
                    │  每交易日 09:25:40 推送 TOP3 到微信       │
                    └─────────────────────────────────────────┘
```

---

## 二、目录结构

```
.
├── astock-realtime/                 # 看板服务 + 推送（部署到 /opt/astock-realtime）
│   ├── server.py                    #   后端服务（行情接口 + /api/bid_watch + 定盘快照）
│   ├── index.html                   #   前端看板页面
│   ├── em_changes.py                #   东财接口字段变更探测
│   ├── ui_smoke_test.js             #   前端冒烟测试
│   ├── push_picks.py                # 【推送】消息渲染 + REST 通道下发
│   ├── mcp_wx_push.py               # 【推送】MCP 通道下发（复用 push_picks 渲染）
│   ├── auto_push_bid.py             # 【推送】定时调度执行器（四道护栏）
│   ├── auto_push_bid.bat            # 【推送】Windows 启动壳
│   ├── start.bat / start-local.bat  # 本地启动（后者用 WorkBuddy 内置 Python）
│   └── start.ps1
│
├── astock-screen/                   # 竞价选股引擎（部署到 /opt/astock-screen）
│   ├── em.py                        #   东财接口封装（单一事实源）
│   ├── live_limit.py                #   抓取当日涨停 / 炸板专题池
│   ├── limit_up.py                  #   重算历史涨停基因池（读 kdata/）
│   ├── gen_tomorrow.py              #   生成次日竞价观察名单
│   ├── reco_engine.py               #   竞价观察推荐引擎（六维分 / 赚钱效应分 / 买点门控）
│   ├── refresh_watch.py             #   刷新入口（盘后定时任务调用）
│   ├── gen_review.py                #   盘后复盘报告
│   ├── win_verify.py                #   分档胜率验证（累积样本）
│   ├── shadow_tiebreak.py           #   排序因子影子对比（换 tiebreaker 的对照实验）
│   ├── shadow_log.py                #   智能推荐排序口径 · 前向影子记录
│   ├── shadow_log.json              #   影子记录累积数据（入库，长期证据链）
│   ├── limit_up.json                #   涨停基因池（预置，325 KB，入库）
│   ├── raw_stats.json               #   code→name 映射（1.3 MB，入库）
│   └── kdata/                       #   1194 只个股日K缓存（22 MB，**不入库**）
│
├── python/mcp-subscribe-push/       # MCP 订阅推送服务（stdio）
│   ├── main.py                      #   MCP 服务主体
│   ├── push_once.py                 #   单次推送 CLI
│   ├── selfcheck.py                 #   stdio 协议四层自检
│   ├── requirements.txt             #   ⚠️ 须锁 mcp>=1.0.0,<2.0.0
│   └── .env.example                 #   凭据模板（真实 .env 不入库）
│
├── deploy/                          # 部署物料（进版本库）
│   ├── install.sh                   #   一键安装（幂等）
│   ├── deploy.conf                  #   唯一配置源（端口/用户/目录）
│   ├── README.md                    #   ★ 服务器部署与运维手册
│   ├── scripts/                     #   ctl-*.sh 启停与健康检查
│   ├── systemd/                     #   服务单元 + 盘后刷新 timer 模板
│   └── app/                         #   ← 构建产物，不入库（见下）
│
├── tools/                           # 构建工具
│   ├── build_deploy.py              #   源码 → deploy/app/（自动组装 + 自检）
│   └── pack_deploy.py               #   deploy/ → dist/*.tar.gz（含权限修正）
│
├── docs/                            # 文档
│   ├── 交易逻辑.txt                  #   策略与选股逻辑说明
│   ├── 使用说明.txt                  #   看板使用说明
│   └── 竞价推送模板设计.md            #   微信推送模板字段设计
│
├── dist/                            # 部署包产物，不入库
└── archive/                         # 历史脚本与数据归档，不入库
```

---

## 三、本地运行

### 实时看板

```bat
:: Windows：双击即用（start-local.bat 使用 WorkBuddy 内置 Python）
astock-realtime\start-local.bat

:: 或指定端口
astock-realtime\start-local.bat 8080
```

访问 `http://127.0.0.1:8000`。

> `start.bat` 用于系统 Python 环境；`start-local.bat` 用于本机 WorkBuddy 内置解释器。

### 生成次日观察名单（手动）

```bash
cd astock-screen
python refresh_watch.py            # 交易日盘后执行
python refresh_watch.py --force    # 忽略休市判定强制跑
```

### 排序因子影子对比（策略调参前必跑）

`score_pool()` 的 tiebreaker 决定了「同分票谁进 top5」。改它等于改推荐结果，
因此**必须先做影子对比**再上线：

```bash
cd astock-screen
python shadow_tiebreak.py          # 逐日抓 zt/zb 专题池 → _phist/ 缓存 → 断点续跑
```

指标口径（按用户实际关切排序）：

| 指标 | 定义 | 说明 |
|---|---|---|
| **可买晋级率** | 次日进涨停池 且 次日非 9:25 竞价封死 | **主口径**，剔除了根本买不进的票 |
| 晋级率 | 次日进涨停池 | 未剔除不可买样本，会高估 |
| 触板率 | 次日进涨停池 或 次日炸板池 | 反映「曾涨停」强度 |
| vs 全池 | 相对「当日涨停池全量」的超额 | **负值 = 策略没有 alpha** |

> ⚠️ 结论须过统计显著性（Fisher 精确检验）。多方案同批数据择优存在多重比较
> 问题：试 10 个方案时，最优那个天然偏高，p 值需按比较次数校正后再判断。

### 并行影子记录（前向盲测，决策前必看）

影子回测是在**已经看过的历史**上挑方案，天然乐观。因此候选口径不直接上线，
而是挂成影子**并行记录**，等它在"还没被看见的未来"上跑够样本再决定：

```bash
cd astock-screen
python shadow_log.py daily      # 盘后：结算上一交易日 + 记录今日（幂等）
python shadow_log.py backfill    # 一次性回填历史（标为 in-sample）
python shadow_log.py report      # 只看报告，不写数据
```

已挂到两条刷新入口（`refresh_watch.py` 与看板刷新按钮），**触发名单刷新即同步记录**；
影子记录失败不影响名单刷新（best-effort）。

| 产物 | 说明 |
|---|---|
| `shadow_log.json` | 累积记录（**入库**）：每日全池每只票的特征 + 次日晋级/触板/竞价封死 |
| `_shadow_log_report.txt` | 文本报告（运行时产物） |
| `_shadow_log_report.html` | 可视化报告（运行时产物） |

三个关键设计：

1. **记录的是全池特征，不是 top5** —— 因此日后任何新方案都能**离线复算**，
   不必重新抓数，也不受 `_score_stock` 打分逻辑变更影响。
2. **样本分组统计**：`in-sample`（回填历史，已参与挑方案）与 `forward`（前向盲测）
   分开算，**只有 forward 样本具备决策效力**；前向不足 10 个交易日时，任何差异
   都不足以支撑切换生产排序。
3. **同源**：排序键与池缓存直接复用 `shadow_tiebreak.py`，不复制实现，避免口径漂移。

用法上两条护栏：**15:05 前拒绝记录**（盘中涨停池仍在变动，记了会失真）、
**次日未收盘拒绝结算**（否则会拿盘中池子判定"次日是否晋级"）。

#### 定时触发

Windows 侧已注册计划任务 **`AStock-ShadowLog-1510`**（工作日 15:10，开启
`StartWhenAvailable` 以便错过开机时间后补跑），入口 `astock-screen/run_shadow_log.bat`：

```powershell
Get-ScheduledTask -TaskName "AStock-ShadowLog-1510" | Select-Object TaskName,State   # 查看
Unregister-ScheduledTask -TaskName "AStock-ShadowLog-1510" -Confirm:$false          # 卸载
```

> `schtasks.exe` 在本机被安全策略黑名单拦截（`PROGRAM BLOCKED BY SECURITY POLICY`），
> 注册/查询一律走 PowerShell cmdlet（`New-ScheduledTaskAction` + `Register-ScheduledTask`）。

---

## 四、构建与部署

### 构建部署包（两步）

```bash
python tools/build_deploy.py       # ① 从源码目录组装 deploy/app/
python tools/pack_deploy.py        # ② 打包为 dist/astock-realtime-deploy.tar.gz
```

`build_deploy.py` 内建三道自检：源侧文件齐全性、产物必需文件、**是否误带运行时名单**。
`pack_deploy.py` 会修正 Unix 权限位（`.sh` → 0755），否则解压后 `install.sh` 报
`Permission denied`。

> **为什么要有构建脚本**：此前 `deploy/app/` 是手工从源码目录拷贝的，长期必然漂移
> ——改了源码忘记同步，部署到服务器的还是旧代码且不易察觉。现在这一步是可重复的单一入口。

### 部署到服务器

```bash
# 1. 上传并解压
scp dist/astock-realtime-deploy.tar.gz user@server:/tmp/
ssh user@server
tar -xzf /tmp/astock-realtime-deploy.tar.gz -C /tmp
cd /tmp/astock-realtime-deploy

# 2. 一键安装（需 root）
sudo ./install.sh

# 3. 验证
astock-realtime-check
```

详细的运维命令、配置修改、排障手册见 **`deploy/README.md`**。

### Windows 定时推送（本机）

已注册计划任务 `AStock-BidPush-0925`，每交易日 09:25:40 触发。
任务定义调用 `<仓库根>\astock-realtime\auto_push_bid.bat`（含绝对路径，**移动该目录会使任务失效**）。

```bat
:: 手动回放（不消耗配额）
astock-realtime\auto_push_bid.bat --now 09:25:40 --dry-run

:: 切回 REST 通道
astock-realtime\auto_push_bid.bat --channel rest
```

---

## 五、数据与凭据

### 不入库的内容及原因

| 内容 | 原因 |
|---|---|
| `astock-screen/kdata/`（22 MB / 1194 文件） | 体积大；服务器侧自行维护。全新环境可从旧环境拷贝，或跑 `limit_up.py` 重新抓取 |
| `astock-screen/tomorrow_watch*.json`、`live_raw*.json` | **每日行情产物**。固定打包会过期误导 —— 基准日错位会让次日复盘退化成「自我对照」（晋级率/红盘率双双 100%），结论完全失真 |
| `astock-screen/_review*.json`、`review_*.html` | 复盘数据与报告，可由 `gen_review.py` 重新生成 |
| `astock-realtime/_changes_buffer.json`、`_bid_picks_*.json`、`_auto_push_state_*.json` | 运行时状态，服务进程持续改写 |
| `deploy/app/`、`dist/` | 构建产物，可由 `tools/` 脚本重新生成 |
| `archive/` | 本地归档，仅供追溯 |
| `.workbuddy/` | 工作区记忆，可能含内网地址与凭据 |

### 凭据

- `python/mcp-subscribe-push/.env` —— 含后端账号密码，**已被 `.gitignore` 排除**。
  首次使用请 `cp .env.example .env` 并填入真实值。
- 推送服务的 Bearer 令牌存于 `~/.workbuddy/mcp.json`（本机用户级配置，不在本仓库内）。
- 源码中**无任何硬编码凭据**，全部走环境变量。

---

## 六、改动前必读：两个目录名不能改

`astock-realtime/` 与 `astock-screen/` 这两个目录名**存在双向跨目录引用**：

| 引用方 | 被引用 | 用途 |
|---|---|---|
| `astock-realtime/server.py` | `../astock-screen/` | 读 `tomorrow_watch.json` + `import reco_engine` |
| `astock-screen/refresh_watch.py` | `../astock-realtime/` | 复用 `server.market_open_today()` 做开市判定 |

服务器上这两个目录固定为 `/opt/astock-realtime` 与 `/opt/astock-screen`，
因此本地目录名必须保持一致，否则开发期就无法自测。

此外，`install.sh` 的 `SCREEN_DIR="$(dirname "$INSTALL_DIR")/astock-screen"` 也依赖此约定。

---

## 七、已知注意事项

| 项 | 说明 |
|---|---|
| `requirements.txt` 版本上限 | 必须锁 `mcp>=1.0.0,<2.0.0`。mcp 2.x 移除了 `Server.list_tools()` 装饰器，装到 2.x 会直接崩溃 |
| 行尾 | 由 `.gitattributes` 锁定：shell/python/json → LF，bat/ps1 → CRLF。避免 Windows 开发、Linux 部署时报 `bad interpreter: /bin/bash^M` |
| 看板进程 | 单实例常驻即可，**不要用 cron 反复拉起**（server.py 内置后台刷新线程，HTTP 请求只读内存缓存） |
| 9:25 定盘 | 定盘快照由服务进程在 9:25–9:30 窗口写入，**9:20 前必须已完成重启**，否则跑的是旧代码 |

---

## 免责声明

本项目仅展示公开行情数据，仅供研究学习参考，**不构成任何投资建议**。
投资有风险，决策需谨慎。请自行承担交易风险。
