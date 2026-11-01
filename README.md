# strata-wechat-bridge — 把微信接到本机 agent(跨平台:Windows / Linux)

手机微信发一句话 → 本机跑一个 dsh(headless)会话把它执行 → 结果回到微信。
**不用建应用、不用 App Secret、不用公网地址**:一条扫码就完成绑定。

协议是照着 `@tencent-weixin/openclaw-weixin` 2.4.10 的源码移植的(iLink bot HTTP/JSON + 长轮询 + QR 登录
+ CDN 媒体 AES-128-ECB/PKCS7),全部逻辑在本包内,没有写死某台机器的路径。

## 需要装什么

1. **Python 3.10+**(`qrcode[pil]`、`pycryptodome`)。
2. **本机有 Strata / deepseek-harness(dsh)**:桥靠 `node <harness>/lib/bin.js --profile headless --json "<任务>"`
   执行指令,模型走本机服务(`http://127.0.0.1:18500/v1`)。没有 dsh 的机器跑不了执行那一半(可以只当协议 demo 跑测试)。
   路径靠环境变量或 `wechat.config.json` 指定(见下)。

## 装与跑

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1   # 建 .venv、装依赖、跑离线测试、打状态
powershell -ExecutionPolicy Bypass -File run.ps1     # 没登录就出扫码链接并立刻常驻;已登录直接常驻
```

未签名脚本会被执行策略挡掉,所以加 `-ExecutionPolicy Bypass`。

Linux / macOS:

```bash
bash setup.sh
bash run.sh
```

**依赖是可选的**:核心登录 + 纯文字对话只用标准库 urllib;`qrcode[pil]` 只用于生成二维码 PNG(没装就只打印链接),
`pycryptodome` 只用于图片/文件附件的 AES(没装就明确报错)。所以 pip 装不上时(受限沙箱里下载索引可能卡住),
在普通终端装依赖即可,或者先跑零依赖的对话链路 —— `python tests/test_deps_optional.py` 会验证这条降级路径。

装不上依赖时的离线步骤(不需要 pip,也能跑起来):

```bash
python3 -m venv .venv
.venv/bin/python -m py_compile *.py        # Windows: .venv\Scripts\python.exe
.venv/bin/python run_tests.py
.venv/bin/python bridge.py --status        # token:none 就接着 bridge.py --login --and-serve
```

`setup.ps1 -NoDeps` / `bash setup.sh --no-deps` 就是跳过 pip 那一步。

**换机器第一步**:`python doctor.py`(或 `python bridge.py --doctor`)逐项检查这台机器能不能跑 ——
node、harness `lib/bin.js`、`dsh_home`、可写临时目录、profile patch、密钥目录、本机模型服务、
可选依赖、是否已绑定,缺的会告诉你该设哪个环境变量。

扫码:登录会打印**二维码链接**(手机浏览器打开,再用微信扫或确认授权),同时写 `weixin-qr.png`。
服务端二维码约 **2 分钟**过期,`--login` 会自动换下一张(最多 5 张)。绑定后凭证进 `weixin-account.json`,
把打印出来的 `ilink_user_id` 填进 `wechat.config.json` 的 `allow_from`(只认你一个人)。

只想拿一张新链接:`python bridge.py --qr-link`。登录+立刻收消息:`python bridge.py --login --and-serve`。

## 两个档

- **对话档(默认)**:`patches/chat.patch.yml` —— 人设换成"这台机器上的聊天助手"(先给结论再补理由,三四句到六七句,
  不要单字敷衍),关掉写/跑命令类工具(bash/pwsh/subagent/workflow/todo/goal/skill/jobs/agent-instructions/plan-mode),
  保留只读的 `tool-fs`/`tool-fs-search`(手机上问"工作目录里有什么文件"它能看一眼再答),权限固定 `read-only`。
- **编程档**:`patches/coding.patch.yml` —— `workspace-write`,能用工具写文件跑命令,这次真正写成的文件会自动回传(最多 2 个)。

手机上切换:`/agent chat` · `/agent code`(写回配置文件,重启保持)。

## 手机上的指令

`/status`(档、会话、模型、路径) · `/whoami` · `/session` · `/session current` · `/transcript` ·
`/relay on|off` · `/new` · `/abort` · `/mode <read-only|workspace-write|danger-full-access>` · `/file <路径>` ·
`/confirm` · `/cancel` · `/help`(别名:`/rely`→`/relay`,`/history`→`/transcript`,`/stop`→`/abort`,`/reset`→`/new`)

## 配置与环境变量(跨平台的关键)

`wechat.config.json` 里字段:`allow_from`、`workspace`、`chat_mode`、`chat_context`、`relay`、`shared_session`、
`session_id`、`send_deliverables`、`confirm_first`、`allow_mode_switch`、`inbound_dir`、`log_file`、`agent.*`。

没写死的默认值都来自环境变量(见 `paths.py`):

| 环境变量 | 作用 | 没设时的默认 |
|---|---|---|
| `DSH_HOME` | dsh home(profile 缓存、会话) | `~/.strata/data/agent/home` |
| `DSH_PATCH_DIR` | host profile patch 目录 | `DSH_HOME` 的上一级 |
| `DSH_KEY_DIR` | 本机模型 API key 目录 | `~/.strata/data/native-instances` |
| `DSH_HARNESS_BIN` | `lib/bin.js` 路径 | Windows 试 `C:\Strata\harness\lib\bin.js`,再试 `~/.strata/harness/lib/bin.js` |
| `DSH_NODE` | node 可执行文件 | Windows 试 `C:\Strata\node\node.exe`,否则 PATH 里的 `node` |
| `DSH_SERVE_URL` | 本机模型接口 | `http://127.0.0.1:18500/v1/models` |
| `DSH_KEY_ENV` | 密钥的环境变量名 | `STRATA_LOCAL_KEY_0` |
| `WECHAT_WORKSPACE` / `STRATA_WORKSPACE` | 桥的工作目录 | 当前目录 |
| `WECHAT_TMP_DIR` | 给 harness 的临时目录 | 不设就用系统 temp |

`python bridge.py --status` 会把这份路径快照打出来,换机器时先看它。

**沙箱/受限环境的坑**:harness 要重写 profile 缓存、`spill-local` 要在 `%TEMP%` `mkdtemp`。如果这两处不可写,
把 `DSH_HOME` 指到一个可写目录(比如包内的 `.dsh-home/`),并设 `WECHAT_TMP_DIR`。注意 `DSH_HOME` 换了以后,
本机模型的密钥也要跟着有着落 —— 要么那个 home 里有 `.credentials.yaml`,要么直接在
`wechat.config.json` 的 `agent.api_key` 里写密钥(实测:换 home 后报 `MISSING_CREDENTIAL: llm-deepseek`)。
`temp_dir()` 会挨个试 `WECHAT_TMP_DIR`、系统 temp、`/tmp`、包内 `.tmp`、当前目录,用第一个**真的能写**的。

## 测试(不需要凭据、不联网发微信)

```bash
python run_tests.py
```

覆盖:iLink 协议头/端点形状、扫码登录状态机、长轮询与去重、凭证失效(`-4001`/`-14`)自动重新扫码、
附件下载解密落盘、出站上传(AES 密文可解回原文)+ `/file`、markdown 过滤与 `ref_msg` 引用、
档位切换与对话档拒 `/mode`、交付物判定(只发真正写成的文件)、relay 与 `send.py`(桌面侧回微信)。

## 已知限制(实测)

- 登录发出的 token 是**短期会话**:绑定后必须立刻开始长轮询(`--login --and-serve`),隔几分钟再用会回
  `errcode=-14 "session timeout"`;`auto_relogin`(默认开)遇到 `-14`/`-4001` 会自动换码重绑。
- headless 一次性执行**不能接管**桌面 profile 建的会话(带 agent preset,`dsh-headless` 的守卫),所以
  微信走它自己的持久会话;对话档还会把最近对话垫进指令,换会话也不丢上下文。
- 微信没有消息编辑接口,长回复按段落切块(每块 ≤1200 字,带 `(1/2)` 前缀)。
- 语音(silk)转码、引用缓存这些插件功能没移植;语音消息若服务端带转文字则用文字。
