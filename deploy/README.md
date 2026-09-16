# A股实时看板 · 服务器部署包

后台常驻 + 开机自启 + 一键启停 + 健康检查。零第三方依赖，仅需 Python 3.7+。

---

## 一、包内容

```
astock-realtime-deploy/
├── deploy.conf                     唯一配置源（端口/用户/目录，改这里）
├── install.sh                      安装脚本（一键部署 + 自检）
├── README.md                       本文件
├── app/
│   ├── server.py                   后端服务（行情接口 + 竞价看板，规避浏览器 CORS）
│   ├── index.html                  前端看板页面
│   └── screen/                     竞价名单工具链（仅 Python 标准库，无需 pip/npm）
│       ├── refresh_watch.py        刷新入口：交易日判定 → 抓取 → 生成 → 双重校验
│       ├── gen_tomorrow.py         生成次日竞价观察名单（tomorrow_watch.json）
│       ├── live_limit.py           抓取当日涨停 / 炸板专题池
│       ├── limit_up.py             重算历史涨停基因池（读 kdata/）
│       ├── em.py                   东方财富接口封装（单一事实源）
│       ├── reco_engine.py          智能推荐引擎（六维分 / 赚钱效应分 / 买点门控）
│       ├── limit_up.json           近1年涨停基因池（预置，免首次重算）
│       ├── raw_stats.json          候选池 code→name 映射
│       └── kdata/                  1194 只个股近1年日K缓存（供基因池重算）
├── systemd/
│   ├── astock-realtime.service.template          主服务单元模板
│   ├── astock-realtime-refresh.service.template  盘后名单刷新单元（oneshot）
│   └── astock-realtime-refresh.timer.template    刷新定时器（每交易日 15:10）
└── scripts/
    ├── lib.sh                      公共函数库（被其他脚本引用）
    ├── ctl-start.sh                启动
    ├── ctl-stop.sh                 停止
    ├── ctl-restart.sh              重启（含健康检查）
    ├── ctl-status.sh               状态（进程/端口/资源/日志）
    ├── ctl-check.sh                健康检查（4 层校验，核心验证工具）
    ├── ctl-uninstall.sh            卸载
    └── ctl-nohup.sh                nohup 模式备用启停（无 systemd 时用）
```

**架构要点**：server.py 内置后台刷新线程（涨停/跌停 15s、板块 30s），
HTTP 请求只读内存缓存，与上游频率解耦；上游限频时指数退避（15s→120s 封顶）
并降级返回缓存快照（`stale=true`），页面不报错。因此**单实例常驻即可**，
不要用 cron 反复拉起。

---

## 二、快速部署（3 条命令）

```bash
# 1. 上传并解压到服务器（示例路径 /tmp）
tar -xzf astock-realtime-deploy.tar.gz -C /tmp
cd /tmp/astock-realtime-deploy

# 2. 一键安装（需要 root）
sudo ./install.sh

# 3. 验证
astock-realtime-check
```

安装脚本自动完成：探测 Python → 创建运行用户 `astock`（禁止登录）→
部署代码到 `/opt/astock-realtime` → 部署竞价名单工具链到 `/opt/astock-screen` →
安装 systemd 单元与**盘后刷新定时器** → 设置开机自启 → 启动服务 → 打印验证地址。

### 前置条件

| 项 | 要求 | 检查命令 |
|----|------|---------|
| 操作系统 | 有 systemd 的 Linux（CentOS 7+/Ubuntu 16+/麒麟 V10） | `systemctl --version` |
| Python | 3.7 或更高 | `python3 -V` |
| 权限 | root 或 sudo | `id -u` |
| 磁盘 | ≥ 100 MB（含 22 MB 日K缓存与名单历史副本） | `df -h /opt` |
| 网络 | 能访问 `*.eastmoney.com`（出站 443） | `curl -I https://push2.eastmoney.com/` |
| 网络（竞价推荐） | 能访问 `web.ifzq.gtimg.cn`（出站 443，腾讯指数/日K） | `curl -I https://web.ifzq.gtimg.cn/` |

**CentOS 7 注意**：系统自带 python2，需先装 python3：
```bash
sudo yum install -y python3
```
**麒麟 V10 / 内网服务器**：若无外网 DNS，需确认能解析 `push2.eastmoney.com`，
否则看板数据为空（服务本身仍能启动）。

---

## 三、验证地址

安装完成后：

| 场景 | 地址 |
|------|------|
| 服务器本机 | `http://127.0.0.1:8000` |
| 局域网其他机器 | `http://<服务器IP>:8000` |

获取服务器 IP：
```bash
ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}'
```

### 防火墙放行（局域网访问必做）

```bash
# firewalld（CentOS 7 / 麒麟）
sudo firewall-cmd --permanent --add-port=8000/tcp && sudo firewall-cmd --reload

# ufw（Ubuntu）
sudo ufw allow 8000/tcp

# iptables 直连
sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT
```

### 验证接口清单

| 接口 | 用途 | 期望 |
|------|------|------|
| `/` | 看板主页 | HTTP 200，HTML |
| `/api/limit_up` | 涨停个股 | HTTP 200，JSON `{"ok":true,...}` |
| `/api/limit_down` | 跌停个股 | HTTP 200 |
| `/api/sectors` | 板块资金流 | HTTP 200 |
| `/api/trends?code=000001` | 个股分时（含分钟量柱） | HTTP 200 |
| `/api/kline?code=000001&days=60` | 个股日K（蜡烛 + MA5/10/20 + 日量柱） | HTTP 200，数据源腾讯日K，东财兜底 |
| `/api/bid_watch` | 竞价观察名单 + TOP3 精选（含买点门控） | HTTP 200，读 `/opt/astock-screen/tomorrow_watch.json` |
| `/api/refresh_watch` | 手动触发名单刷新（后台线程执行，约 1-3 分钟） | HTTP 200，`{"ok":true,"started":true}` |
| `/api/rebuild_genepool` | 重算历史涨停基因池（秒级，本地计算） | HTTP 200 |

> **关于 `/api/bid_watch`**：该接口读取 `../astock-screen/tomorrow_watch.json`，
> 并加载 `../astock-screen/reco_engine.py` 计算六维分、赚钱效应分与买点门控。
> 两者均由 `install.sh` 一并部署，**名单由盘后定时器自动生成**（见第四节）。
> 仅当定时器尚未跑过（例如安装当天 15:10 之前）时返回 502，
> 此时执行一次 `sudo astock-realtime-refresh` 即可。

手动验证：
```bash
curl -s http://127.0.0.1:8000/api/limit_up | head -c 300
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/
```

---

## 四、启停与运维命令

安装脚本会把以下命令装到 `/usr/local/bin`，**任意目录直接调用**：

| 命令 | 作用 |
|------|------|
| `astock-realtime-start` | 启动（已运行则提示，不重复启动） |
| `astock-realtime-stop` | 停止（SIGINT 优雅退出，10s 后强杀） |
| `astock-realtime-restart` | 重启并自动健康检查 |
| `astock-realtime-status` | 状态：进程/端口/内存/CPU/最近日志 |
| `astock-realtime-check` | **健康检查**：进程→端口→HTTP→上游 4 层校验 |
| `astock-realtime-port` | **改端口**（查看/修改，含自动回滚） |
| `astock-realtime-refresh` | **手动生成次日竞价名单**（与盘后定时任务同一入口） |
| `astock-realtime-uninstall` | 卸载（加 `--purge` 连数据一起删） |

原生 systemd 操作（等价可用）：

```bash
sudo systemctl start   astock-realtime     # 启动
sudo systemctl stop    astock-realtime     # 停止
sudo systemctl restart astock-realtime     # 重启
sudo systemctl status  astock-realtime     # 状态
sudo systemctl enable  astock-realtime     # 开机自启
sudo systemctl disable astock-realtime     # 取消自启
journalctl -u astock-realtime -f           # 实时日志
journalctl -u astock-realtime --since "10 min ago"   # 近 10 分钟日志
```

### 竞价观察名单（自动生成，无需人工维护）

`/api/bid_watch` 的数据源 `/opt/astock-screen/tomorrow_watch.json`，由 systemd 定时器
**每交易日 15:10 自动生成**：周末由 `OnCalendar` 排除，法定节假日由脚本内部的开市判定排除。

| 项 | 值 |
|----|-----|
| 定时器 | `astock-realtime-refresh.timer` |
| 触发时间 | 每交易日 15:10（`RandomizedDelaySec=120` 打散整点负载） |
| 错过补跑 | `Persistent=true` —— 关机期间错过的任务，开机后补跑一次 |
| 数据链路 | 东财涨停专题池 → `live_raw.json` → 基因池打分 → `tomorrow_watch.json` |
| 产物目录 | `/opt/astock-screen/`（名单 + 按日历史副本 + HTML） |
| 服务身份 | `astock`（非 root），`Type=oneshot`，跑完即退出，不常驻 |

```bash
# 手动刷新（排障/补跑；休市日会打印「今日休市，跳过刷新」并直接结束）
sudo astock-realtime-refresh

# 查看定时器与下次触发时间
systemctl list-timers astock-realtime-refresh.timer

# 查看刷新日志（含每一步取数结果与退出码）
journalctl -u astock-realtime-refresh -n 50 --no-pager

# 确认名单基准日（应等于最近一个交易日）
python3 -c "import json;d=json.load(open('/opt/astock-screen/tomorrow_watch.json'));print(d['base_date'],len(d['list']))"
```

**为什么名单不放进部署包**：名单是**每日变化的行情产物**，固定打包会过期误导
（基准日错位会让次日复盘退化成"自我对照"，结论完全失真）。因此部署包只带
**基因池（`limit_up.json`）与日K缓存（`kdata/`）** 这两类慢变数据，
名单一律由服务器本地盘后生成，且生成前后各做一次日期校验。

**刷新异常对照**：

| 现象 | 含义 | 处理 |
|------|------|------|
| 日志「今日休市，跳过刷新」 | 正常（节假日） | 无需处理，继续用上一交易日名单 |
| 日志「涨停快照日期 X != 今日」 | 上游数据未出或接口延迟 | 脚本**主动拒绝生成错误名单**；收盘后重跑 |
| 日志「抓取涨停池失败」 | 上游不可达 | `curl -I https://push2.eastmoney.com/` 测出站 |
| `/api/bid_watch` 仍 502 | 名单始终未生成 | 确认目录可写：`ls -l /opt/astock-screen`（应属主 `astock`） |
| 定时器到点未触发 | timer 未启用 | `sudo systemctl enable --now astock-realtime-refresh.timer` |

---

## 五、配置修改

**只改 `deploy.conf`，不要改脚本内部**。修改后重新执行 `sudo ./install.sh` 生效。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `APP_NAME` | `astock-realtime` | 服务名（unit 名 / 命令前缀） |
| `INSTALL_DIR` | `/opt/astock-realtime` | 代码部署路径 |
| `LOG_DIR` | `/var/log/astock-realtime` | 日志路径 |
| `SERVE_PORT` | `8000` | **监听端口**（可自定义，见下方专节） |
| `RUN_USER` / `RUN_GROUP` | `astock` | 运行身份（非 root，降低风险） |
| `STOP_TIMEOUT` | `10` | 优雅停止等待秒数 |
| `SERVE_HOST` | `0.0.0.0` | ⚠️ **无效项**，仅作记录。server.py 内部硬编码绑定 `0.0.0.0`，改此值不生效；限制来源 IP 请用防火墙（见下方） |

### 如何修改监听端口 ✅ 支持自定义

端口是**单一配置项**，集中定义在 `deploy.conf` 的 `SERVE_PORT`，所有脚本动态读取。
推荐用专用命令修改（自动校验 → 改配置 → 重载单元 → 重启验证，**失败自动回滚**）：

```bash
# 查看当前端口（同时显示 systemd 单元实际生效值，便于发现不一致）
sudo astock-realtime-port

# 改为 8080
sudo astock-realtime-port 8080
```

该命令自动完成：

1. 校验端口合法性（1-65535）、检查新端口是否被占用（占用则中止）
2. 备份 `deploy.conf`
3. 更新 `SERVE_PORT`，并同步安装目录下的副本
4. 重渲染 systemd 单元的 `ExecStart` 端口 → `daemon-reload`
5. 重启服务 → 请求 `http://127.0.0.1:<新端口>/` 验证 HTTP 200
6. 验证失败**自动回滚到原端口**并重启，不会把服务留在坏状态

改完后同步防火墙（脚本结束时会打印这两条命令）：

```bash
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --permanent --remove-port=8000/tcp   # 可选，关闭旧端口
sudo firewall-cmd --reload
```

> **⚠️ 常见坑：直接改 `deploy.conf` 再 `systemctl restart` 为什么不生效？**
> systemd 的 `ExecStart` 里的端口是**安装时用 sed 注入的静态值**，
> 单独改配置文件后重启，服务仍监听旧端口。
> 正确做法：用 `astock-realtime-port`，或改完配置后重跑 `sudo ./install.sh` 重新生成单元。
> 用 `sudo astock-realtime-port`（不带参数）可以对比"配置值 vs 单元实际值"，快速判断是否踩坑。

### 其他改端口方式（等价）

**方式二：改配置 + 重装**（会重启服务，保留数据与日志）
```bash
sed -i 's/^SERVE_PORT=8000/SERVE_PORT=8080/' deploy.conf
sudo ./install.sh
```

**方式三：一次性临时改**（不改配置，仅本次运行，重启后恢复）
```bash
sudo systemctl stop astock-realtime
sudo -u astock /usr/bin/python3 /opt/astock-realtime/server.py 8080
```

**方式四：nohup 模式**（无 systemd 环境），端口通过环境变量传入
```bash
APP_DIR=/opt/astock-realtime/app SERVE_PORT=8080 ./scripts/ctl-nohup.sh restart
```

### 仅允许本机访问
server.py 内部固定绑定 `0.0.0.0`，如需限制来源，用防火墙而非改代码：
```bash
# 只允许 10.21.0.0/16 网段访问
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="10.21.0.0/16" port port="8000" protocol="tcp" accept'
sudo firewall-cmd --permanent --remove-port=8000/tcp
sudo firewall-cmd --reload
```

---

## 六、通用排障

按 **从下往上** 的顺序排查：

| 现象 | 定位 | 处理 |
|------|------|------|
| `systemctl status` 显示 `failed` | 看日志 | `journalctl -u astock-realtime -n 50 --no-pager` |
| 报 `python: command not found` | Python 缺失 | `sudo yum install -y python3` 后重装 |
| 报 `Address already in use` | 端口被占 | `ss -lntp \| grep 8000` 找占用进程 |
| 服务 active 但页面打不开 | 防火墙 | 见第三节防火墙放行 |
| 页面打开但 `Failed to fetch` | 直接双击了 index.html | 必须走 `http://IP:8000`，不能 `file://` 打开 |
| 数据空白/一直加载 | 上游不可达 | `curl -I https://push2.eastmoney.com/` 测出站 |
| 字段显示 `--` 且提示降级 | push2ex 专题池限频 | 正常降级行为，等下一轮刷新（≤120s）自动恢复 |
| 竞价页提示「名单未生成」 | 名单文件缺失 | `sudo astock-realtime-refresh`（见第四节） |
| 反复重启 | 启动即崩溃 | `journalctl -u astock-realtime -n 100` 看堆栈 |

**判断"数据空"还是"服务坏"**：
```bash
# 服务坏了 → 返回非 200 或连接拒绝
curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8000/

# 服务正常但上游有问题 → 返回 200 且 stale=true
curl -s http://127.0.0.1:8000/api/limit_up | grep -o '"stale":[^,]*'
```

---

## 七、无 systemd 环境（容器 / 精简系统）

用 `scripts/ctl-nohup.sh`：

```bash
cd /tmp/astock-realtime-deploy
APP_DIR=$(pwd)/app ./scripts/ctl-nohup.sh start     # 启动
APP_DIR=$(pwd)/app ./scripts/ctl-nohup.sh status    # 状态
APP_DIR=$(pwd)/app ./scripts/ctl-nohup.sh check     # 健康检查
APP_DIR=$(pwd)/app ./scripts/ctl-nohup.sh log       # 跟踪日志
APP_DIR=$(pwd)/app ./scripts/ctl-nohup.sh stop      # 停止
```

开机自启（二选一）：
```bash
# 方式一：crontab
echo "@reboot cd /tmp/astock-realtime-deploy && APP_DIR=$(pwd)/app ./scripts/ctl-nohup.sh start" | crontab -

# 方式二：/etc/rc.local（需 chmod +x /etc/rc.local）
```

**竞价名单生成**（无 systemd 就没有 timer，需自建 cron。脚本内部自带开市判定，
休市日会打印「今日休市」并直接退出，因此可放心按「工作日」粗排）：

```bash
# 每工作日 15:10 生成次日名单；输出追加到日志便于排障
sudo tee /etc/cron.d/astock-refresh >/dev/null <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
10 15 * * 1-5 astock /usr/bin/python3 /opt/astock-screen/refresh_watch.py >> /var/log/astock-refresh.log 2>&1
CRON
```

> 需确保该用户对 `/opt/astock-screen/` 有写权限（install.sh 已 chown 给运行用户）。

---

## 八、升级与回滚

**升级**（保留配置与日志）：
```bash
tar -xzf astock-realtime-deploy-v2.tar.gz -C /tmp
cd /tmp/astock-realtime-deploy            # 新版目录
# 如需保留原端口，先把旧 deploy.conf 拷过来
sudo cp /opt/astock-realtime/deploy.conf ./deploy.conf
sudo ./install.sh                         # 幂等，直接覆盖代码
```

**回滚**：
```bash
sudo cp /path/to/backup/server.py      /opt/astock-realtime/server.py
sudo cp /path/to/backup/index.html     /opt/astock-realtime/index.html
sudo systemctl restart astock-realtime
astock-realtime-check
```

---

## 九、安全说明

部署包在 systemd 单元中内建了以下加固项（`systemd/astock-realtime.service.template`）：

| 加固项 | 作用 |
|--------|------|
| 非 root 运行（`User=astock`） | 进程被攻破也无法直接操作系统 |
| `NoNewPrivileges=true` | 禁止通过 setuid 提权 |
| `ProtectSystem=full` | `/usr`、`/boot`、`/etc` 只读 |
| `ProtectHome=true` | 隐藏 `/home` 内容 |
| `PrivateTmp=true` | 独立 `/tmp` 命名空间 |
| `RestrictAddressFamilies` | 仅允许 IPv4/IPv6/Unix socket |
| `ReadWritePaths` 白名单 | 仅日志目录被显式列为可写（`/opt` 不在 `ProtectSystem=full` 的保护范围内，故名单与定盘快照可落盘） |

盘后刷新单元（`astock-realtime-refresh.service`）采用**同档加固**，区别仅在 `Type=oneshot`：
任务跑完即退出，不常驻内存。

服务与刷新任务仅需**出站 443**（东方财富 / 腾讯接口）与**本地监听 8000**，
无需任何入站的额外权限。
如需进一步限制，可在防火墙上把入站 8000 限定到管理网段（见第五节）。

---

## 免责声明

本看板仅展示公开行情数据，仅供研究学习参考，**不构成任何投资建议**。
投资有风险，决策需谨慎。
