# astrbot_plugin_mio_nai

A lightweight NovelAI drawing plugin for AstrBot.

Features:
- Basic draw command: `/画图 <description>`
- Assist draw command: `/辅助画图 <description>`
- Natural language trigger: mention `mio` + drawing intent
- Auto draw scheduler (context mode / random mode scaffold)
- Global queue (concurrency = 1)

## Install

1. Put this folder into AstrBot plugin path, e.g. `data/plugins/astrbot_plugin_mio_nai`.
2. Restart AstrBot.
3. Configure required fields in plugin config.

## Required config

- `nai_api_url` (default: `https://ai.mio-ai.cyou/ai/generate-image`)
- `nai_api_key`

Optional but recommended:
- `llm_api_url`
- `llm_api_key`
- `llm_model`
- `whitelist_group_ids`

## Commands

User commands:
- `/画图 二次元猫娘，海边沙滩椅`
- `/辅助画图 mio 帮我画一个夜景少女`
- `/画图状态`

Admin commands:
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
- `/画图管理 llm开 | llm关`
- `/画图管理 llmurl <url>`
- `/画图管理 llmkey <key>`
- `/画图管理 llmmodel <model>`

## Notes

- If `whitelist_group_ids` is empty, all groups are allowed.
- Auto draw is disabled by default.
- Default model: `nai-diffusion-4-5-full`.
- Response parsing supports zip/image/json(base64).

## Repo

- https://github.com/miofling/astrbot_plugin_mio_nai
