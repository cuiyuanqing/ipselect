# luci-app-ipselect 安装指南 (OpenWrt 24 / iStoreOS 22/24 / opkg 环境)

适用于 OpenWrt 24.10、23.05、22.03 及更早版本的标准 `opkg` IPK 安装包。

### 命令行安装
上传 `luci-app-ipselect_1.1.0-1_all.ipk` 到路由器的 `/tmp/` 目录，执行：
```bash
opkg update
opkg install /tmp/luci-app-ipselect_1.1.0-1_all.ipk
```

### Web 界面一键安装
在 LuCI 管理后台进入【系统】->【软件包】（或 iStore 商店），点击“上传软件包”，选择此 IPK 文件即可一键安装。
