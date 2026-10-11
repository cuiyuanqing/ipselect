# ipselect

Cloudflare 节点优选与订阅自动化维护仓库。

## 📖 核心文档
* [Cloudflare 节点优选与 WorkerVless2sub 订阅自动化运维全景实战指南](./Cloudflare_节点优选与WorkerVless2sub订阅自动化运维实战指南.md)

## 🚀 自动生成与维护资产
* **公司专属美区 Top 15 极速优选节点**：[`best_us.txt`](./best_us.txt)
  * 由公司软路由（`10.10.18.2`）通过黑龙江哈尔滨联通宽带物理出口每日自动测速推仓。
  * **订阅拉取入口**：
    ```text
    https://raw.githubusercontent.com/cuiyuanqing/ipselect/main/best_us.txt
    ```
* **家庭专属美区 Top 15 极速优选节点**：[`home_us_best_node.txt`](./home_us_best_node.txt)
  * 由家庭软路由（`192.168.0.3` / `192.168.0.2`）通过中国移动家庭宽带物理出口每日自动测速推仓。
  * **订阅拉取入口**：
    ```text
    https://raw.githubusercontent.com/cuiyuanqing/ipselect/main/home_us_best_node.txt
    ```

## 🛠 本地工具脚本
* 节点筛选脚本：[`scripts/filter_us_nodes.py`](./scripts/filter_us_nodes.py)
* 自动化工作流：[`.github/workflows/update_us_nodes.yml`](./.github/workflows/update_us_nodes.yml)

## 阶段 4 真实吞吐测速（v1.4.0+）

默认参数：5 秒 × 2 轮 / 节点内并发 3 / 节点间并发 4 / 429 重试 1 次 / 单 IP 累计下载 80MB。

| UCI 选项 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `speed_test_duration` | `SPEED_TEST_DURATION` | 5 | 单节点测速时间窗(秒)，越大越准 |
| `speed_test_parallel` | `SPEED_TEST_PARALLEL` | 3 | 节点内并发下载数 |
| `speed_test_node_concurrency` | `SPEED_TEST_NODE_CONCURRENCY` | 4 | 同时测速的节点数 |
| `speed_test_max_retry` | `SPEED_TEST_MAX_RETRY` | 1 | 429/1020 重试次数 |
| `speed_test_max_bytes_per_ip` | `SPEED_TEST_MAX_BYTES_PER_IP` | 80 | 单 IP 累计下载上限(MB) |

**总耗时估算**：30 节点 × (5s + 1s + 5s) / 4 并发 ≈ 75-90 秒。
