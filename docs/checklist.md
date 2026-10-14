# Checklist：QQ 发表情包 + 戳一戳

> 逐项核对 `docs/spec.md` 与 `docs/tasks.md` 的落地情况。全部为已核实项。

## 数据与配置底座
- [x] `companion/cognition/stickers.py` 存在，`Sticker`（id/url/md5/label/tags/use_count/last_used_at/added_at/source）+ `StickerLibrary`（path_for/load/save/list/merge_from_urls/update/delete/find_by_label_or_tag/mark_used/build_sticker_block）全部实现；落盘 `data/stickers/<life_id>.json`（`{"version":1,"stickers":[...]}`），原子写（tmp→os.replace）
- [x] `merge_from_urls` 以 `md5(url)` 去重：重复拉取 `added=0` 且保留用户手填的 label/tags/use_count
- [x] `build_sticker_block` 只给 label/tags，**输出不含任何 URL**，并附「用法 + 防注入」框定；空库返回 `""`
- [x] `parse_sticker_line` / `parse_poke_line` **仅独立行**命中（`今天[表情:x]真开心` 不命中）
- [x] `types.py`：`OutgoingEvent.type` 含 `"sticker"`/`"poke"`，新增 `sticker_id`/`sticker_url`；`TurnContext.sticker_block: str = ""`
- [x] `config.py`：`StickerConfig`/`PokeConfig` 已定义并在 `AppConfig` 注册；`config.toml` 新增带中文注释的 `[sticker]`/`[poke]`

## 生成链
- [x] `persona.build_messages`：在中期块之后、状态块之前注入非空 `ctx.sticker_block`（静态块仍在最前，不破坏 KV 缓存布局）
- [x] `responder`：按独立行标记分块生成——text→`expression.compose`；sticker 需 `sticker_block` 非空且不超 `max_per_round` 且命中素材，否则丢弃并 `logger.debug`；poke 需 `meta["poke_allowed"]`，否则丢弃；override 时两者均不发；最终为空时有兜底（剥标记后重走 compose）
- [x] `session`：`SessionDeps` 含 `sticker_lib`；每轮轮次计数配合 `min_round_gap`、戳冷却配合 `cooldown_s`、主动轮按 `proactive_chance` 随机插入 poke；`_speak` 按 `meta.kind` 分派 `emit(type="sticker"/"poke")`，并即时 `mark_used` / 记冷却
- [x] `memory.persist_assistant`：`meta.segments` 为结构化 `list[dict]`（`{"type":"text"|"sticker"|"poke"}`）；`content` 仍为文本拼接

## QQ 适配层
- [x] `qq.py` `_FORWARD_KINDS` 含 `sticker`/`poke`；`_on_outbound` **先按 kind 分派**：sticker→`send_private_msg` 的 `image` 段、poke→`send_poke`，其余走原文本逻辑
- [x] `_on_ws_message` 在 `post_type != "message"` 提前返回**之前**处理 `notice/notify/poke`：去自戳、按 `allow_users` 白名单、emit `IncomingMessage`，并按 `reply_chance`+冷却概率回戳
- [x] `QQManager(bus, poke=...)` 透传戳配置；新增 `async fetch_custom_faces(count)`，兼容 `data` 为 `list` 或 `{"urls":[...]}`，异常/超时返回 `[]`

## 管理面与前端
- [x] `web.py`：`create_app` 含 `stickers` 参数；`GET/POST/PATCH/DELETE /api/stickers` 均已定义且**位于末尾静态挂载之前**；缺 `life_id`、QQ 未连接均有清晰中文错误
- [x] `main.py`：构造 `StickerLibrary(ROOT/"data")` 并传入 `SessionDeps` / `CompanionResponder(stickers=..., sticker_cfg=config.sticker)` / `QQManager(bus, poke=config.poke)` / `create_app(stickers=...)`；服务可正常启动
- [x] `console.html`：孕育页「表情」标签页可切换；空库展示引导语；「从 QQ 拉取收藏表情」失败时展示清晰提示
- [x] `index.html`：`handleEvent` 含 `sticker`/`poke` 分支；`addSticker` 与 `addBubble` 同构并渲染 `<img class="stickerimg">`；`loadHistory` 兼容旧 `string` 与新结构化对象两种 `segments`
- [x] `common.css`：`.stickerimg { max-width:132px; max-height:132px; border-radius:12px; display:block; }`

## 验证
- [x] `compileall companion` 通过
- [x] 库层 + 生成链自测 24/24 PASS（merge 幂等/保留手填、标记边界、提示词块不含 URL、标签命中、分块生成与丢弃、落盘结构化）；临时脚本与数据已清理
- [x] 接口实测：`GET /api/stickers?life_id=life_niannian` → `ok:true`；`POST`（QQ 未连接）→ `ok:false` + 清晰错误；缺 `life_id` → 清晰错误
- [x] 前端目视：孕育页「表情」标签页与空库引导语正常、拉取失败提示正常；夜话页真实 CSS 下 sticker 气泡渲染 132×132 / 圆角 12px / her 侧、无布局溢出、无 JS 报错
- [ ] **端到端（待 NapCat 部署后单独验证）**：真实 QQ 号发/收收藏表情、戳一戳入站与回戳
