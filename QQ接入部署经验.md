# QQ 接入部署经验（OneBot v11 + NapCatQQ）

> 记录一次真实打通「念念 niannian → QQ」的完整过程与踩坑，供后续复现与排错。
>
> **合规说明**：本项目只实现 OneBot v11 开放协议**客户端**，不包含、不打包、不分发任何第三方协议端。
> NapCat（协议端）由用户自行下载部署，安装目录刻意放在仓库之外的 `C:\Users\<你的用户名>\NapCat`，规避侵权风险。
> 本文档中的 QQ 号、WebUI token 等均已脱敏为占位符。

---

## 一、目标与结论

**目标**：把「念念」接到真实 QQ，验证收发消息、发表情包、戳一戳。

**结论**：已打通。链路为

```
手机QQ ──> NapCat(协议端) ──正向WS(:3001)──> QQAdapter ──事件总线──> 会话引擎 ──> 回复分条 ──> NapCat ──> 手机QQ
```

实测证据（服务端日志）：

```
16:36:24  会话状态 → perceiving         # 收到 QQ 消息
16:36:36  意愿闸门 score=0.401 → terse  # 状态机判定回复意愿
16:36:37  表达：1字 → 1 条气泡           # 生成并分条
16:36:38  会话状态 → speaking → idle     # 发出
```

---

## 二、环境

| 项 | 值 |
| --- | --- |
| 系统 | Windows 11 专业版 build 26300 |
| 本机 QQ | QQ NT `9.9.32.51246`（`C:\Program Files\Tencent\QQNT`） |
| Shell 权限 | 非管理员（很多需要 UAC 的脚本会另开窗口、抓不到输出） |
| Node / Docker / git / curl | 均可用 |

---

## 三、实现端选型（关键决策）

NapCat 官方提供三种部署方式，实测结论：

| 方式 | 说明 | 结论 |
| --- | --- | --- |
| `NapCat.Shell.zip`（Shell 手动） | 需另装 QQ，靠 `qqnt.json` 声明目标 QQ 版本 | ❌ **失败**，见坑 1 |
| `NapCat.Shell.Windows.OneKey.zip`（Windows 一键） | 自带匹配版本 QQ，从腾讯 CDN 下载并自解压为独立实例 | ✅ **采用** |
| Framework（LiteLoaderQQNT） | 以插件形式挂进现有 QQ | 未采用（改动现有 QQ，风险高） |

**OneKey 的三个好处**：自带匹配版本的 QQ（不会撞版本）；免 UAC；独立实例，不污染本机已装的 QQ。

---

## 四、完整部署步骤

1. **下载 OneKey 包**（GitHub 直连在国内会被 reset，用镜像前缀 `https://ghfast.top/`）
2. 解压后运行 `NapCatInstaller.exe`，它会从腾讯 CDN 拉取匹配版 QQ 并自解压出 `NapCat.52230.Shell/`（含自带 QQ `9.9.33-52230`）
3. 启动实例：`napcat.bat`（等价于 `.\NapCatWinBootMain.exe`），或 `NapCatWinBootMain.exe <QQ号>` 走快速登录
4. **扫码登录**：二维码出现在控制台（ASCII）、`cache\qrcode.png`，或 WebUI `http://127.0.0.1:6099/webui?token=<token>`
5. 配置 `config\onebot11_<QQ号>.json` 的 `network.websocketServers`（结构见第六节）
6. **重启 NapCat 实例**，新配置才会生效（见坑 2）
7. 项目侧 `config.toml` 的 `[qq]` 打开 `enabled = true`、对好 `ws_url`，**并且同步改 `data/settings.json`**（见坑 4）
8. 重启念念服务，验证日志出现 `QQ 已连接 OneBot 端点`

---

## 五、踩坑与根因（重点）

### 坑 1：QQ NT 版本不匹配 → hook 静默失败

- **现象**：`NapCatWinBootMain` 输出 `[NapCat Backend] Process resumed` 后**再无任何日志**，6099 不监听、无二维码、无 `webui.json`。看起来像"启动了但什么都没发生"。
- **根因**：Shell 包内的 `qqnt.json` 声明 `"version": "9.9.22-40990"`，而本机 QQ 是 `9.9.32.51246`。版本对不上，注入静默失败，**不报错**。
- **解决**：改用 OneKey 版，自带匹配版本 QQ。
- **教训**：NapCat 的版本兼容性是硬约束，**报错方式极隐蔽**（静默失败）。一旦遇到"启动了但没反应"，先核对 `qqnt.json` 声明的版本 vs 实际 QQ 版本。

### 坑 2：NapCat 没有配置文件热重载

- **现象**：改完 `onebot11_*.json` 的 `websocketServers`，3001 端口始终不监听。
- **排查**：grep `napcat.mjs` 源码，**没有** `chokidar` / `fs.watch` / `ConfigWatch`；只有 WebUI 的 `SetConfig` 会触发 `reloadNetwork`。
- **WebUI 走不通**：`/api/OB11Config/SetConfig` 需要 JWT 凭证；`secretKey` 是 `Math.random()` 随机生成（`NAPCAT_WEBUI_JWT_SECRET_KEY || random`），外部无法伪造。
- **解决**：**重启实例**。杀了 NapCat 的 QQ 进程 + `NapCatWinBootMain.exe`，重新启动。
- **教训**：NapCat 的配置是**启动时加载**的。改配置文件 ≠ 生效。

### 坑 3：WS 服务端在 QQ 登录成功之后才启动

- **现象**：重启后 3001 依然不监听，直到扫码登录完成才监听。
- **根因**：源码里 `await this.networkManager.openAllAdapters()` 位于**登录成功回调**内。
- **教训**：调试 WS 端口不监听时，先确认 QQ 是否已登录——"没登录"和"没配置"现象一样。

### 坑 4：`settings.json` 覆盖 `config.toml`（项目侧最坑的一个）

- **现象**：`config.toml` 里 `[qq] enabled = true` 改好了，重启服务后日志**完全没有 QQ 相关输出**。
- **根因**：`_resolve_qq_config()` 的优先级是 **`data/settings.json` > `config.toml`**，而 `settings.json` 里存着 `qq.enabled = false`（网页端上次保存的），把 toml 覆盖掉了。
- **排查手法**：`GET /api/qq` 看快照。若 `enabled=false / status=disabled`，说明被 settings.json 覆盖。
- **教训**：**本项目网页端保存的运行时配置优先于 config.toml**。改了 toml 不生效时，先去 `data/settings.json` 看有没有同名覆盖项。

### 坑 5：其它小坑

| 现象 | 原因 / 解决 |
| --- | --- |
| PowerShell 报 `Cannot overwrite variable PID because it is read-only` | `$pid` 是只读内置变量，改名 `$procId` |
| `curl` 下载 GitHub 报 `(56) Connection was reset` | 用镜像前缀 `https://ghfast.top/` |
| 工具沙箱禁止 `cmd /c` | 改用 PowerShell 直接调用可执行文件 |
| `-Verb RunAs` 提权后输出另开窗口、抓不到 | 改用免 UAC 的 OneKey 方案 + 后台 job 捕获 stdout |

---

## 六、关键配置与路径速查

### OneBot v11 正向 WS 服务端配置

写入 `versions\<版本>\resources\app\napcat\config\onebot11_<QQ号>.json`：

```json
{
  "network": {
    "websocketServers": [
      {
        "name": "niannian",
        "enable": true,
        "host": "127.0.0.1",
        "port": 3001,
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "token": "",
        "enableForcePushEvent": true,
        "debug": false,
        "heartInterval": 30000
      }
    ]
  }
}
```

> `host` 用 `127.0.0.1`（项目侧是**正向 WS 客户端**，主动连这个服务端）。
> `name` 字段用于配置变更时比对，改成能识别的名字便于排查。

### 关键路径

| 用途 | 路径 |
| --- | --- |
| 实例根目录 | `C:\Users\<你的用户名>\NapCat\onekey\NapCat.52230.Shell` |
| 启动脚本 | `napcat.bat` / `napcat.quick.bat` |
| OneBot 配置 | `...\versions\9.9.33-52230\resources\app\napcat\config\onebot11_<QQ号>.json` |
| WebUI token | 同目录 `webui.json` 的 `token` 字段 |
| 登录二维码 | `...\napcat\cache\qrcode.png`（约 2 分钟自动刷新） |
| 账号数据目录 | `C:\Users\<你的用户名>\Documents\Tencent Files\NapCat\data` |
| 项目侧配置 | `config.toml` 的 `[qq]` + `data/settings.json` 的 `qq` 段 |

### 重启命令

```powershell
# NapCat（已登录过可用 quick 走快速登录）
& 'C:\Users\<你的用户名>\NapCat\onekey\NapCat.52230.Shell\NapCatWinBootMain.exe' <QQ号>

# 念念（项目根目录）
& '.\.venv\Scripts\python.exe' -m companion.main
```

---

## 七、排错清单（按现象查）

| 现象 | 先查这里 |
| --- | --- |
| NapCat 启动了但没任何反应 | `qqnt.json` 声明的 QQ 版本 vs 实际版本（坑 1） |
| 改了 onebot 配置但端口不监听 | 是否重启了实例（坑 2）；QQ 是否已登录（坑 3） |
| 3001 一直不监听 | 先确认 QQ 登录成功；再确认 `websocketServers[].enable` |
| 念念日志没有 QQ 字样 | `data/settings.json` 的 `qq.enabled`（坑 4） |
| 二维码老是失效 | 用 WebUI 页面里的自动刷新码，别用控制台静态图 |
| 发了消息她不理 | `GET /api/qq` 看 `status`；确认 `797` 端口连通、白名单 `allow_users` |
| 她收不到消息 | `allow_users` 是否把对方 QQ 号挡在外面（空数组 = 不限） |

---

## 八、遗留事项

1. **表情包目前发不出去**：素材已从收藏拉取入库（42 张），但 `label` / `tags` 全为空——
   模型只认标签、看不到图，`build_sticker_block()` 会把空标签兜底成"未命名"，
   `find_by_label_or_tag()` 在任何 key 下都命中不了，因此一张都发不出。
   **解法**：在孕育页「表情」标签页给常用几张补 label/tags（每轮实时生效，无需重启）。
2. **`get_login_info` 无回包**：服务端 `bot_uin` 显示为 `null`，孕育页 QQ 卡片显示不出登录身份/昵称。
   不影响收发主链路（事件里的 `self_id` 会兜底），但待排查。
