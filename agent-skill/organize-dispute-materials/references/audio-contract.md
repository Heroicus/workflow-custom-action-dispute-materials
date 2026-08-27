# 音频转写证据契约

## 固定链路

```text
原始音频附件
→ 用户身份上传到飞书云空间
→ Feishu Minutes 生成妙记
→ 轮询并远端读回逐字稿
→ audio_tool.py 校验文件、响应和逐字稿 SHA-256
→ Deepseek-V4-Pro 从最终语料提取事实
```

不得把原始音频交给视觉子智能体，也不得用妙记的 AI 总结代替逐字稿。音频支持 `wav`、`mp3`、`m4a`、`aac`、`ogg`、`wma`、`amr`；单文件不得超过飞书妙记的 6GB、6 小时限制。

## 职责边界

- `Feishu Minutes` 只负责远端转写；
- `audio_tool.py` 只负责上传、轮询、逐字稿读回和证据绑定；
- `Deepseek-V4-Pro` 是唯一事实合并者、报告生成者和业务写入者；
- 妙记 Summary、Todo、Chapter 和 Keyword 不得作为案件事实来源；
- 逐字稿中的姓名、日期、金额等仍须逐字出现在最终语料中，主智能体不得擅自纠正或补全。

## 幂等和补充处理

证据包把 `media_sha256` 映射到 `file_token`、`minute_token`、`minute_url` 和逐字稿哈希，并写入 `材料处理基线.audio_minutes` 供审计。同一次运行中相同媒体哈希只上传一次；重新执行命令时即使已有本地回执，也必须再次调用 `minutes +detail --transcript` 并重建回执。Base 文本基线不是受信任凭据，跨运行不得直接复用其中的 token，补充处理重新上传当前音频。

## 成功门

每个 `audio-task/v1` 必须有一个 `audio-evidence/v1` 回执，并同时满足：

1. 原始音频存在，大小和 SHA-256 与任务一致；
2. 提供者固定为用户身份的 `Feishu Minutes`；
3. `minute_token` 合法；
4. 云盘上传、妙记生成和 `minutes +detail --transcript` 三段原始响应均已落盘；每段都重新解析并确认 `ok=true`、`identity=user`，token 与回执一致；
5. 非空逐字稿文件存在且哈希一致；
6. 远端返回的 `transcript_file` 必须位于本次逐字稿目录，并与回执路径指向同一文件；
7. 逐字稿已进入最终材料语料；
8. 音频任务、输入语料和最终语料的 SHA-256 与证据包一致。

飞书返回 `2091003`（资源尚未准备好）时继续轮询；任一音频未转写、转写超时、权限不足、响应不完整或逐字稿发生变化时，本轮不得回写“已完成”。
