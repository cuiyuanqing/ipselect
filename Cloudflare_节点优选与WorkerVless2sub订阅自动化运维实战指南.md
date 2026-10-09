# Cloudflare 节点优选与 WorkerVless2sub 订阅自动化运维全景实战指南

> **作者 / 维护者**：JeffSmith  
> **核心组件**：Cloudflare Workers / Pages、WorkerVless2sub、edgetunnel、GitHub Actions、CloudflareSpeedTest  
> **文档定位**：生产级边缘隧道节点优选、订阅自动化同步与全自动精简运维避坑指南。

---

## 目录
1. [系统拓扑与组件协作机理](#一系统拓扑与组件协作机理)
2. [经典踩坑与故障深度排障实录](#二经典踩坑与故障深度排障实录)
   - 2.1 访问 `/auto` 快速订阅呈现空白页（白屏）
   - 2.2 edgetunnel 关联自建优选生成器“完全没效果”
   - 2.3 节点数量爆炸与几百个 `NRT/FRA/AMS/LAX` 杂乱机场码
   - 2.4 节点名称残留 `订阅器内置节点 UUID 未设置！！！` 警告后缀
3. [测速机制的本质真相（静态测速表 vs 实时测试）](#三测速机制的本质真相)
4. [GitHub Actions 自动化运维架构演进](#四github-actions-自动化运维架构演进)
   - 4.1 为什么完全不需要调用 Cloudflare API 重新部署？
   - 4.2 每日定时筛选脚本设计（Top 15 美区极速节点）
   - 4.3 GitHub Actions 工作流自动化配置
   - 4.4 Cloudflare Worker 极简环境变量配置
5. [日常运维与即时验证速查命令 (Cheat Sheet)](#五日常运维与即时验证速查命令)

---

## 一、 系统拓扑与组件协作机理

在基于 Cloudflare 边缘计算的网络加速架构中，**edgetunnel** 与 **WorkerVless2sub** 属于上下游协同关系：

```mermaid
flowchart TD
    Client["客户端 (Clash Verge / OpenClash / v2rayN)"]
    EdgeTunnel["主网关 edgetunnel (proxy.19940407.xyz)"]
    VlessSub["优选生成器 WorkerVless2sub (sub.19940407.xyz)"]
    GHA["GitHub Actions 定时任务 (ipselect)"]
    RawNodes["静态优选源 best_us.txt (GitHub Raw)"]

    Client -->|1. 请求主订阅 /sub?token=...| EdgeTunnel
    EdgeTunnel -->|2. 探测拉取优选 IP /sub?host=example.com&uuid=...| VlessSub
    VlessSub -->|3. 动态读取 ADDAPI| RawNodes
    GHA -->|每日凌晨定时测速筛选并推仓| RawNodes
    VlessSub -->>|4. 返回携带优选 IP 的节点流| EdgeTunnel
    EdgeTunnel -->>|5. 聚合生成专属优选节点列表| Client
```

### 核心分工与数据交互契约
1. **edgetunnel（主隧道网关）**：
   * 负责流量解密、WebSocket 协议转发与策略组下发；
   * 自身不具备大规模并发测速能力，依赖外部优选生成器提供高质量 CDN 节点。
2. **WorkerVless2sub（优选订阅生成器）**：
   * 纯粹的“优选 IP 注入器”，负责收集并维护优质 Anycast / 反代 IP 池；
   * 当 edgetunnel 向其发送固定请求：
     ```url
     /sub?host=example.com&uuid=00000000-0000-4000-8000-000000000000
     ```
     生成器将优选 IP 结合 `example.com` 组装返回，edgetunnel 提取其中的 `IP:端口#备注` 并替换为自身真实域名与 UUID。

---

## 二、 经典踩坑与故障深度排障实录

### 1. 访问 `/auto` 快速订阅呈现空白页（白屏）
* **故障现象**：在浏览器或客户端访问 `https://<生成器域名>/auto`，返回 HTTP 200，但页面一片死白，无任何内容（Body 为 0 字节）。
* **底层根因**：
  * 在 `WorkerVless2sub` 源码中，优选数组默认初始值全为空：
    ```javascript
    let addresses = [];
    let addressesapi = [];
    let addressescsv = [];
    ```
  * 仓库同目录下的 `addressesapi.txt` 与 `addressescsv.csv` 只是源码静态文件，**Cloudflare Workers 运行时仅执行 `_worker.js`，绝不会自动把同目录下的 txt/csv 加载进内存**。
  * 未配置 `ADD` / `ADDAPI` 环境变量时，生成的节点数组长度为 0，经 Base64 编码后 `btoa("")` 仍为空字符串，返回 `Content-Length: 0`，浏览器解析 0 字节文本自然显示为完全白屏。
* **排障与解决**：必须通过环境变量 `ADD`（静态列表）或 `ADDAPI`（在线 txt URL）为 Worker 注入数据源。

---

### 2. edgetunnel 关联自建优选生成器“完全没效果”
* **故障现象**：在 edgetunnel 后台添加了自建的 `sub.19940407.xyz`，保存刷新后，主订阅节点列表没有任何变化。
* **底层根因**：
  * edgetunnel 拉取时调用：
    ```bash
    curl -s "https://sub.19940407.xyz/sub?host=example.com&uuid=00000000-0000-4000-8000-000000000000"
    ```
  * 由于生成器未配置有效数据源，直接向 edgetunnel 返回了 0 字节空文本；
  * edgetunnel 正则匹配出 0 个优选 IP，无法生成任何新节点，只能降级使用默认节点或备用生成器，因此表现为“完全没效果”。

---

### 3. 节点数量爆炸与几百个 `NRT/FRA/AMS/LAX` 杂乱机场码
* **故障现象**：在环境变量中配置了官方示例的 `ADDCSV` 与 `DLS: 5` 后，订阅节点瞬间暴增至近 700 个，节点全是以 `NRT`、`FRA`、`AMS`、`LAX` 等机场代号命名的杂乱节点。
* **底层根因**：
  * `ADDCSV` 引入的测速文件 `addressescsv.csv` 包含全球 2200 多行测速记录；
  * 源码中未做数量上限截断（Top N），仅做了 `speed > DLS` 的阈值判断；
  * 源码第 185 行直接使用测速表中的数据中心机场代号作为节点备注名：
    ```javascript
    const dataCenter = row[tlsIndex + remarkIndex];
    const formattedAddress = `${ipAddress}:${port}#${dataCenter}`;
    ```
  * 导致大量欧洲（FRA、AMS）、亚洲（NRT、HKG）等非目标地区的节点被无差别倾倒进订阅中。

---

### 4. 节点名称残留 `订阅器内置节点 UUID 未设置！！！` 警告后缀
* **故障现象**：每个节点名称末尾都被强行拼上一长串感叹号中文警告。
* **底层根因**：
  * 在环境变量中配置了 `"HOST": "..."`，但遗漏了 `"UUID"` 配置；
  * 源码判定 `host != "null" && uuid == "null"`，触发全局变量追加：
    ```javascript
    EndPS += ` 订阅器内置节点 UUID 未设置！！！`;
    ```
  * `EndPS` 作为顶层全局变量污染了每一个生成的节点名称。
* **修复**：在环境变量中必须配对填写 `UUID` 与 `HOST`，或两者均不填。

---

## 三、 测速机制的本质真相

> **核心结论**：速度测试**绝不是** Worker 在云端实时跑出来的，而是**读取外部已经测好速度的静态数据**。

* **为什么云端不能实时测速？**  
  Cloudflare Worker 是无服务器边缘计算（Serverless），每次请求仅分配数十毫秒至数秒的 CPU 执行时限。若要在云端对成百上千个 IP 逐一跑并发下载测速，需要耗费数十分钟、几十 GB 流量，Worker 会瞬间被平台强制超时截断。
* **正确测速生产流程**：
  1. 在本地电脑或专属服务器上运行 `CloudflareSpeedTest`；
  2. 程序向 Cloudflare 各 Anycast IP 段并发握手并发起测速，测出真实 TCP 延迟与实际下载速度（MB/s）；
  3. 测速工具将结果导出为 CSV 或 TXT 表格；
  4. Worker 仅仅是在用户请求订阅时，**按预设规则过滤并读取表格中已有的测速数值**。

---

## 四、 GitHub Actions 自动化运维架构演进

### 1. 为什么完全不需要调用 Cloudflare API 重新部署？
`WorkerVless2sub` 原生支持 **`ADDAPI` 在线拉取**：
* 每次客户端拉取订阅，Worker 都会通过 `fetch()` 实时向 `ADDAPI` 填写的 URL 获取最新内容；
* **只要 `ADDAPI` 指向的在线文本（如 GitHub Raw 链接）发生变化，订阅节点将在下次请求时秒级无感更新**；
* 无需触碰 Cloudflare 控制台，无需重新部署 Worker，更不需要调用复杂的 Cloudflare API！

---

### 2. 每日定时筛选脚本设计（Top 15 美区极速节点）
在资源仓库 `cuiyuanqing/ipselect` 中编写自动化脚本 `scripts/filter_us_nodes.py`：
* 自动拉取最新的全量测速数据源；
* 严格剔除非美国机房（排除加拿大、欧洲、亚太节点），仅保留 `SJC`、`LAX`、`SEA`、`ORD`、`IAD` 等美西/美东核心机房；
* 仅保留 `TLS == TRUE` 的高安全性 443 端口节点；
* 严格按下载速度倒序排列，精准截取 **最快的前 15 个美国节点**；
* 自动规范化命名为：
  ```text
  138.2.151.106:443#US-DaTree-01 10.67MB/s
  129.146.247.85:2053#US-DaTree-02 7.88MB/s
  ...
  192.9.138.241:443#US-DaTree-15 0.34MB/s
  ```

---

### 3. GitHub Actions 工作流自动化配置
在 `.github/workflows/update_us_nodes.yml` 中配置全自动守护流程：
* **定时运行**：每天北京时间凌晨 **04:00**（UTC 20:00）自动执行；
* **手动调试**：支持在 GitHub 仓库 Actions 界面随时点击 **Run workflow** 手动触发；
* **免密推仓**：使用 GitHub 原生提供的安全凭据 `${{ secrets.GITHUB_TOKEN }}`，自动更新推送到 `best_us.txt`。

```yaml
name: 自动筛选并更新美区优选节点

on:
  schedule:
    - cron: '0 20 * * *' # 每天北京时间 04:00 执行
  workflow_dispatch:      # 支持手动一键触发

permissions:
  contents: write

jobs:
  update-nodes:
    runs-on: ubuntu-latest
    steps:
      - name: 检出仓库
        uses: actions/checkout@v4

      - name: 设置 Python 运行环境
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: 执行美区节点筛选
        run: python scripts/filter_us_nodes.py

      - name: 提交并推送最新节点
        run: |
          git config --local user.email "github-actions[bot]@users.noreply.github.com"
          git config --local user.name "github-actions[bot]"
          git add best_us.txt
          if git diff --staged --quiet; then
            echo "节点列表无变化，无需提交"
          else
            git commit -m "自动更新: 筛选最新 Top 15 美区优选节点 [$(date -u +'%Y-%m-%d %H:%M:%S') UTC]"
            git push
          fi
```

---

### 4. Cloudflare Worker 极简环境变量配置
在 Cloudflare 后台项目 **`vless2sub-worker`** 的【设置】$\rightarrow$【变量和机密】中，配置保持极度精简：

| 变量名 | 设置值 | 说明 |
| :--- | :--- | :--- |
| **`ADDAPI`** | `https://raw.githubusercontent.com/cuiyuanqing/ipselect/main/best_us.txt` | **唯一优选数据源**，指向 GitHub Actions 自动维护的节点文件 |
| **`HOST`** | `proxy.19940407.xyz` | 您的 edgetunnel 伪装域名 |
| **`UUID`** | `8204f875-4cc7-2277-c284-4d3cf4c5c6df` | 您的真实 VLESS UUID |
| **`TOKEN`** | `chuiyuanwo` | 快速订阅访问路径 `/chuiyuanwo` |
| **`ADD`** | **【彻底清空/删除】** | 不再需要本地写死静态节点 |
| **`ADDCSV`** | **【彻底清空/删除】** | 杜绝全量 CSV 导致节点爆炸 |
| **`DLS`** | **【彻底清空/删除】** | 随 CSV 一并移除 |

---

## 五、 日常运维与即时验证速查命令 (Cheat Sheet)

### 1. 本地手动重新执行筛选并推仓
```bash
cd D:\nice_wall\ipselect
python scripts/filter_us_nodes.py
git add best_us.txt
git commit -m "chore: 手动更新优选节点"
git push origin main
```

### 2. 验证 GitHub Raw 节点分发是否生效
```bash
curl -s "https://raw.githubusercontent.com/cuiyuanqing/ipselect/main/best_us.txt"
```

### 3. 验证优选生成器当前输出的节点名单与数量
```bash
python -c "
import urllib.request, base64
req = urllib.request.Request('https://sub.19940407.xyz/chuiyuanwo', headers={'User-Agent': 'v2rayN'})
with urllib.request.urlopen(req) as res:
    data = base64.b64decode(res.read()).decode('utf-8')
    lines = [l for l in data.splitlines() if l.strip()]
    print(f'当前优选节点总数: {len(lines)}')
    for l in lines:
        print(' -', urllib.parse.unquote(l.split('#')[-1]))
"
```

### 4. 验证 edgetunnel 主订阅汇聚结果
```bash
curl -s "https://proxy.19940407.xyz/sub?token=8204f8754cc72277c2844d3cf4c5c6df" | base64 -d | grep -o '#.*'
```
