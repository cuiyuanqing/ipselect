# LuCI 配置分组美化、配置持久化与 Telegram / 飞书推送实施计划

## 概述
实现以下 4 大核心功能与改进：
1. **配置持久化保护 (防部署覆盖)**：在 OpenWrt 25 自安装脚本 (`installer.sh`) 及 IPK 打包中保护 `/etc/config/ipselect`，部署时保留用户已有配置并智能增量补齐新字段。
2. **LuCI 表单卡片分组与视觉重构**：将基础配置拆分为 5 大视觉卡片区块（常规设置、速率保底与候选补测、沙箱质检代理、推送通知、远端同步与调度），解决参数杂乱及“保底门槛/补位轮数”不易找的问题，并通过静态资源版本号解决浏览器强缓存问题。
3. **Telegram 频道 / 机器人消息推送**：支持向 Telegram 频道 (`@channel` 或 `chat_id`) 及机器人推送维护完成通知，支持自定义模板与宏变量，提供“一键测试推送”功能。
4. **飞书群机器人 Webhook 推送**：支持向飞书群机器人 Webhook 推送通知（支持可选密钥加签验证），提供“一键测试推送”功能。

---

## 涉及文件与职责分配
- `luci-app-ipselect/scripts/build_ipk.py`:
  - 改进 `OPENWRT25_INSTALLER_SCRIPT`：解包前备份 `/etc/config/ipselect`，解包后还原并增量合并新字段。
  - 改进 `build_control_tar_gz`：在 control 增加 `conffiles: /etc/config/ipselect`。
- `luci-app-ipselect/root/etc/config/ipselect`:
  - 增加 Telegram 配置项：`telegram_enabled`, `telegram_bot_token`, `telegram_chat_id`, `telegram_template`。
  - 增加飞书配置项：`feishu_enabled`, `feishu_webhook_url`, `feishu_secret`。
- `scripts/notify.py` (新增独立通用通知推送模块):
  - 提供 `send_telegram(bot_token, chat_id, text)` 与 `send_feishu(webhook_url, secret, text)`。
  - 提供 `format_notification(template, context)` 宏变量替换引擎。
  - 支持 CLI 命令行测试与任务调用。
- `luci-app-ipselect/luasrc/controller/ipselect.lua`:
  - 增加 RPC 端点 `test_telegram` 与 `test_feishu`，用于前端界面的“一键测试推送”交互。
- `luci-app-ipselect/root/usr/bin/ipselect-runner`:
  - 在任务执行完毕生成结果后，调用 `scripts/notify.py` 发送已启用的通知。
- `luci-app-ipselect/htdocs/luci-static/resources/view/ipselect/overview.js`:
  - 5 大卡片式区块重构。
  - 增加 Telegram 配置卡片与“测试 Telegram”按钮。
  - 增加飞书配置卡片与“测试飞书”按钮。
- `tests/test_notify.py` (新增单元测试):
  - 单元测试覆盖宏变量渲染、Telegram 推送格式、飞书签名与请求格式。

---

## 阶段任务清单

### Task 1: 编写通知模块 `scripts/notify.py` 与单元测试 `tests/test_notify.py`
- [ ] 编写 `tests/test_notify.py` 覆盖宏变量替换、Telegram API 请求构造、飞书签名与 payload 构造。
- [ ] 编写 `scripts/notify.py` 实现 Telegram 和飞书推送逻辑。
- [ ] 运行 pytest 确保 100% 通过。

### Task 2: 升级安装打包机制解决配置丢失问题 (`build_ipk.py`)
- [ ] 在 `build_ipk.py` 的 `OPENWRT25_INSTALLER_SCRIPT` 中添加配置备份、还原与 uci 增量填充逻辑。
- [ ] 在 control 打包中增加 conffiles 规则。
- [ ] 编写或更新单元测试验证打包产物及升级脚本逻辑。

### Task 3: 扩展 Controller RPC 端点与 Runner 自动通知联动
- [ ] 在 `ipselect.lua` 中增加 `test_telegram` 和 `test_feishu` 端点。
- [ ] 在 `ipselect-runner` 中在优选成功后调用通知脚本。
- [ ] 在 `root/etc/config/ipselect` 中添加默认选项。

### Task 4: 重构 LuCI 视图 `overview.js` 为 5 大卡片分组并增加通知配置与测试按钮
- [ ] 使用卡片式容器 (`cbi-section-node` / `card-group`) 重构各个配置项布局。
- [ ] 显式突出保底下载速率门槛与补位轮数。
- [ ] 添加 Telegram 和飞书配置项及测试按钮。
- [ ] 运行 `node test_view_syntax.js` 验证前端代码。

### Task 5: 打包构建并部署软路由 `10.10.18.2` 生产环境验证
- [ ] 构建新版本 IPK 与 installer。
- [ ] 验证现有配置在重新安装后无损保留。
- [ ] 在软路由上触发测试验证通知推送与卡片 UI 显示。
