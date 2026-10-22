# QQ 群机器人「女仆」· 完整交接文档

> 生成日期：2026-10-21 · 交接来源：TraeCode 会话 · 接收对象：下一位维护者（人类或 AI）
> 本文档目标是：拿着它 + 仓库代码，能完全接管这台机器人。

---

## 一、这是什么

一个跑在 **GitHub Actions** 上的 QQ 官方群机器人（匿名昵称「女仆」），具备：

- 群聊 @ 回复 + 私聊（C2C）回复
- AI 人格（当前：原版猫娘人设，每句带"喵"）
- 多人身份识别（每人独立记忆 + 花名册记真名 + 心情状态 + 时段感知）
- 永久记忆（重启不丢，存 GitHub 仓库自身）
- 真人感工程（随机延迟、拆条连发、双生成挑优）
- 5.7 小时接力常驻 + 30 分钟看门狗自动复活
- 群内管理命令（/认主、/记名、/花名册、ping）

**关键认知**：腾讯只提供"工卡"（AppID/Secret + 消息通道），**不提供运行环境**。
机器人的大脑（提示词）在代码里，值班室在 GitHub Actions 上。

---

## 二、系统架构

```
群友 @女仆 发消息
   ↓
腾讯 QQ 服务器（api.sgroup.qq.com）
   ↓ WebSocket 长连接（女仆持有 AppSecret 登录的连接）
GitHub Actions runner（ubuntu, 2C7G）
   ↓ bot.py 收到 GROUP_AT_MESSAGE_CREATE 事件
   ↓ 并行调 AI ×2（deepseek-v4-flash，书生平台）
   ↓ human_score 评分挑优 → 记名/心情解析 → 拆条+延迟
   ↓ POST /v2/groups/{group_openid}/messages 回复
腾讯 → 群友看到回复（引用样式，昵称由QQ客户端渲染）

每 5.7 小时：job 结束前 curl 触发下一班（接力链）
每 30 分钟：watchdog workflow 巡检，链断自动拉起
记忆（state.json）：定期 commit 回仓库，重启加载
```

---

## 三、仓库文件清单（github.com/qq416423-eng/qq-nvpu，公开仓库）

| 文件 | 作用 |
|------|------|
| `bot.py` | 全部核心代码（单文件，~500 行，本地备份：/workspace/qqbot/bot_hf.py 同内容） |
| `.github/workflows/bot.yml` | 常驻接力工作流（单 job，timeout 355 分钟） |
| `.github/workflows/watchdog.yml` | 看门狗（cron 每 30 分钟 + 手动触发） |
| `state.json` | 永久记忆（机器人自己读写，勿手改；格式见 §5.4） |

⚠️ **仓库必须保持 public**（私有仓库 Actions 每月只有 2000 分钟，撑不过 3 天）。

---

## 四、密钥与账户（⚠️ 交接卡：本节勿推送到公开仓库）

全部配置在 GitHub Actions Secrets（Settings → Secrets and variables → Actions），已配好无需改动：

| Secret 名 | 值 | 用途 |
|-----------|-----|------|
| `QQ_APPID` | （已配置，此处不展示） | QQ 机器人 ID（q.qq.com） |
| `QQ_SECRET` | （已配置，此处不展示） | QQ 机器人密钥（重置后需同步更新此处） |
| `INTERN_KEY` | （已配置，此处不展示） | 书生平台（主力 AI） |
| `ZHIPU_KEY` | （已配置，此处不展示） | 智谱平台（兜底） |
| `GHP_TOKEN` | （已配置，此处不展示） | GitHub PAT（state.json 回写用） |

**换 Secret 教训**：在 q.qq.com 重置 Secret 后旧码立即失效，机器人会被踢下线。
必须立刻更新 Actions Secrets 并重新 dispatch（见 §8.2）。

---

## 五、核心机制详解

### 5.1 常驻接力链（5.7 小时换班）

- bot.yml：`timeout-minutes: 355`（5.92h，留余量），job 内 nohup 跑 bot.py，
  脚本轮询进程存活；**job 结束前（无论成败）curl dispatch 下一班 bot.yml**
- concurrency: `bot-runner`（cancel-in-progress: false，重复 dispatch 只排队不互杀）
- 链条断点 = GitHub runner 暴毙（job 死了 curl 没跑）→ 靠看门狗兜底
- 实际事故记录：2026-10-19 23:33（北京）runner 失联，job 无日志死亡（BlobNotFound），链条断约 2 天——这就是看门狗诞生的原因

### 5.2 看门狗（watchdog.yml）

- 每 30 分钟（cron */30）+ 手动触发
- 逻辑：查 `bot.yml` 最新 run 状态；若不是 in_progress/queued → sleep 30s → dispatch bot.yml
- 注意它只看 bot.yml 的 runs（不会把自己误判为活体）

### 5.3 WebSocket 断线恢复（bot.py 的 Bot 类）

- op10 → 有 session_id/seq 就发 **op6 RESUME**（腾讯补推断线期间的消息），
  否则 op2 IDENTIFY
- READY 事件保存 `session_id` 和 `resume_gateway_url`
- op9 = 会话失效 → 清空重新 identify
- op7 = 服务端要求重连 → close → run() 循环内 5 秒后用 resume_url 重连
- 心跳线程 30 秒一跳，顺带每小时检查 state 脏标记做持久化

### 5.4 记忆系统（state.json）

结构（base64 后存仓库根目录，走 GitHub Contents API 读写）：

```json
{
  "names":   {"openid加密串": "主人甲"},   // 每人稳定代号（天干轮转）
  "creators": ["openid加密串"],            // /认主 绑定的主人（仅一人）
  "roster":  {"openid加密串": "小红"},     // 花名册：真实称呼
  "mood":    ["开心"]                      // 当前心情（单元素数组）
}
```

- 加载：进程启动 `load_state()`（优先 GHP_TOKEN 走 API，兜底 raw 链接）
- 保存：`_state_dirty` 标记 + 每次记名/心情变化立即存 + 心跳线程每 30 分钟兜底
- 代号→UID 反查：`apply_renames`/`manual_renames` 里做
- QQ 平台拿不到昵称（隐私限制），所以"问名字→记名"是认人唯一途径；
  创造者也可用 `/记名 主人乙=小刚` 硬教

### 5.5 AI 调用链

- 主力：书生 `deepseek-v4-flash-0731`（无思考）；兜底：`deepseek-v4-pro-0813`；
  再兜底：智谱 `glm-4.7-flash` / `glm-4-flash`
- **双生成挑优**：同题并行调 2 次（threads + join 超时 22s/3s），
  `human_score()` 评分（奖励 ≤15 字、纯反应"？"嗯 6、哈×3+、含"？"；
  惩罚 ~结尾、表情包括号、AI 高频词、超 60 字、长句多标点）
- 超时 25 秒；回复文本剥离 `<think>` 标签
- temperature 1.0，max_tokens 2000

### 5.6 真人感工程

- **发送延迟**：首条 1.5~12s（按长度加随机），连发条间 2~6s
- **拆条连发**：AI 输出用 `||` 分隔 → `send_group_multi` 拆条发送（≤3 条）
- **时段人设**：`now_cn()` 注入北京时间 + 时段（凌晨困/早八上课/周末活跃）
- **心情**：AI 输出 `<<心情:xx>>` 标记 → 更新 state → 下一轮注入影响语气
- **记名**：AI 输出 `<<记名:代号=名字>>` → 正则捕获 → 写花名册 → 从回复剔除（用户不可见）

---

## 六、人设（当前版本）

SYSTEM_PROMPT（bot.py 顶部）= **easydu2002/chat_gpt_oicq wiki 的原版猫娘 prompt**，
只把名字改为"女仆"，每句带喵、身体反应、专业问题正经答。未附加其他修饰。

历史人设档案（按时间）：温柔女仆 → 傲娇毒舌（钉宫系原型锚定）→ 毒舌拉满（用户否决）
→ 真人感女友（原神/奶茶细节，穿帮风险）→ 坦然 AI（无扮演，AI 自嘲玩梗）→ **原版猫娘（现行）**。

改人设的唯一入口 = 改 bot.py 的 SYSTEM_PROMPT 后推送重启（§8.1）。

---

## 七、群内命令（硬编码在 ai_reply，不走 AI）

| 命令 | 权限 | 效果 |
|------|------|------|
| `/认主`（或"认主"、"/绑定"） | 先到先得，仅一人 | 绑定创造者（nickname 前缀变 [创造者]） |
| `/记名 主人乙=小刚 创造者=老板` | 仅创造者 | 直接写花名册（空格分隔多条，= 或 ：均可） |
| `/花名册` | 任何人 | 查看当前记名情况 |
| `ping` / `测试` | 任何人 | 回 pong（连通性测试） |

---

## 八、运维手册

### 8.1 改提示词
1. 编辑 bot.py 的 SYSTEM_PROMPT（网页：仓库 → bot.py → 铅笔图标 → 只改引号内中文）
2. Commit → 手动重启（§8.4）或等接力自动生效

### 8.2 换 QQ Secret
1. q.qq.com 重置后，新码给维护者
2. 更新 Actions Secrets → QQ_SECRET（网页粘贴即可，无需加密脚本）
3. 手动重启（§8.4）→ 看日志确认 `[ws] READY`

### 8.3 机器人不响应 → 排查顺序
1. 开 Actions 页：最新 bot run 是否 in_progress？
   - 否 → 看门狗 30 分钟内会拉起；急就手动 dispatch（§8.4）
   - 是 → 看该 run 日志：搜 `[ws] READY`（连上了吗）、`[recv-group]`（收到消息吗）、
     `[ai-err]`（AI 挂了？）、`[send-group] 200`（发出去了吗）
2. 都正常还不回 → QQ 平台限流/延迟，等 10 分钟
3. 密钥失效特征：日志里 token 相关 4xx → 走 §8.2

### 8.4 手动重启（重置链条）
Actions → bot.yml → Run workflow → Run。
或命令行：`curl -X POST -H "Authorization: Bearer $GHP_TOKEN" \
  https://api.github.com/repos/qq416423-eng/qq-nvpu/actions/workflows/bot.yml/dispatches \
  -d '{"ref":"main"}'`

### 8.5 日志注意
运行中的 run 日志只能看"原始日志"页面（API 拉已完成 run 的日志才稳定）；
in_progress 时 API 日志可能 BlobNotFound，等完成或用网页看。

---

## 九、已知风险与边界

1. **GitHub runner 暴毙**（已发生）：日志全无、接力丢失。看门狗 ≤30 分钟自愈
2. **灰色条款**：公开仓库 24/7 占用 Actions 属擦边用法，最坏被封 Actions → 迁移（§10）
3. **QQ 平台审核**：回复涉黄涉政/脏话会被拦甚至封机器人 → 人设里留了生存规则，别删
4. **QQ 主动消息限制**：机器人只能被动回复（msg_id 5 分钟内有效、每条 msg_id 有次数上限），
   没有可靠的主动推送能力
5. **QQ 拿不到昵称**：认人全靠问+记（花名册机制）
6. **接力换班间隙**：每 5.7h 有约 10-30 秒空窗，期间消息可能丢失（WS RESUME 能补大部分）

---

## 十、迁移指南（哪天要离开 GitHub）

首选 **魔搭创空间**（modelscope.cn，阿里官方、免费、合规）：
1. 网页创建创空间（Docker 类型）
2. 上传 bot.py + 写一个 Dockerfile（python:3.11-slim → pip install requests websocket-client → python bot.py）
3. 空间设置 → 环境变量 → 配 5 个密钥（同 §四）
4. state.json 持久化改用空间自带存储或继续用 GitHub API（GHP_TOKEN 保留即可）
5. 删掉 bot.yml 链（不再需要接力），watchdog 留着无害

备选：轻量云服务器（30-50元/月）→ systemd 挂 bot.py，流程同理。

---

## 十一、bot.py 代码地图（函数索引）

```
Health/HTTPServer    # 7860 端口占位（旧 HF 遗产，勿删可保平安）
get_token()          # AppID+Secret → access_token（带缓存，bots.qq.com 接口）
PROVIDERS            # AI 提供商列表（含 thinking 开关）
SYSTEM_PROMPT        # 人设（猫娘，见 §6）
load_state/save_state  # state.json ↔ GitHub 仓库
title_of/roster_line # 代号分配 / 花名册文本
handle_owner_claim   # /认主
manual_renames       # /记名
apply_renames        # 解析 <<记名>>/<<心情>> 标记
human_score          # 真人感评分器
_best_of_two         # 并行双生成挑优
now_cn               # 北京时间 + 时段人设文本
ai_reply             # 总入口：命令→记忆→注入→AI→解析→清理
send_group(_multi)   # 发群消息（msg_seq 递增；multi=拆条+延迟）
send_c2c             # 发私聊
handle_event         # 事件分发（群/私聊）
Bot 类               # WS 生命周期（identify/resume/心跳/重连）
run()                # 主循环
__main__             # load_state() → Bot().run()
```

---

## 十二、给下一任的三个忠告

1. **别把仓库设私有**，别动 concurrency 组名，别删 watchdog
2. **改人设一次只改一层**：SYSTEM_PROMPT 和运行时注入（ai_reply 里的后缀）是两回事，
   改错层会出现"提示词改了没效果"的灵异事件
3. **出问题先看 Actions 运行列表**（status/时间），九成问题在那里就现形；
   剩下一成才是代码 bug——而代码在 2026-10-21 已经过完整实测
