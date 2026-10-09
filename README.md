# ipselect

Cloudflare 节点优选与订阅自动化维护仓库。

## 📖 核心文档
* [Cloudflare 节点优选与 WorkerVless2sub 订阅自动化运维全景实战指南](./Cloudflare_节点优选与WorkerVless2sub订阅自动化运维实战指南.md)

## 🚀 自动生成与维护资产
* **美区 Top 15 极速优选节点**：[`best_us.txt`](./best_us.txt)
  * 由 GitHub Actions 每日凌晨 04:00 自动定时更新，筛选自最新测速结果，仅保留前 15 个最快美国 TLS 节点。
* **Cloudflare Worker 订阅拉取入口**：
  ```text
  https://raw.githubusercontent.com/cuiyuanqing/ipselect/main/best_us.txt
  ```

## 🛠 本地工具脚本
* 节点筛选脚本：[`scripts/filter_us_nodes.py`](./scripts/filter_us_nodes.py)
* 自动化工作流：[`.github/workflows/update_us_nodes.yml`](./.github/workflows/update_us_nodes.yml)
