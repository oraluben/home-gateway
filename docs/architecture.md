# 组件与故障边界

Ubuntu 网关内的基础转发、DNS、容器、订阅刷新分别由 systemd 管理。它可以运行在 Hyper-V VM 或专用 Ubuntu 主机上；网关核心与持久化格式相同。

## 代码边界

保持一台 Linux 宿主和一个业务容器。拓扑、CPU 架构与 Hyper-V 生命周期是三个独立维度，不为每种组合复制实现。

| 层 | 代码 | 边界 |
|---|---|---|
| 网络意图 | `image/topology.py` | 不执行命令；校验 LAN/WAN、静态/DHCP 上联、客户端网段；旧配置只在入口翻译 |
| Linux 规则生成 | `image/firewall.py` | 从拓扑和 VPN 策略生成 nftables 文本，不操作内核 |
| Linux 执行 | `image/network.py` | 原子替换本项目的表，检查与修复本项目的代理策略路由 |
| 宿主策略 | `image/host_policy.py` | 基础 NAT/公司网段保护的唯一常驻写入方，同时更新 systemd-resolved |
| 业务运行 | `image/gateway.py`、`component.py` | Mihomo/OpenConnect/统计的生命周期、有限重试；仅接管代理规则，不改基础规则 |
| 配置与状态 | `image/runtime.py` | 设备路径、配置读取、完整 JSON 原子发布；不访问 pass/yadm |
| 管理端渲染 | `tools/render.py` | 从部署意图生成最小运行配置；凭据解析由调用者传入，只读取启用组件的凭据 |
| 构建平台 | `tools/target_platform.py` | amd64/arm64 产物与 ELF 校验，与宿主安装器支持范围分开 |
| 平台初始化 | `hosts/` | Hyper-V 生命周期、Linux 依赖及系统检查，不包含订阅或 VPN 身份 |

入口脚本保留原名称；导入模块不会启动业务进程或修改网络。没有新增常驻容器、通用插件框架或配置继承系统。`host_policy.py` 运行在宿主，未装入业务镜像。

## 网络配置契约

现有 `network.address/gateway/dns` 加 `runtime.network.interface/clients` 继续可用，不需要修改 pass。管理端使用 `deployment_topology()` 翻译一次，实际下发的 `runtime.network` 使用统一的 LAN/WAN 表示；设备端也能读取旧格式，便于回滚。

新部署可把网络设置集中在顶层 `network`，此时不能再配置 `runtime.network`，避免两个来源相互覆盖。例如现有单网口部署的等价形式：

```json
{
  "network": {
    "mode": "one-arm",
    "lan": {"interface": "enp1s0", "address": "192.168.50.201/24"},
    "wan": {"interface": "enp1s0", "method": "static", "gateway": "192.168.50.1", "dns": ["192.168.50.1"]},
    "clients": ["192.168.50.0/24"],
    "tailscale_exit": false
  }
}
```

单网口共享 LAN/WAN 的接口和地址；双网口 `router` 模式必须使用不同接口和静态 LAN 地址，WAN 可以是 DHCP 或完整静态配置。Mihomo 绑定 WAN，透明代理捕获 LAN。接口可以是宿主管理好的物理口、桥或 VLAN，核心不猜测网卡顺序，也不负责创建它们。

**当前只实现双网口的模型、校验和业务规则角色拆分。主路由 DHCP、WAN 防火墙、网络应用回滚尚未实现，安装器与基础规则渲染会明确拒绝 `router`，不会套用宽松的单网口转发规则。** ARM64 已支持自定义 Mihomo 的构建/校验，完整宿主初始化与默认产物仍限 amd64；不能据此直接部署 NanoPi。

下一阶段在 Linux 宿主层实现地址/DHCP/主路由防火墙和独立于 SSH 的网络回滚，核心继续消费同一拓扑。多 VPN、多个 LAN、IPv6 路由与无线 AP 管理不在本次抽象中预做。

## 策略与故障边界

VPN hook 发布 `data/vpn-policy.json`，包含 `up/prefixes/dns/domains`，不包含凭据。宿主和业务容器分别读取同一快照：宿主更新基础保护与 DNS，容器更新代理排除目标。策略文件原子替换，多个发布者使用独立临时文件，读者不会读到半份 JSON。损坏文件明确报错，不静默当成空策略解除保护。

启用但断线的 VPN 保留已知公司网段；明确禁用 VPN 时忽略旧策略，也不读取 VPN profile、密码或令牌。关闭 Clash 时不读取控制密钥/订阅 URL、不启动统计和订阅定时器；备份允许没有订阅缓存，旧备份仍兼容。

宿主沿用 `home-gateway-dns` 服务名，实际同时对账基础规则和 DNS，不再增加另一个轮询器。没有策略变化时不重写 nftables 或 DNS；每五秒发布 `network-status.json` 心跳，`gatewayctl status` 将其缺失、过期和失败与容器状态分别报告。部署会重启宿主策略服务，确保它读取本次配置。

管理端只保存连接信息和规范化网络等非秘密字段。默认家庭条目沿用 `~/.config/home-gateway/deployment.json`；其他 pass 条目使用同目录下 `targets/<来源散列>.json`，避免部署公司目标后默认命令误连它。所有目标的凭据仍只由管理端解密并最小化下发。

2026-10-25 验证：WSL 与当前 Ubuntu 24.04 宿主各通过 166 项测试；隔离业务容器通过 141 项测试（宿主管理/SSH 公钥测试在宿主执行），另有 16 组 nftables 内核检查。两份旧示例在 Tailscale 开/关、VPN 有/无策略下的 28 组规则/配置渲染与重构前完全相同。隔离验证只使用公开源码和测试数据。

随后已在现有 Hyper-V 网关完成预检、备份与实际部署，沿用 `0.9.1`，由部署专用镜像 ID 区分本次构建。直连、代理、公司站点与局域网 DNS 检查通过；节点选择保留，关闭记录记账正常，宿主策略与 Tailscale 路由同步正常。组件未发生自动重启。移动网络下的 Tailscale 客户端端到端访问本次未复测。

| 组件 | 职责 | 持久化 |
|---|---|---|
| `home-gateway-base` | nftables NAT、公司网段防止错误转发、DNS 监听 | 配置；规则开机重建 |
| `home-gateway-dns` | 对账基础转发保护，把 VPN DNS/域名交给 systemd-resolved | 策略现场；重连重新下发；服务心跳 |
| `home-gateway` | 一个容器内监督 Mihomo 与 OpenConnect | 配置与独立数据目录 |
| `home-gateway-subscription.timer` | 每日刷新完整订阅 | 上次有效订阅与状态 |
| MetaCubeXD | 网页面板 | 浏览器登录信息；选择保存在 Mihomo |
| 镜像内 `gatewayctl` | SSH 后本地诊断、连通性探测与组件控制 | 读取网关状态与日志；无需操作机配置 |
| 共享 Python 工具 | SSH 部署、备份、诊断与有限重试 | 操作机配置与 SSH 身份 |
| `hosts/hyperv` / `hosts/linux` | VM 创建与恢复 / 专用 Ubuntu 目标准备 | 平台参数；主机初始化记录 |

Mihomo 关闭自身 TUN 和 DNS，由 nftables 透明转发及主机 DNS 统一处理。VPN 获得的网段和 DNS 地址优先排除代理，交由 Linux 路由；VPN 退出后保留已知公司网段的转发限制。IPv6 没有实现。

透明代理还依赖 fwmark 路由规则及表 17001 的本地路由。监督程序每 5 秒核验并补回缺失项，正常时不改规则、不重连 VPN；`status` 的 `proxy_routing.ready` 单独检查内核中的实际规则。同优先级存在其他规则时明确报错，避免把冲突误报为正常。

安装时配置 `ManageForeignRoutingPolicyRules=no`，让 systemd-networkd 保留网关管理的规则。否则自动更新重启网络服务时可能删除这些规则，导致进程正常但下游设备全部无法上网。该设置用于专用网关目标，安装时不主动重启网络服务。选项行为见 [systemd 255 文档](https://github.com/systemd/systemd/blob/v255/man/networkd.conf.xml)。

OpenConnect 采用有限自动重试：默认 `attempts=4` 包括初次连接及最多 3 次重连，间隔 15、30、60 秒。只有进程运行且 VPN 策略连续处于已连接状态 600 秒后，才恢复重试预算，因此正常的后续断线仍可自动恢复，连续连接失败则停止。`status` 显示尝试次数与剩余等待时间；`stop-vpn` 会停用自动重试，`retry-vpn` 恢复预算。已有配置显式设置 `attempts=1` 时仍保持一次连接，需修改部署条目后重新部署。

OpenConnect 底层重连有时间上限；Mihomo 保持原来的三次尝试上限。整个监督程序异常退出时，systemd 也有三次上限。手动重试对应组件无需同时重启另一个组件。

容器退出后透明转发规则被清理，Ubuntu 的基础 NAT/DNS 仍然运行。Windows 或 VM 退出、或原生 Linux 主机退出时基础转发也消失，单主机架构没有自动接管。这是当前方案的故障边界。

平台适配与配置边界详见 [hosts.md](hosts.md)。初始化与日常部署分开，预检不会隐式安装 Docker 或改主机网络设置。

参考上游的组件化和文件挂载方式：[Mihomo](https://github.com/MetaCubeX/mihomo)、[MetaCubeXD](https://github.com/MetaCubeX/metacubexd)、[OpenConnect](https://www.infradead.org/openconnect/)、[pass](https://www.passwordstore.org/)。本项目只添加家庭网关的路由、配置组合、有限重试和操作工具。
