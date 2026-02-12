# astrbot_plugin_mio_nai

这是一个面向 AstrBot 的 NovelAI 轻量绘图插件，支持：

- 基础画图：`/画图 <描述>`
- 辅助画图：`/辅助画图 <自然语言描述>`
- 自然语言触发：在群里提到 `mio` 并表达“画图/生图”意图即可触发
- 自动画图：可按间隔基于群聊上下文自动出图
- 全局队列：并发固定为 1，避免接口瞬时高并发
- 429 自动重试：共享 API 被占用时自动指数退避重试

## 1. 你关心的“管理页面”

可以做到。

本插件已经改成 AstrBot WebUI 可识别的分组配置结构，导入后会出现类似你截图的配置弹窗，包含：

- 通用设置
- 请求设置
- 提示词设置
- 权限与白名单
- LLM 优化设置
- 自动画图设置

对应文件：`_conf_schema.json`

## 2. 安装方式（Git 仓库）

1. 打开 AstrBot 插件页面，选择“从 Git 仓库安装”。
2. 填入仓库地址：`https://github.com/miofling/astrbot_plugin_mio_nai`
3. 安装完成后进入插件配置页，填写必要参数并保存。
4. 重启 AstrBot（建议）。

## 3. 必填配置

至少填写：

- `nai_api_url`（默认已填：`https://ai.mio-ai.cyou/ai/generate-image`）
- `nai_api_key`

如需“辅助画图/自动画图”的自然语言优化，优先推荐在配置页选择：

- `llm_provider_id`（点击“选择提供商”，直接选 AstrBot 已接入模型）

可选兜底（仅在 provider 不可用时使用）：

- `llm_api_url`
- `llm_api_key`
- `llm_model`

## 4. 常用命令

普通用户：

- `/画图 1girl, white hair, red eyes, cat ears`
- `/辅助画图 帮我画一张夜景少女`
- `/画图状态`

管理员：

- `/画图管理 帮助`
- `/画图管理 查看`
- `/画图管理 白名单 添加 <群号>`
- `/画图管理 白名单 删除 <群号>`
- `/画图管理 自动 开|关`
- `/画图管理 模式 上下文|随机`
- `/画图管理 间隔 <最小分钟> <最大分钟>`
- `/画图管理 正面 <tags>`
- `/画图管理 负面 <tags>`
- `/画图管理 回显 开|关`
- `/画图管理 opus 开|关`
- `/画图管理 请求日志 开|关`
- `/画图管理 文案开始 <文案>`
- `/画图管理 文案排队 <文案>`
- `/画图管理 基础警告 <文案>`
- `/画图管理 llm开 | llm关`
- `/画图管理 llmprovider <provider_id>`
- `/画图管理 llmurl <url>`
- `/画图管理 llmkey <key>`
- `/画图管理 llmmodel <model>`

## 5. 说明

- `whitelist_group_ids` 为空时表示不限制群。
- 自动画图默认关闭。
- 默认模型为 `nai-diffusion-4-5-full`。
- 返回格式优先按 `application/zip` 解析，也兼容直出图片和 JSON(base64)。
- 基础画图默认开启标签格式校验；自然语言建议使用 `/辅助画图`。
- 如需查看发送给 NAI 的 JSON 请求体，可开启“请求日志”。

## 6. 仓库地址

- https://github.com/miofling/astrbot_plugin_mio_nai
