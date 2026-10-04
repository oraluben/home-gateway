# Third-party software

The appliance scripts are MIT licensed. Bundled programs retain their own licenses.

| Component | Version | License | Corresponding upstream source |
| --- | --- | --- | --- |
| Mihomo | v1.19.32 | GPL-3.0 | https://github.com/MetaCubeX/mihomo/tree/v1.19.32 |
| MetaCubeXD | v1.273.1 | MIT | https://github.com/MetaCubeX/metacubexd/tree/v1.273.1 |
| OpenConnect | Recorded in each built image | LGPL-2.1 | https://www.infradead.org/openconnect/ |
| Ubuntu packages | Recorded by dpkg in each image | Individual package licenses | /usr/share/doc/*/copyright |

`fetch-artifacts.py` includes the Mihomo and dashboard license notices in the build context.
No upstream program is modified. This repository publishes appliance source; it does not
publish prebuilt third-party binaries. Before distributing a built image, provide the
corresponding sources and notices required by the included packages' licenses.
