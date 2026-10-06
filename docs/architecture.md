# 组件与故障边界

Ubuntu 网关内的基础转发、DNS、容器、订阅刷新分别由 systemd 管理。它可以运行在 Hyper-V VM 或专用 Ubuntu 主机上；网关核心与持久化格式相同。

| 组件 | 职责 | 持久化 |
|---|---|---|
| `home-gateway-base` | nftables NAT、公司网段防止错误转发、DNS 监听 | 配置；规则开机重建 |
| `home-gateway-dns` | 把 VPN 推送的 DNS/域名交给 systemd-resolved | 策略现场；重连重新下发 |
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
