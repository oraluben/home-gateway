# 平台部署与职责

网关运行环境统一为 Ubuntu 24.04 amd64。`host.backend` 选择 `hyperv` 或 `linux`，共享部署通过 SSH 访问该 Ubuntu 目标，与操作机的系统分开。

| 层 | 目录 / 入口 | 职责 |
|---|---|---|
| 网关核心 | `image/`、`compose.yaml`、基础网络与 DNS 脚本 | 单容器监督、透明代理、VPN、DNS、订阅和状态 |
| 共享维护 | `tools/` | 凭据提取、部署事务、日志与组件重试、加密备份、恢复、通用 cloud-init seed |
| Hyper-V 适配 | `hosts/hyperv/` | VHDX、Windows 外部交换机、VM、启动设置、创建失败后的网络恢复 |
| Linux 目标准备 | `hosts/linux/prepare.py` | Ubuntu 条件检查、Docker Engine 与系统依赖、指定网卡的转发设置 |
| Windows 便捷入口 | 顶层 PowerShell | WSL 路径转换、原命令兼容、面板剪贴板与浏览器 |

两种部署使用同一 Docker 镜像、`config/gateway.yaml`、`data/` 和加密备份格式。Hyper-V 参数与操作机的 yadm/pass 不进入容器。Linux 操作机也能通过 SSH 维护 Hyper-V 中的网关；Windows 操作机同样能维护原生 Linux 网关。

## Hyper-V

私有配置使用 `host: {"backend": "hyperv", "hyperv": {...}}`。Windows 便捷入口仍需要 `operator.WslDistribution`；WSL 使用 Windows SSH 时按示例填写执行文件、私钥与 known_hosts 路径。旧文件中的顶层 `hyperv` 会在读取时转换，不改写私有文件。

新建的顺序：准备固定版本的 cloud image 与 seed，创建 VM，核验 SSH 主机密钥，准备 Ubuntu 目标，预检并部署。顶层 `Prepare-VM.ps1`、`Initialize-VM.ps1`、`Get-VMStatus.ps1`、`Restore-HostNetwork.ps1` 继续可用。磁盘转换和 ISO 创建留在 Hyper-V 适配中，cloud-init 内容由共享工具生成。

## 专用 Linux 主机

使用 `examples/deployment.linux.example.json`。它不需要 Hyper-V、WSL、虚拟机参数或 SSH 公钥注入字段。操作机的 SSH 私钥与 known_hosts 支持 `~`；`runtime.network.interface` 填实际的网卡名，例如 `enp1s0`。不要把配置中的示例接口照搬到目标。

部署前在目标完成：

1. Ubuntu Server 24.04 amd64，systemd 与 systemd-resolved 正常运行。
2. LAN 接口具有持久静态 IPv4，地址在上级 DHCP 池外；默认路由与公共 DNS 指向预期上游。持久网络配置通过 Ubuntu 的 Netplan 等原生管理方式完成，项目不接管它。
3. SSH 可用，部署用户具备无需交互的 sudo 权限。通过可信渠道确认 SSH 主机密钥。
4. 目标专用于网关，没有其他 Docker 容器、UFW、firewalld、nftables.service 或其他已存在的防火墙表。额外 SSH 访问控制应放在上游设备。

从操作机运行：

```sh
python3 tools/prepare-host.py --config ~/.config/home-gateway/deployment.json
python3 tools/prepare-host.py --apply
python3 tools/deploy.py --validate-only
python3 tools/deploy.py
python3 tools/gateway.py status
```

检查阶段上传短暂的检查脚本，不修改系统配置，也不解密 pass 凭据。它检查系统、架构、网卡/IP/默认路由、DNS/代理端口、已有防火墙与容器、Docker 和 TUN。输出 `blockers` 与拟执行的 `changes`。

显式 `--apply` 安装缺少的依赖，向 Docker 配置补充关闭 bridge 与 Docker 自有防火墙管理的键，只写并应用本项目的 sysctl 文件。已有 Docker 配置的其他键保留；存在冲突键时拒绝处理。需要变更 Docker 配置且网关容器正在运行时，也拒绝重启 Docker。原配置保存在目标 `/var/lib/home-gateway/host-before/`，供人工恢复参考；这是主机初始化材料，不属于可跨平台恢复的网关状态备份。首次初始化不是整个系统的原子事务；中途失败先检查原因再显式重试，不自动循环。

主机已准备好后，重复执行为空操作，不访问软件源或重启 Docker。部署和 `--validate-only` 都先检查主机条件，不隐式执行初始化。

网关运行时管理自己的 nftables 表、策略路由、VPN 路由与 systemd-resolved 设置。Linux 主机直接运行时，这些操作作用于物理主机。当前不支持与其他容器、防火墙或本机 VPN 共享网络管理；需要这些工作负载时，使用独立网关设备或独立虚拟机更合适。[Docker 主机网络](https://docs.docker.com/engine/network/drivers/host/)、[Docker 防火墙行为](https://docs.docker.com/engine/network/packet-filtering-firewalls/)。

## 后续平台

KVM 只需新增 QCOW2/桥接/虚拟机生命周期的适配，Ubuntu 环境、镜像与维护工具可继续共用，但目前没有 KVM 自动创建入口。ARM 需要对应架构的固定二进制和构建验证，不能仅更换 cloud image。

现有测试覆盖配置兼容、主机冲突拒绝、初始化重复执行与配置保留。共享初始化与部署已在当前 Hyper-V 的 Ubuntu 环境中验证；独立物理 Linux 主机的首次安装仍需实际设备验证。
