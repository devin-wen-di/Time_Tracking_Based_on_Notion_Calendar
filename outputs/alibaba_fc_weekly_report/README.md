# 阿里云函数计算：Notion 时间使用周报

此函数每周只读同步 Notion「Parent databases」下的数据库，汇总上一完整周的数据，生成类别时间占比图和每日分类堆叠趋势图，并通过阿里云邮件推送以邮件内嵌 PNG 发送。函数使用 Python 3.12 标准库绘制图表，不要求在你的电脑安装 Python 或图表依赖。

## 函数参数

- 运行时：Python 3.12
- Handler：`index.handler`
- 建议内存：512 MB；超时：120 秒
- 定时计划：每周一 08:00（Asia/Shanghai），统计前一个周一至周日
- 手动预览事件：`{"action":"preview","week_start":"2026-10-19"}`（week_start 必须是周一；省略则自动统计上一完整周，不发邮件）
- 手动发送事件：`{"action":"send"}`

## 需要在 FC 配置的环境变量

敏感值请放在函数计算环境变量中并标记为密钥，不要写入源码、压缩包、触发器消息或聊天。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `NOTION_API_KEY` | 是 | Notion 内部集成密钥；将 Parent databases 页面和其子数据库共享给该集成。 |
| `NOTION_PARENT_PAGE_ID` | 否 | Parent databases 页面 ID；默认沿用现有本机工具的页面 ID。 |
| `REPORT_TO` | 是 | 周报收件邮箱。 |
| `MAIL_FROM` | 是 | 已在阿里云邮件推送中验证的发件地址。 |
| `MAIL_SMTP_USER` | 是 | 邮件推送中配置的 SMTP 用户名。 |
| `MAIL_SMTP_PASSWORD` | 是 | 邮件推送 SMTP 密码。 |
| `MAIL_SMTP_HOST` | 是 | 依邮件推送地域配置的 SMTP 地址；杭州通常为 `smtpdm.aliyun.com`。 |
| `MAIL_SMTP_PORT` | 否 | 默认 465（SSL）。 |
| `REPORT_TIMEZONE` | 否 | 默认 `Asia/Shanghai`。 |
| `CATEGORY_COLORS_JSON` | 否 | 可选颜色映射，例如 `{"工作":"#d0bf2d"}`。 |

## 部署步骤

1. 在函数计算控制台创建 Python 3.12 函数，入口设为 `index.handler`。
2. 运行本目录 `package.ps1` 生成 `weekly_time_report.zip`，上传并部署。ZIP 根目录包含 `index.py` 与 `notion_calendar_export.py`。
3. 配置环境变量，并允许函数访问公网出站，以便访问 Notion API 和邮件推送 SMTP。无需启用公网 HTTP 触发器。
4. 先运行预览事件，检查周范围、记录数和汇总时长；再配置收件地址和 SMTP 密钥，手动触发一次真实发送，确认两张图显示正常。
5. 添加定时触发器。Cron 填写 `CRON_TZ=Asia/Shanghai 0 0 8 * * MON`，触发消息填写 `weekly-report`。

## 计算规则

- 每次运行都会递归发现 Parent databases 下的子数据库，并按 Notion 分页读取所有历史记录；API 查询不按周裁剪。周报图表与汇总仍只统计上一完整周。预览结果分别返回 `source_records`（读入总数）和 `week_records`（周报窗口记录数）。
- 每个 Notion 子数据库作为一个类别；零时长类别仍显示为 0 小时、0%。
- 优先使用 Time allotted；缺少时用有效开始与结束时间补算。差异超过一分钟会提示。
- 有时长但日期缺失的记录按现有仪表盘规则计入类别汇总，不进入每日趋势；缺少有效时长的记录不计入小时汇总并提示。
- 时间重叠会提示，但保留原始时长，不自动扣减。
- 未记录时长按一周内没有活动覆盖的时间估算，并加入占比图和趋势图。
- 周范围为周一至周日；小时保留两位小数，占比保留一位小数。

## 安全和费用

此部署包不含 Notion 密钥或邮件密码。个人活动数据会进入周报邮件，请只发送到你指定的邮箱。实际 FC、日志、网络和邮件推送费用取决于阿里云账号的剩余额度及发件配置；上线前需在控制台核对。

本目录提供部署代码和操作说明，不会代替你创建阿里云资源、录入密钥或向真实邮箱发送邮件。
