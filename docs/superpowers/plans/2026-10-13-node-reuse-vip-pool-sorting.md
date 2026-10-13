# 往期节点复用、高价值 VIP 留存池与输出排序优化实施计划

## 概述
实现以下 4 大核心机制：
1. **往期优选结果复用 (Warm Pool & Fast Track)**：优选启动时读取上期生成的 `best_us.txt` 与 `best_us_detail.json`，将历史胜出的优质节点作为高优先级种子注入待测候选池，防止随机采样导致优质节点丢失。
2. **≥ 8.0 MB/s 高价值 VIP 节点专库留存 (VIP Reserve Pool)**：维护持久化 `vip_nodes.json` 极速节点库，自动沉淀测速 ≥ 8.0 MB/s 的节点；新一轮优选时优先复核录用，实现优质节点持续稳定在位。
3. **按需补位联动 (On-Demand Deficit Refill)**：优先复核录用 VIP 与历史节点，当达标节点不足目标配额时，才从外部大池按需分批增量补测，兼顾节点供给稳定与运行效率。
4. **节点输出排序重构 (Region-Grouped Priority + Speed Descending)**：阶段 5 导出时，彻底打破分批测试的轮次边界，实行“核心区域优先分组（US -> HK -> TW -> SG -> JP -> KR -> ...） + 组内速率绝对降序”的统一重排，消除后批次补测节点被甩到末尾的问题。
5. **LuCI 界面与配置联动**：在卡片 2 中加入“往期结果复用开关”与“高价值留存门槛 (MB/s)”设置项，与执行引擎无缝联动。

---

## 涉及文件与职责分配
- `scripts/filter_us_nodes.py`:
  - 增加历史节点与 VIP 库加载模块 `load_warm_and_vip_seeds(workdir, output_file, vip_file)`。
  - 增加 VIP 库持久化更新函数 `update_vip_nodes_pool(vip_file, tested_nodes, threshold)`。
  - 重构最终排序与导出逻辑 `sort_qualified_nodes_by_region_and_speed(nodes, region_order)`。
  - 在 `run_filter_and_export` 中接入上述流水线，并支持命令行参数 `--reuse-history` 与 `--vip-speed`。
- `luci-app-ipselect/root/etc/config/ipselect`:
  - 增加 `option reuse_history '1'` 与 `option vip_speed_threshold '8.0'`。
- `luci-app-ipselect/root/usr/bin/ipselect-runner`:
  - 读取并透传 `--reuse-history` 与 `--vip-speed` 至 `filter_us_nodes.py`。
- `luci-app-ipselect/htdocs/luci-static/resources/view/ipselect/overview.js`:
  - 在“卡片 2：速率保底门槛与流水线补位”中添加 `reuse_history` 与 `vip_speed_threshold` 选项。
- `tests/test_vip_pool_and_sorting.py` (新增单元测试):
  - 覆盖历史节点回流、VIP 库增删淘汰、区域分组与跨轮次速度严格降序。

---

## 任务拆解与执行步骤

### Task 1: 编写单元测试 `tests/test_vip_pool_and_sorting.py`
- [ ] 编写测试用例覆盖区域优先级排序（验证后补的美区节点无论批次如何，均排在最前且速度由高到低）。
- [ ] 编写测试用例覆盖 VIP 库加载、沉淀（>= 8.0 MB/s）与低速淘汰。
- [ ] 编写测试用例覆盖历史优质节点（best_us_detail.json / best_us.txt）提取与种子注入。

### Task 2: 在 `scripts/filter_us_nodes.py` 实现历史回流、VIP 留存与区域速度降序
- [ ] 实现 `sort_qualified_nodes_by_region_and_speed` 函数。
- [ ] 实现 `load_warm_and_vip_seeds` 与 `update_vip_nodes_pool` 函数。
- [ ] 在 `run_filter_and_export` 的阶段 1 前注入种子，阶段 5 导出前执行全局重排与 VIP 库同步。
- [ ] 添加 `--reuse-history` 与 `--vip-speed` CLI 参数。
- [ ] 运行 pytest 确保 Task 1 测试通过。

### Task 3: LuCI 配置与 Runner 参数透传联动
- [ ] 在 `/etc/config/ipselect` 增加默认配置。
- [ ] 在 `ipselect-runner` 导出并透传命令行参数。
- [ ] 在 `overview.js` 卡片 2 表单增加对应控件。
- [ ] 运行 `node test_view_syntax.js` 验证前端代码。

### Task 4: 全量测试、版本升级、打包与软路由实测部署
- [ ] 运行全量 pytest 确保 100% 通过。
- [ ] 更新 Makefile 版本号至 1.7.0 并运行 `build_ipk.py` 构建新包。
- [ ] Git commit & push 远端。
- [ ] 在软路由 `10.10.18.2` 执行 `git pull` 与一键安装更新。
- [ ] 触发一次优选任务，验证 VIP 留存、区域聚合排序与美区节点置顶效果。
