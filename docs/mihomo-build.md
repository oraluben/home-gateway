# 指定 Mihomo 构建

默认仍使用 `versions.json` 固定的上游发行版。自定义核心通过显式构建清单选择；不把个人配置、订阅或凭据放进源码仓库和构建产物。

在 Linux / WSL 操作机上，从干净、已提交的本地 checkout 构建：

```sh
python3 tools/build-mihomo.py \
  --source ~/repo/mihomo \
  --go /path/to/go/bin/go \
  --output ~/.local/state/home-gateway/mihomo-builds/<build-name> \
  --feature connection-history-v1
```

工具只构建 Git 提交中的文件，忽略 checkout 中未跟踪或被忽略的文件。默认构建 Linux amd64 v1，可用 `--arch arm64` 交叉构建；两者都关闭 CGO、启用 `with_gvisor`、固定版本/提交时间和裁剪路径，产物目录必须不存在。生成二进制、LICENSE、对应源码归档与 `build.json`，固定各文件 SHA-256、源码 revision、Go 工具链和构建参数。工具不会建立 GitHub fork、推送分支、创建 release 或 PR。ARM64 产物支持不代表宿主安装器和 NanoPi 实机已经支持；当前完整部署仍限 Ubuntu 24.04 amd64。

部署时指定清单：

```sh
python3 tools/deploy.py --mihomo-build /path/to/build.json --validate-only
python3 tools/deploy.py --mihomo-build /path/to/build.json
```

Windows 也可使用 `./Deploy-Gateway.ps1 -MihomoBuild /path/to/build.json -ValidateOnly` 预检，去掉 `-ValidateOnly` 应用；Windows 文件路径会转换为所选 WSL 的路径。

也可在管理端部署配置中增加 `mihomo_build` 指向同一清单。清单路径按操作机解析，不进入容器运行设置；CLI 参数优先。后续重新部署时应继续指定这份清单或配置引用，省略则选择默认上游版本。已经运行的网关重启不会重新选核心，它继续使用已部署镜像。

清单格式 1 包含 `version`、`binary`/`binary_sha256`、`license`/`license_sha256`、`goos`/`goarch`、`features` 和 `source`；source 包含 HTTPS repository、40 位 revision、对应源码 archive 与 SHA-256。路径必须位于清单目录内，二进制必须是与清单一致的 Linux amd64/arm64 ELF。部署还会检查它与 `host.arch` 相符（省略时为 amd64）。构建元数据会替换本次部署包中的 Mihomo 条目，不改仓库默认的 `versions.json`，不同构建仍由不同镜像 recipe、校验和和部署专用标签区分。

选择 `connection-history-v1` 时，部署自动启用核心的 `connection-history: {capacity: 4096, log: false}`，并要求采集器成功接收、提交关闭记录后才算部署完成。无需修改 pass 中的个人运行配置。核心不提供该能力时，部署失败并回退，不默默退成采样模式。

预检不停止运行中的网关。切换镜像前保存一致的 SQLite 备份，失败回退时恢复旧数据库与镜像，避免新 schema 阻止旧采集器运行。对应源码归档与许可随自定义镜像携带；`gatewayctl status --json` 的 `mihomo_build` 可核对实际构建。

本地可运行真实流量对照，只访问临时 localhost 服务，不读取或更改运行中的网关配置：

```sh
python3 tools/check-mihomo-history.py --binary /path/to/mihomo-linux-amd64
```

它检查采样间完成的 TCP 请求、UDP 流量、独立读取、重放/采集器重启去重和缓冲区溢出提示。上游贡献说明与最终 PR 内容应使用 Mihomo 仓库的通用接口和测试，不引用私人网络或密码库。
