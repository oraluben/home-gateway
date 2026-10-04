# 组件与故障边界

Ubuntu 网关内的基础转发、DNS、容器、订阅刷新分别由 systemd 管理。它可以运行在 Hyper-V VM 或专用 Ubuntu 主机上；网关核心与持久化格式相同。

| 组件 | 职责 | 持久化 |
|---|---|---|
| `home-gateway-base` | nftables NAT、公司网段防止错误转发、DNS 监听 | 配置；规则开机重建 |
| `home-gateway-dns` | 把 VPN 推送的 DNS/域名交给 systemd-resolved | 策略现场；重连重新下发 |
| `home-gateway` | 一个容器内监督 Mihomo 与 OpenConnect | 配置与独立数据目录 |
| `home-gateway-subscription.timer` | 每日刷新完整订阅 | 上次有效订阅与状态 |
| MetaCubeXD | 网页面板 | 浏览器登录信息；选择保存在 Mihomo |
| 共享 Python 工具 | SSH 部署、备份、诊断与有限重试 | 操作机配置与 SSH 身份 |
| `hosts/hyperv` / `hosts/linux` | VM 创建与恢复 / 专用 Ubuntu 目标准备 | 平台参数；主机初始化记录 |

Mihomo 关闭自身 TUN 和 DNS，由 nftables 透明转发及主机 DNS 统一处理。VPN 获得的网段和 DNS 地址优先排除代理，交由 Linux 路由；VPN 退出后保留已知公司网段的转发限制。IPv6 没有实现。

OpenConnect 只进行有限次数连接，底层重连也有时间上限；Mihomo 进程最多重试三次。整个监督程序异常退出时，systemd 也有三次上限。手动重试清零对应组件计数，彼此无需同时重启。

容器退出后透明转发规则被清理，Ubuntu 的基础 NAT/DNS 仍然运行。Windows 或 VM 退出、或原生 Linux 主机退出时基础转发也消失，单主机架构没有自动接管。这是当前方案的故障边界。

平台适配与配置边界详见 [hosts.md](hosts.md)。初始化与日常部署分开，预检不会隐式安装 Docker 或改主机网络设置。

参考上游的组件化和文件挂载方式：[Mihomo](https://github.com/MetaCubeX/mihomo)、[MetaCubeXD](https://github.com/MetaCubeX/metacubexd)、[OpenConnect](https://www.infradead.org/openconnect/)、[pass](https://www.passwordstore.org/)。本项目只添加家庭网关的路由、配置组合、有限重试和操作工具。
