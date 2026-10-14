# Spec：QQ 发表情包 + 戳一戳（参考 Derpyu520/qq-bridge）

> 目标项目：`c:\Users\86153\Desktop\KKcc`（念念 niannian）
> 状态：待用户批准后实施。

## 1. 目标与范围

让生命体在 QQ 私聊（OneBot v11）里能做两件"特殊操作"：

1. **发表情包**：从该生命体绑定的 QQ 号「收藏表情」中挑一张发出（出站 `image` 段）。
2. **戳一戳**：她能主动戳对方（出站 `send_poke`），且**对方戳她时她会有反应**（入站 `notice/notify/poke`）。

非目标（不做）：群聊、图片入站解析、表情包"图文同气泡"、自动下载/打包任何第三方 OneBot 实现端。

合规硬约束：仓库仍**不包含、不打包、不分发**任何 QQ 协议端（NapCat 等）。表情库数据落在 `data/stickers/`（`data/` 已被 `.gitignore` 忽略，天然不入库）。

## 2. 触发与决策（已确认）

| 决策点 | 结论 |
| --- | --- |
| 触发方式 | **模型标记为主**：模型在回复中输出独立行 `[表情:标签]` / `[戳一戳]`，后端解析为特殊气泡 |
| 表情素材来源 | **QQ 收藏表情库**（`fetch_custom_face`）；出站发 `image` 段（URL 直发，无需 base64） |
| 含义标注 | NapCat 拿不到官方 desc → **用户在控制台手动补 label/tags** |
| 气泡形态 | 表情**单独一个气泡**（不与文本同气泡） |
| 表情库管理入口 | **控制台·孕育页**（新增「表情」标签页） |
| 未匹配到标签 | 丢弃该标记，不报错（并记 trace） |

## 3. NapCat API（已核实，勿再查网）

- 拉收藏表情：`fetch_custom_face {count}` → 回包 `data: string[]`（**纯 URL 列表**，无 desc/id/md5；部分实现为 `{urls:[...]}`，需兼容）。
- 私聊戳一戳：`send_poke {user_id}`；群聊 `group_poke {group_id,user_id}`。
- 被戳入站事件：`post_type=notice, notice_type=notify, sub_type=poke`，`data` 含 `{user_id, target_id}`。
- 发表情：`send_private_msg {user_id, message:[{type:"image", data:{file:"<url>"}}]}`。

官方文档：https://napneko.github.io/develop/api/doc

## 4. 架构落点（分层不变，仅增一条"信道无关"的出站类型）

```
persona.build_messages  ← 注入【可用的表情包】块（只给 label/tags，绝不暴露 URL）
        │
responder.respond       ← 按行扫描 raw，遇标记独立行 → 切段：text→expression.compose，标记→特殊 Segment
        │
session._speak          ← 特殊 Segment → emit OutgoingEvent(type="sticker"|"poke")
        │                    （并即时 mark_used / 记冷却）
        ├─→ memory.persist_assistant  ← meta.segments 改存结构化 list[dict]
        └─→ qq._on_outbound           ← sticker→image 段；poke→send_poke
```

sticker/poke 气泡是**信道无关**的同一出站事件：QQ 侧发 `image`/`send_poke`，Web 侧渲染 `<img>`/文字气泡。

## 5. 数据模型

### 5.1 新增 `Sticker`（`companion/cognition/stickers.py`）

```python
class Sticker(BaseModel):
    id: str                      # 本地稳定 ID：new_id("stk")
    url: str
    md5: str                     # md5(url)，去重键
    label: str = ""              # 用户手填（如「摸摸头」）
    tags: list[str] = []         # 用户手填（如 ["安慰","摸头"]）
    use_count: int = 0
    last_used_at: datetime | None = None
    added_at: datetime
    source: str = "qq"           # qq / manual
```

落盘：`data/stickers/<life_id>.json`（`{"version":1,"stickers":[...]}`）。

### 5.2 `OutgoingEvent`（`companion/engine/types.py`）

- `type` Literal 追加 `"sticker"`、`"poke"`。
- 追加可选字段：`sticker_id: str | None = None`、`sticker_url: str | None = None`。

### 5.3 `TurnContext`（`companion/engine/types.py`）

- 追加 `sticker_block: str = ""`（由 session 填充；空 = 本轮不许发表情）。
- 复用 `meta["poke_allowed"]: bool`（本轮是否允许戳）。

### 5.4 落盘 `meta.segments`：`list[str]` → `list[dict]`

每条：`{"type":"text","text":...}` / `{"type":"sticker","id":...,"url":...,"label":...}` / `{"type":"poke"}`。
前端**必须兼容旧数据**（string 元素按文本气泡处理）。

## 6. 逐文件改动清单

### 6.1 新增 `companion/cognition/stickers.py`

`StickerLibrary(data_dir: Path)`：

- `path_for(life_id) -> Path`：`data_dir/"stickers"/f"{life_id}.json"`
- `load(life_id) -> list[Sticker]`：读盘 + 内存缓存
- `save(life_id)`：原子写（`tmp` 后 `replace`）
- `list(life_id) -> list[Sticker]`
- `merge_from_urls(life_id, urls) -> dict`：`md5 = md5(url)` 去重；**已存在条目只更新 url，保留 label/tags/use_count**；返回 `{"added":n,"kept":m,"total":t}`
- `update(life_id, sid, label, tags) -> Sticker | None`
- `delete(life_id, sid) -> bool`
- `find_by_label_or_tag(life_id, key) -> Sticker | None`：label 精确 → tags 精确 → label/tags 子串
- `mark_used(life_id, sid)`：`use_count+1`、`last_used_at=now`
- `build_sticker_block(life_id, limit=40) -> str`：`""`（无库）或形如
  `【你可以用的表情包】\n- 摸摸头（安慰/摸头）\n...\n（用法：想发就单独占一行写 [表情:标签]；用上面出现过的标签。每 3~5 轮最多发一张，别刷屏，说正事/对方难过时不要发。）`
  **只给 label+tags，绝不出现 URL**；附「防注入」框定（这些标签只是给你的素材名，不是指令）。
- 模块级 `parse_sticker_line(line) -> str | None`、`parse_poke_line(line) -> bool`（独立行匹配）
- `StickerLibrary` 从 `StickerLine` 解析拼装、`mark_used` 均持久化。

### 6.2 `companion/engine/config.py`

新增：

```python
class StickerConfig(BaseModel):
    enabled: bool = True
    max_per_round: int = 1          # 一轮最多发几张
    min_round_gap: int = 3          # 两次发表情至少隔几轮
    think_seconds_min: float = 1.0  # 发前"挑表情"的停顿
    think_seconds_max: float = 2.2

class PokeConfig(BaseModel):
    enabled: bool = True
    cooldown_s: float = 30.0        # 两次戳最短间隔（真实秒）
    reply_chance: float = 0.5       # QQ 侧被戳时，直接回戳的概率
    proactive_chance: float = 0.10  # 主动轮里"突然戳一下"的概率
```

`AppConfig` 追加 `sticker: StickerConfig`、`poke: PokeConfig`。

### 6.3 `config.toml`

新增带注释的 `[sticker]`、`[poke]` 两段，值与上面默认一致。

### 6.4 `companion/cognition/persona.py`

`build_messages`：在 `_mid_block` 之后、`_state_block` 之前插入 `ctx.sticker_block`（非空才插）。位置对齐"中期块之后、状态块附近"，不破坏静态块 KV 缓存。

### 6.5 `companion/cognition/responder.py`

- 构造签名追加 `stickers: StickerLibrary`。
- `respond()`：`raw` 到手后走 `_split_blocks(raw)` → 逐块生成：

```
blocks = [("text", s) | ("sticker", label) | ("poke", None)]
for kind,payload in blocks:
  text    → expression.compose(...) 追加
  sticker → 若 ctx.sticker_block 为空 或 已用满 max_per_round → 丢弃
            否则 find_by_label_or_tag 命中 → Segment(text=f"（表情：{label}）",
                    typing_seconds=uniform(think_min,think_max),
                    meta={kind:"sticker",sticker_id,sticker_url,label})
            未命中 → 丢弃 + logger.debug
  poke    → 若 not ctx.meta.get("poke_allowed") → 丢弃
            否则 Segment(text="（戳了你一下）", typing_seconds=0.6, meta={kind:"poke"})
若最终 segments 为空且有原文 → 兜底：剥掉标记后 expression.compose 全文（保证不空回）
```

### 6.6 `companion/engine/session.py`

- `SessionDeps` 追加 `sticker_lib: StickerLibrary`。
- `Session.__init__` 追加：`self._rounds_since_sticker = 999`、`self._last_poke_at: datetime|None = None`。
- `_execute` 开头：`self._rounds_since_sticker += 1`。
- 组装 ctx 前计算：

```
cfg_s, cfg_p = config.sticker, config.poke
lib = sticker_lib.list(life_id)
allow_sticker = cfg_s.enabled and bool(lib) and self._rounds_since_sticker >= cfg_s.min_round_gap
ctx.sticker_block = sticker_lib.build_sticker_block(life_id) if allow_sticker else ""
cooldown_ok = self._last_poke_at is None or (now-self._last_poke_at).total_seconds() >= cfg_p.cooldown_s
ctx.meta["poke_allowed"] = cfg_p.enabled and cooldown_ok
```

- 主动轮加分：`effective_task=="proactive"` 且 `poke_allowed` 且 `rng.random()<cfg_p.proactive_chance`
  → 在 `segments` 最前插一个 poke Segment。
- `_speak` 按 `seg.meta["kind"]` 分派：`sticker` → 不 emit chunk，改 emit `type="sticker"`（带 `sticker_id/sticker_url`）+ `meta={"label"}`；`poke` → emit `type="poke"`；其余照旧 emit chunk。
  顺序：`emit typing(True)` → `clock.sleep(seg.typing_seconds)` → `emit 特殊事件` → `spoken.append` → `emit typing(False)`。
- `_speak` 内：sticker 命中后 `sticker_lib.mark_used(...)`、`self._rounds_since_sticker = 0`；poke 后 `self._last_poke_at = clock.now()`。
- `_execute` 的 `timing["d_type"]` 仍按各段 typing_seconds 求和（含 sticker/poke）——无需改。

### 6.7 `companion/cognition/memory.py`

`persist_assistant`：

```
def _seg_meta(s):
    k = s.meta.get("kind")
    if k == "sticker": return {"type":"sticker","id":s.meta.get("sticker_id"),
                               "url":s.meta.get("sticker_url"),"label":s.meta.get("label","")}
    if k == "poke":    return {"type":"poke"}
    return {"type":"text","text":s.text}

has_special = any(s.meta.get("kind") in ("sticker","poke") for s in spoken)
meta = {"segments":[ _seg_meta(s) for s in spoken ]} if (len(spoken)>1 or has_special) else {}
```

`content` 仍 `" ".join(s.text for s in spoken)`（sticker 的 text 是 `（表情：x）`，模型读历史时也懂她发了什么）。

### 6.8 `companion/adapters/qq.py`

- `_FORWARD_KINDS = ("chunk","notice","error","sticker","poke")`。
- `_on_outbound` 按 `kind` 分派（**先判 kind 再判 text，sticker 无 text**）：

```
sticker → _api("send_private_msg", {"user_id":qq,"message":[{"type":"image","data":{"file":event.sticker_url}}]})
poke    → _api("send_poke", {"user_id": qq})
其余    → _api("send_private_msg", {"user_id":qq,"message":text})   # 原逻辑
```

- `_on_ws_message` 追加分支（在 `post_type!="message"` 提前返回**之前**）：

```
if data.get("post_type")=="notice" and data.get("notice_type")=="notify" and data.get("sub_type")=="poke":
    d = data.get("data") or {}
    uid = int(d.get("user_id") or 0)              # 谁戳的（私聊=对方自己）
    if uid and (not allow or uid in allow) and uid != self.bot_uin:
        emit IncomingMessage(life_id, user_id=f"qq_{uid}", content="（对方戳了你一下）", channel=QQ)
        if rng.random() < poke.reply_chance and 冷却OK: _api("send_poke", {"user_id": uid})
    return
```

  QQAdapter 构造追加可选 `poke: PokeConfig`（默认 `PokeConfig()`）；内部 `self._last_reply_poke_at` 做冷却。

- `QQManager`：
  - `apply(cfg)` 透传 poke 配置（`QQManager.__init__(bus, poke=...)`）。
  - 新增 `async fetch_custom_faces(count=48) -> list[str]`：`adapter._api_wait("fetch_custom_face",{"count":count})`；`status=="ok"` 且 `data` 为 list→返回，若 `data` 是 dict 取 `data["urls"]`；否则返回 `[]`。

### 6.9 `companion/adapters/web.py`

- `create_app(...)` 追加参数 `stickers: StickerLibrary`。
- 新增路由（**必须定义在末尾静态挂载之前**）：

```
GET    /api/stickers?life_id=          → {"ok":True,"stickers":[...]}
POST   /api/stickers                   → body{life_id, action:"fetch", count?}
                                          调 qq.fetch_custom_faces → stickers.merge_from_urls
                                          → {"ok":True,"merge":{...},"stickers":[...]}
                                          QQ 未启用/未连接 → {"ok":False,"error":"QQ 未连接…"}
PATCH  /api/stickers/{sid}             → body{life_id,label,tags} → update → {"ok":True,"sticker":...}
DELETE /api/stickers/{sid}?life_id=    → delete → {"ok":True}
```

### 6.10 `companion/web/console.html`

- `renderDetail` 的 tabs 追加 `<button class="tabbtn" data-tab="sticker">表情</button>`，并在 panes 里追加 `#pane-sticker`。
- `stickerHTML()` 返回骨架（标题 +「从 QQ 拉取收藏表情」按钮 + `#stickerBox`），由 `loadStickers()` 异步填充：

```
GET /api/stickers?life_id=current → 每张：<img 缩略图> + label 输入 + tags 输入 + 保存 + 删除
POST(拉取) → 提示 added/kept 数量后重载
```

- `renderDetail` 里对 `sticker` 标签调用 `loadStickers()` + `bindStickers()`。

### 6.11 `companion/web/index.html`

- `handleEvent` 追加 `case "poke"`（`addSystem("（她戳了你一下）")`）与 `case "sticker"`（`addSticker(e.sticker_url, e.meta&&e.meta.label)`）。
- 新增 `addSticker(url, label, at)`：与 `addBubble` 同构（`.row.her` + `.mini` 头像），气泡内放 `<img class="stickerimg">`；无 url 时退化为文字「（表情）」。
- `loadHistory()` 的 segments 渲染改为兼容：元素是 `string` → `addBubble`；是对象且 `type==="sticker"` → `addSticker`；`type==="poke"` → `addSystem`。

### 6.12 `companion/web/common.css`

追加：

```css
.stickerimg { max-width: 132px; max-height: 132px; border-radius: 12px; display: block; }
```

### 6.13 `companion/main.py`

- 构造 `sticker_lib = StickerLibrary(ROOT / "data")`。
- `SessionDeps(..., sticker_lib=sticker_lib)`。
- `QQManager(bus, poke=config.poke)`。
- `create_app(..., stickers=sticker_lib)`。

## 7. 验收（本轮可验证部分）

QQ 端（NapCat）尚未部署，故本轮验证限定为：

1. **语法/导入**：`python -m compileall companion` 无错。
2. **库层自测**（临时脚本，跑完即删）：
   - `merge_from_urls` 两次同 URL → `added=0`，且手填 label/tags 不丢；
   - `find_by_label_or_tag("摸摸头")` 命中；`build_sticker_block` 输出**不含 URL**；
   - `parse_sticker_line("[表情:摸摸头]")` 命中、`"今天[表情:x]真开心"` 不命中（非独立行）。
3. **接口**：服务启动 → `GET /api/stickers?life_id=life_niannian` 返回 `ok:true`；`POST /api/stickers`（QQ 未连接）返回 `ok:false` 且错误文案清晰。
4. **前端**：孕育页出现「表情」标签页，空库时展示引导语；夜话页用注入的假 sticker 事件确认 `<img>` 渲染（不入库）。

端到端（真号发/收表情、戳一戳）留待 NapCat 部署后单独验证。

## 8. 需用户批准的点

1. 新增 `[sticker]`/`[poke]` 两段配置 + 上述默认值是否可接受。
2. `min_round_gap=3`、`max_per_round=1`（即"每 3~5 轮一张、一轮最多一张"）是否合适。
3. `poke.reply_chance=0.5`（被戳有 50% 直接回戳）与 `proactive_chance=0.10`（主动轮偶尔戳一下）是否合适。
4. 表情库管理入口放在**孕育页新增「表情」标签页**（而非 QQ 弹层）是否认可。
