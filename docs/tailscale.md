# Tailscale 远程管理与退出节点

第一阶段提供网关 SSH、面板隧道，以及 Windows 独立远程桌面入口。Tailscale 是 Linux 主机上的可选 systemd 服务，与 Docker 容器独立；Hyper-V 和原生 Linux 使用同一安装器。Windows 另装一个客户端，虚拟机或网关容器故障时仍有修复入口。Windows 关机、断电或家庭宽带故障仍会同时中断这两个入口。

默认只提供管理，不发布公司网段、不提供退出节点，也不接受其他节点的路由或 DNS。v0.7.1 起可显式启用 IPv4 出口适配；客户端选择退出节点后才会复用公司 VPN 与 Clash。安装并登录本身不会代理手机上网。

## 安装和登录

先正常部署 v0.6.1+ 网关。操作机已有 SSH 信任和管理缓存时，无需解锁 pass：

```sh
python3 tools/tailscale.py install
python3 tools/tailscale.py login
python3 tools/tailscale.py status
```

Windows 对应：

```powershell
./Tailscale-Gateway.ps1 install
./Tailscale-Gateway.ps1 login
./Tailscale-Gateway.ps1 status
./Install-TailscaleWindows.ps1 -Action Install
./Install-TailscaleWindows.ps1 -Action Login
```

按工具输出的登录链接，在浏览器使用同一个个人账号授权两台设备。安装 Windows 服务会触发系统管理员授权；安装不主动重启 Windows。Windows 客户端启用 unattended，因此用户注销后仍保持运行。

Linux 从官方签名 APT 源安装固定版本；Windows MSI 校验仓库记录的 SHA256 与 Tailscale Inc. 的有效 Authenticode 签名。版本位于 `hosts/tailscale-versions.json`。APT hold 与客户端自动更新关闭用于避免后台升级改变网关行为；需要人工定期更新。升级时更新版本记录和 MSI 校验值，显式解锁/安装 Linux 包，并复核兼容配置、局域网转发和远程连接。初次安装脚本拒绝接管不属于此项目的现有 Tailscale 安装，不会自动升级已安装版本。

## 远程使用

在管理台或 `tailscale status` 查看设备的 Tailscale IP。使用现有 SSH 公钥登录 Linux，Tailscale SSH 保持关闭：

```sh
ssh gateway@<网关-Tailscale-IP>
gatewayctl status
gatewayctl check
gatewayctl tailscale-status
gatewayctl tailscale-netcheck
gatewayctl logs-tailscale
```

最后三个动作由主机入口执行，即使网关容器停止也可用。只重启远程管理服务使用 `sudo systemctl restart tailscaled`；临时停止使用 `sudo systemctl stop tailscaled`。

同一 SSH 主机首次用新地址连接时，核验它与局域网地址相同的主机密钥；已固定已知主机文件的操作机可使用 `-o HostKeyAlias=<原局域网-IP>` 复用已核验记录。不要关闭 SSH 主机密钥校验。

面板保持绑定原局域网地址，通过 SSH 隧道访问：

```sh
ssh -N -L 127.0.0.1:19090:<网关局域网-IP>:9090 gateway@<网关-Tailscale-IP>
# 浏览器打开 http://127.0.0.1:19090/ui/ ，继续使用原有面板密钥
```

Windows 远程桌面连接它自己的 Tailscale IP；前提是 Windows 已启用远程桌面，防火墙允许该接口的 RDP。安装器不额外开放远程桌面或改账户权限。初次接入时核验这两个条件。

手机无 SSH 客户端时，可用 Tailscale Serve 作为仅 tailnet 可见的面板入口：

```sh
sudo tailscale serve --bg --http=9090 http://<网关局域网-IP>:9090
sudo tailscale serve status
# 使用输出的 MagicDNS 主机名打开 /ui/；API 仍需原密钥。
# 关闭此入口：sudo tailscale serve --http=9090 off
```

此命令已在固定版本 1.102.5 验证局域网后端。Serve 不把端口开放给公网，不启用 Funnel；状态由 Tailscale 自己持久保存，重新登录/重建后按需恢复。用 IP 访问 HTTP Serve 可能返回 404，应使用命令输出的主机名。来源：[Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve)。

固定设备需要在 Tailscale 管理台检查 node key 的到期策略。到期后可能需要重新授权；它不会影响本地 Wi-Fi 上网。设备身份分别保存在 Linux `/var/lib/tailscale` 和 Windows `%ProgramData%\Tailscale`，属于私有运行状态，不进 Git、镜像或网关配置备份。重建后重新登录并删除旧设备，不复制同一个身份给两台机器。交互登录不需要向 pass 增加凭据，也不需要给网关安装私有 yadm。

## 与透明代理共存

Tailscale 使用原生 nftables 并保留自己的规则。主机准备工具只认可项目安装记录、服务配置及已知链/规则，仍拒绝其他防火墙管理器和陌生规则。`TS_DEBUG_FIREWALL_MODE` 是上游暂时性配置，因此版本升级必须复核实际规则。来源：[官方防火墙模式说明](https://tailscale.com/docs/features/firewall-mode)。

Tailscale 保留 packet mark 的 `0xff0000` 位；网关 v0.6.1 使用低 16 位的 `0x7001`，并在 Tailscale 的 connmark 恢复之后执行透明代理规则。Tailscale 地址 `100.64.0.0/10` 从普通透明代理中排除。来源：[上游 mark 定义](https://github.com/tailscale/tailscale/blob/v1.102.5/tsconst/linuxfw.go)。

Tailscale 1.98+ 的 netfilter 设置会把全局 `src_valid_mark` 改为 1，使 TPROXY 的反向源地址校验走本地代理表，可能丢掉局域网连接；仅检测网关自身 HTTP 或混合代理端口无法发现。此项目将该参数持久设置为 0，并在 tailscaled 服务的挂载命名空间内把该单独文件绑定为只读，阻止登录、重新配置和重启时改写。宿主网络命名空间保持共享，其他 sysctl 不受此绑定影响。日志中 `failed to enable src_valid_mark ... read-only file system` 是该兼容隔离的预期结果；仍应检查 `tailscale status` 的健康信息。来源：[上游故障报告](https://github.com/tailscale/tailscale/issues/19796)。

`gatewayctl status/check` 检查全局及 LAN 接口的实际 `src_valid_mark`，冲突时返回失败，不自动与其他服务反复争抢设置。此次集成已经在 Hyper-V Ubuntu 24.04 上验证登录、SSH、Tailscale 服务重启和局域网连接；尚未在独立 Linux 硬件上验证。

## 怎样验收

检查分成实际路径和故障恢复，单次服务状态不能代替整条链路：

| 路径 | 正确的检查入口 | 能证明什么 |
|---|---|---|
| 网关自身出站 | `gatewayctl check` | 普通 HTTPS、混合代理、公司 VPN/DNS 可达；不单独证明下游透明代理正常 |
| Wi-Fi 透明代理 | 手机或 Mac 连 Wi-Fi，关闭本机 VPN/代理；打开国内、代理、内网站点 | 流量真正经过路由器和网关；结合入站连接应答与转发计数确认路径 |
| 网关配置 | `gatewayctl status --json`、主机准备检查 | 路由规则、转发表、源校验参数、DNS、组件状态与已知要求一致 |
| Tailscale 管理 | 用 Tailscale IP 新建 SSH 连接；验证 RDP 和面板隧道 | 覆盖加密网络的服务访问，不只是 Tailscale ping |
| 容器故障 | 容器停止时，新建 Tailscale SSH 连接并执行主机诊断 | 管理入口独立于 VPN/代理容器；不能证明整台 Windows 故障时可用 |
| 启动恢复 | 重启 tailscaled 和 Linux VM 后重复状态及 Wi-Fi 检查 | 排除只临时修复参数、开机顺序和设备身份未持久化的问题 |
| 真正外网 | 手机关 Wi-Fi 用移动网络，或 Mac 接外部热点后重复管理检查 | 排除局域网直连掩盖公网穿透或中继问题；记录 direct/relay 和延迟 |

终端卡顿还需在同一客户端对比实际 WSS 响应、TCP 重传和延迟分布；一个平均 ping 或一次成功打开网页不足以判断稳定性。外出复用代理/公司 VPN 的验收，要在退出节点适配完成后再做。

## 启用退出节点

启用分三层，任一层缺失都不能把“已连接”当成出口可用：

1. 在 pass 部署条目的 `runtime.network` 增加 `"tailscale_exit": true`。先运行 `tools/prepare-host.py --apply`，再按常规验证和部署；Windows 对应 `Prepare-Host.ps1 -Apply`、`Deploy-Gateway.ps1 -ValidateOnly` 与 `Deploy-Gateway.ps1`。
2. `python3 tools/tailscale.py exit-on`（Windows：`./Tailscale-Gateway.ps1 exit-on`）。工具先检查镜像出口适配与网关连通性，再发布默认路由。
3. 在 Tailscale 管理台 home-gateway 的 Edit route settings 中启用 Use as exit node。然后手机/Mac 的 Exit node 选择 home-gateway，接受 Tailscale DNS。仍选“无”时，只能访问管理网络；Google 和公司业务流量不经过家里。

IPv4 公司目的地址绕过透明代理、走 OpenConnect；VPN 断线后保留已知网段的拒绝规则。其余 IPv4 TCP/UDP 经过原有 Clash 订阅分流，Tailnet/私网/本机服务与 DNS 流量排除。使用退出节点时，Tailscale 原生 DNS 代理使用出口节点的解析配置，需从真正远端验证公司域名；客户端手工指定其他 DNS或浏览器自带 DoH 会影响结果。来源：[退出节点说明](https://tailscale.com/docs/features/exit-nodes)。

退出节点自身的接收过滤器会从默认路由中排除 RFC1918 私网，所以仅发布 `0.0.0.0/0` 不足以访问公司 `10.x` 地址。可选主机服务 `home-gateway-tailscale-policy` 将 VPN 实际下发的网段和 DNS 地址同步到本机 Tailscale 的非默认发布列表；策略计算位于镜像源码，主机适配只使用本机 Tailscale CLI。切换 VPN profile 自动替换列表；断线继续声明缓存网段，流量由网关拒绝。最多四次发布尝试，失败间隔 15/30/60 秒，持续失败停止写入，可用 `gatewayctl retry-tailscale-policy` 重试，日志使用 `gatewayctl logs-tailscale-policy`。该服务不重连 VPN，也不修改系统 DNS。来源：[固定版本上游过滤实现](https://github.com/tailscale/tailscale/blob/v1.102.5/ipn/ipnlocal/local.go)。

在默认个人 tailnet 的访问策略下，选择已批准的退出节点即可使用这些声明，无需批准每条公司网段；本方案已用手机移动网络验证。管理台可能显示公司网段“等待批准”，它们目前用来补全出口的接收过滤器。若批准网段并让客户端接受路由，将额外开启不选退出节点也访问这些网段的分流模式；当前不依赖这条路径。自定义 grants/ACL 时需确认明确允许所需公司目的地址。VPN 下发默认路由或与 `100.64.0.0/10` 重叠时，本镜像拒绝应用该 VPN 策略。

本镜像当前只支持 IPv4 出口。IPv6 转发开启用于让远端收到明确的 ICMPv6 no-route；从 tailscale0 转发的 IPv6 被拒绝，不走公网直连，双栈应用可回退到 IPv4，只有 IPv6 的服务不能使用此出口。访问网关本身的 Tailscale IPv6 地址属于本地输入，SSH 不受转发拒绝影响。启用 IPv6 转发时保留上游路由通告接收。

紧急撤销发布可以运行 `python3 tools/tailscale.py exit-off`，无需容器工作或解密 pass；管理连接仍保留。手机立即恢复普通移动上网时，将 Exit node 改回“无”。需要停用镜像适配时，在 pass 将 `tailscale_exit` 改为 false 并重新部署，先撤销发布。不会自动撤销服务端管理台的批准记录；每次启用前仍需重新验证路径。

`gatewayctl status/check --json` 显示出口适配是否配置、接口是否存在、网段同步状态及实际源校验参数；`gatewayctl tailscale-status` 查看 Tailscale 状态。这些检查仍不能代替客户端的实际访问。验收至少覆盖移动网络下的国内、Google、公司网站、DNS、Clash 命中的分组，以及公司 VPN 停机后的断线保护。网关自身请求不会经过 tailscale0，不能代替该测试。

## 链路质量与后续扩展

先用手机移动网络或 Mac 外部网络验证管理访问，记录 direct/relay 路径、延迟和丢包。局域网直连成功不能代替外网验证；只走远距离 DERP 中继时，终端交互和退出节点体验可能较差。

便携路由器作为 Tailscale 客户端，等远端真实路径验证完成后再接入。只有公司/家庭网段的分流模式、完整 IPv6 代理与跨公网 MTU/吞吐验证仍需按使用需求扩展；当前不自动发布家庭 LAN 网段，不修改客户端与所处网络重叠的私网路由。
