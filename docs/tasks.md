# Tasks：QQ 发表情包 + 戳一戳

> 依据：`docs/spec.md`。按顺序执行，每步完成即勾选。语言：中文（含代码注释）。

## T1 数据与配置底座
- [x] 1.1 新建 `companion/cognition/stickers.py`：`Sticker` 模型 + `StickerLibrary`（load/save/list/merge_from_urls/update/delete/find_by_label_or_tag/mark_used/build_sticker_block）+ 模块级 `parse_sticker_line` / `parse_poke_line`。
- [x] 1.2 `companion/engine/types.py`：`OutgoingEvent.type` 追加 `"sticker"`、`"poke"`；追加 `sticker_id`/`sticker_url` 可选字段；`TurnContext` 追加 `sticker_block: str = ""`。
- [x] 1.3 `companion/engine/config.py`：新增 `StickerConfig`、`PokeConfig`；`AppConfig` 注册两段。
- [x] 1.4 `config.toml`：新增 `[sticker]`、`[poke]`（含中文注释与默认值）。

## T2 生成链：提示词 → 解析 → 出站事件
- [x] 2.1 `companion/cognition/persona.py`：`build_messages` 在中期块后、状态块前插入 `ctx.sticker_block`。
- [x] 2.2 `companion/cognition/responder.py`：注入 `StickerLibrary`；新增 `_split_blocks()`；`respond()` 按块生成 text/sticker/poke 段；空结果兜底。
- [x] 2.3 `companion/engine/session.py`：`SessionDeps` 加 `sticker_lib`；计算 `sticker_block` 与 `poke_allowed`；`_speak` 分派 `sticker`/`poke` 出站事件；`mark_used` + 冷却记账；主动轮偶尔戳。
- [x] 2.4 `companion/cognition/memory.py`：`persist_assistant` 的 `meta.segments` 改结构化 `list[dict]`。

## T3 QQ 适配层
- [x] 3.1 `companion/adapters/qq.py`：`_FORWARD_KINDS` 扩容；`_on_outbound` 分派 image 段 / `send_poke`；`_on_ws_message` 处理 `notice/notify/poke`（emit IncomingMessage + 概率回戳）。
- [x] 3.2 `QQManager`：构造透传 `PokeConfig`；新增 `fetch_custom_faces(count)`。

## T4 管理面与前端
- [x] 4.1 `companion/adapters/web.py`：`create_app` 加 `stickers` 参数；新增 `/api/stickers` GET/POST/PATCH/DELETE。
- [x] 4.2 `companion/main.py`：装配 `StickerLibrary`、传入 `SessionDeps`/`create_app`/`QQManager`。
- [x] 4.3 `companion/web/console.html`：孕育页新增「表情」标签页（拉取/缩略图/补标签/保存/删除）。
- [x] 4.4 `companion/web/index.html`：`sticker`/`poke` 事件渲染；历史 `segments` 结构化兼容。
- [x] 4.5 `companion/web/common.css`：`.stickerimg` 样式。

## T5 自测与验证
- [x] 5.1 `python -m compileall companion` 语法通过。
- [x] 5.2 临时脚本验证库层（merge 幂等/保留手填、标签命中、提示词块不含 URL、标记独立行解析）；跑完删除。
- [x] 5.3 启动服务：`GET /api/stickers` 返回 `ok:true`；QQ 未连接时 `POST` 返回清晰错误。
- [x] 5.4 前端目视：孕育页「表情」标签页存在；夜话页确认 sticker 气泡 `<img>` 渲染路径可用。
- [x] 5.5 汇报：本轮可验证项结果 + NapCat 端到端验证待办。
