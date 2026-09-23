# 群像谱 · 微信聊天记录分析

读取本机微信 4.x 的群聊/私聊记录，选一个群或一个人，自动生成「群像谱」报告（人物关系网 + 画像 + 对抗主轴 + 指令矩阵 + 发言名册 + 时间线）。**纯本地统计 + 规则，不调用大模型、不上传数据。**

## 快速开始

1. 确认微信（PC 4.x）已登录且在运行。
2. 双击 `run.bat`，或执行：

   ```
   python -m uvicorn wxinsight.web.app:app --host 127.0.0.1 --port 8800
   ```

3. 浏览器打开 http://127.0.0.1:8800 ，选一个群/好友，点它即可出报告。

## 原理

- 微信 4.x 本地库是 SQLCipher4 加密（`D:\chat\wx\xwechat_files\<wxid>\db_storage\*.db`）。
- 密钥被 XOR 掩码加密存在进程内存的 `com.Tencent.WCDB.Config.Cipher` 对象里，程序从运行中的 `Weixin.exe` 内存里提取（见 `wxinsight/wechat/extract_keys.py`）。
- 每个库一把裸 AES-256 密钥，按 16 字节 salt 区分，缓存在 `data/all_keys.json`。
- 解密后用 SQLite 读 `Msg_<md5(talker)>`、`SessionTable`、`contact`、`chat_room`，统计 @/引用回复构建关系网，规则拼装画像。

## 目录结构

```
wxinsight/
  wechat/     定位、内存读取、密钥提取、解密、表结构
  analysis/   统计 / 关系网 / 画像 / 时间线 / 渲染
  web/        FastAPI 后端 + 前端
data/         密钥缓存 + 解密缓存
outputs/      生成的报告 HTML
```

## 说明

- 首次分析会解密消息库（`message_0.db` 约 470MB，解密仅需几秒），结果缓存在 `data/decrypted/`。
- 密钥只需提取一次；之后重启微信（进程变了）需重新提取（自动）。
- 图片/语音的实际内容无法读取（微信鉴权），报告中会标注。
- 仅供查看自己账号的本地数据，请勿用于侵犯他人隐私。
