include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-ipselect
PKG_VERSION:=1.1.0
PKG_RELEASE:=1
PKG_MAINTAINER:=JeffSmith
PKG_LICENSE:=MIT

LUCI_TITLE:=LuCI support for IPSelect (Cloudflare Speedtest & Best IP Selector)
LUCI_DEPENDS:=+python3 +python3-requests +git +git-http
LUCI_PKGARCH:=all

include $(INCLUDE_DIR)/package.mk

define Package/luci-app-ipselect
  SECTION:=luci
  CATEGORY:=LuCI
  SUBMENU:=3. Applications
  TITLE:=LuCI support for IPSelect
  DEPENDS:=+python3 +python3-requests +git +git-http
  PKGARCH:=all
  MAINTAINER:=JeffSmith
endef

define Package/luci-app-ipselect/description
  LuCI web interface for ipselect node selector and speedtest tool.
endef

define Build/Configure
endef

define Build/Compile
endef

define Package/luci-app-ipselect/install
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) ./root/etc/config/ipselect $(1)/etc/config/ipselect

	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) ./root/etc/init.d/ipselect $(1)/etc/init.d/ipselect

	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_BIN) ./root/usr/bin/ipselect-runner $(1)/usr/bin/ipselect-runner

	$(INSTALL_DIR) $(1)/usr/share/luci/menu.d
	$(INSTALL_DATA) ./root/usr/share/luci/menu.d/luci-app-ipselect.json $(1)/usr/share/luci/menu.d/luci-app-ipselect.json

	$(INSTALL_DIR) $(1)/usr/share/rpcd/acl.d
	$(INSTALL_DATA) ./root/usr/share/rpcd/acl.d/luci-app-ipselect.json $(1)/usr/share/rpcd/acl.d/luci-app-ipselect.json

	$(INSTALL_DIR) $(1)/usr/lib/lua/luci/controller
	$(INSTALL_DATA) ./luasrc/controller/ipselect.lua $(1)/usr/lib/lua/luci/controller/ipselect.lua

	$(INSTALL_DIR) $(1)/www/luci-static/resources/view/ipselect
	$(INSTALL_DATA) ./htdocs/luci-static/resources/view/ipselect/overview.js $(1)/www/luci-static/resources/view/ipselect/overview.js
endef

define Package/luci-app-ipselect/postinst
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] || {
	chmod 755 /usr/bin/ipselect-runner
	chmod 755 /etc/init.d/ipselect
	/etc/init.d/ipselect enable
	/etc/init.d/ipselect restart
	[ -s /lib/functions.sh ] && {
		. /lib/functions.sh
		default_postinst $$0 $$@
	}
	rm -rf /tmp/luci-indexcache* /tmp/luci-modulecache* 2>/dev/null || true
	/etc/init.d/rpcd reload 2>/dev/null || true
	/etc/init.d/uhttpd restart 2>/dev/null || true
	exit 0
}
chmod 755 $${IPKG_INSTROOT}/usr/bin/ipselect-runner 2>/dev/null || true
chmod 755 $${IPKG_INSTROOT}/etc/init.d/ipselect 2>/dev/null || true
exit 0
endef

define Package/luci-app-ipselect/postrm
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] || {
	rm -rf /tmp/luci-indexcache* /tmp/luci-modulecache* 2>/dev/null || true
	/etc/init.d/rpcd reload 2>/dev/null || true
	/etc/init.d/uhttpd restart 2>/dev/null || true
	exit 0
}
exit 0
endef

$(eval $(call BuildPackage,luci-app-ipselect))
