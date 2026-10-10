# luci-app-ipselect 安装指南 (OpenWrt 25 / iStoreOS 25 / apk 环境)

OpenWrt 25.12+ 起包管理器全面由 `opkg` 升级为 `apk` (Alpine apk-tools 3.x)。
由于新版系统已不再预置 `opkg` 二进制，本目录提供适用于 OpenWrt 25 的两套免编译一键安装方案：

### 推荐方案：单文件自解压自安装脚本
1. 上传 `luci-app-ipselect-openwrt25-installer.sh` 到路由器的 `/tmp/` 目录；
2. 在终端执行：
   ```bash
   sh /tmp/luci-app-ipselect-openwrt25-installer.sh
   ```
3. 脚本会自动将 7 大核心文件释放到系统目录、修正 0755/0644 权限、配置开机自启、刷新 LuCI 菜单缓存并重载服务。

### 备用方案：tar.gz 压缩包
1. 上传 `luci-app-ipselect_1.1.0-1_openwrt25.tar.gz` 到路由器的 `/tmp/` 目录；
2. 在终端解压并执行安装：
   ```bash
   tar -xzf /tmp/luci-app-ipselect_1.1.0-1_openwrt25.tar.gz -C /tmp/
   sh /tmp/install.sh
   ```
