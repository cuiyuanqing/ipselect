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
