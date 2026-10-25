# Third-party software

The appliance scripts are MIT licensed. Bundled programs retain their own licenses.

| Component | Version | License | Corresponding upstream source |
| --- | --- | --- | --- |
| Mihomo | v1.19.32 | GPL-3.0 | https://github.com/MetaCubeX/mihomo/tree/v1.19.32 |
| MetaCubeXD | v1.273.1 | MIT | https://github.com/MetaCubeX/metacubexd/tree/v1.273.1 |
| OpenConnect | Recorded in each built image | LGPL-2.1 | https://www.infradead.org/openconnect/ |
| Ubuntu packages | Recorded by dpkg in each image | Individual package licenses | /usr/share/doc/*/copyright |

`fetch-artifacts.py` includes the Mihomo and dashboard license notices in the build context.
The default core is the unmodified pinned upstream release. An explicitly selected custom
Mihomo build carries its exact revision, binary/source SHA-256 digests and feature list.
Its verified license and corresponding source archive are included in the appliance at
`/usr/local/share/home-gateway/licenses/mihomo-LICENSE` and `mihomo-SOURCE.tar.gz`.
The selected build provenance is also recorded in the deployment receipt. This repository
publishes appliance source; it does not publish prebuilt third-party binaries. Before
distributing a built image, provide the corresponding sources and notices required by
all included packages' licenses.
