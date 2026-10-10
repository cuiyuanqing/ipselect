# luci-app-ipselect 安装包发布索引

本目录同时提供针对 **OpenWrt 24 (opkg)** 与 **OpenWrt 25 (apk)** 的安装包方案：

| 目标系统 | 包管理器 / 机制 | 产物目录与文件 | 安装指引 |
| :--- | :--- | :--- | :--- |
| **OpenWrt 24** (及 23/22/21) | `opkg` (标准 IPK) | `openwrt-24/luci-app-ipselect_1.1.0-1_all.ipk` | `opkg install luci-app-ipselect_1.1.0-1_all.ipk` 或 LuCI 网页直接上传 |
| **OpenWrt 25** (25.12+ / iStoreOS 25) | `apk` (免 SDK 一键包) | `openwrt-25/luci-app-ipselect-openwrt25-installer.sh` | 终端执行 `sh installer.sh` 一键就绪 |
