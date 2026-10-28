# 参考与实现取舍

核对日期：2026-10-28。代码以原群友项目的消息队列、发送核验、上下文和回复结构为基础；在独立目录中改为用户可配置的本机程序。

| 参考 | 实际采用的思路 | 本版范围 |
| --- | --- | --- |
| [MaiBot](https://github.com/Mai-with-u/MaiBot) | 将人设、表达风格、参与聊天方式分开配置；提供可安装入口 | 自行编写配置界面与提示词，不打包其代码或服务 |
| [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) | 微信 4.x Windows 本地客户端适配，以及 UIA 可用性必须实测 | 延续原项目的可访问控件核验；本版自行实现最小窗口/点击封装，不打包上游库，不调用其数据库读取、密钥扫描或内存修改功能 |
| [OpenAI Codex](https://github.com/openai/codex) | 使用官方 CLI 及结构化 JSONL 完成事件 | 只在 `turn.completed` 后接受完整回复；工具关闭、只读、忽略用户规则与配置，只生成聊天结果 |

上游 wechatauto-replica README 当前标记 1.2.8.1 并说明维护时间有限，其 4.x 支持声明不等于本版兼容所有微信版本。本版采用此前项目实测的 UIA 接收路径，便于在无需数据库密钥的条件下安装；代价是需要完整昵称名单，且高速聊天、同名成员、客户端升级时存在适配限制。

官方依据：[登录与鉴权](https://learn.chatgpt.com/docs/auth)说明 CLI 可通过 `codex login` 使用自己的 ChatGPT 账号，也支持 API Key；两种登录方式的额度/计费不同。[非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)说明 `codex exec --json` 的事件流和 `--output-schema`。

本版的 GPT 登录流程由官方 CLI 承担，不实现网页版 Cookie 复用或自建 OAuth 回调。运行时的图像/UI 依赖通过 pip 单独安装；项目许可证以各依赖发行版的 metadata 和许可证为准。参考仓库的源码没有复制进发布包。
