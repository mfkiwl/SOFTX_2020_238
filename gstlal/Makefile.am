ACLOCAL_AMFLAGS = -I gnuscripts
EXTRA_DIST = gstlal.spec
DISTCHECK_CONFIGURE_FLAGS = --enable-gtk-doc --enable-introspection
SUBDIRS = debian lib python gst bin share tests doc

# check that the most recent changelog entry's version matches the package
# version

check-local :
	@CHANGELOG_VERSION=`"head" -n 1 $(top_srcdir)/debian/changelog | "sed" -e "s?.*(\([.0-9]*\).*?\1?"` ; [ "$${CHANGELOG_VERSION}" = "$(PACKAGE_VERSION)" ] || { "echo" "debian/changelog <--> package version mismatch ($${CHANGELOG_VERSION} != $(PACKAGE_VERSION))" ; "false" ; }
